# Codex 模型容量降级代理

本仓库可独立复制部署，不依赖其它本地 Git 仓库。运行参数和凭据从用户环境文件读取，禁止把真实环境文件提交到 Git。

版本：见 `VERSION`。

## 当前方案：多前台通道

watcher 只监控 Codex 会话 JSONL 的失败事件，不启动隐藏的 `codex exec resume` 子进程。前台通道由 `MultiFrontendAdapter` 路由：

- `waveterm`：使用 WaveTerm 原生 Unix socket。
- `terminal`：优先使用 tmux；不依赖 WaveTerm。裸终端只有在身份校验、显式开关和内核允许 `TIOCSTI` 时才可注入。
- `virtual_employee`：调用虚拟员工提供的显式 hook，由虚拟员工把可见的“继续”发送到自己的前端（例如 Feishu 话题）。
- `auto`：WaveTerm → 普通终端 → 已配置虚拟员工 hook，按唯一目标选择。

没有唯一目标、屏幕提示无法确认、hook 未配置或通道失败时，先记录 `observe_only`/`failed`，再按策略启动同模型后台 `resume` 兜底。

发生非手动失败时，处理顺序如下：

1. 容量错误按 `gpt-5.6-sol → gpt-5.6-terra → gpt-5.6-luna` 选择下一档模型。
2. 前台适配器按通道发现 Codex 进程；session、block/PTY 或 hook target 必须唯一。
3. WaveTerm scrollback 或 tmux pane 的末尾提示符必须连续两次稳定（至少间隔 1 秒），且不能处于工作、审批、确认、危险命令状态。
4. 发送固定 payload `继续\r`。WaveTerm 走 `authenticate → setpeerinfo → controllerinput`；普通终端走已验证目标；虚拟员工走结构化 hook。

目标不唯一、提示不可确认、RPC 失败或处于安全门禁时，优先走同模型后台 `resume` 兜底；后台失败只写 `events.jsonl` 和状态文件，保持当前前台可交互。容量降级必须使用带 `-m` 的后台 `resume`，不能只给原 TUI 输入“继续”。

当前最低模型为 Luna：Luna 仍容量失败时不再切到更低模型，优先给绑定的 WaveTerm block 发送固定 `继续\r`；目标不可用时才用 Luna 后台 `resume` 重试。

自动续跑不设失败次数上限；容量不足或其它非手动失败都会继续触发“继续”，直到成功或用户手动取消/中断。状态文件只保留有限失败历史，避免无限增长。适配器记录事件幂等键和 30/60/120/240/300 秒退避，避免重复注入。

## 开关与配置

服务模板默认配置：

```text
CODEX_AUTO_CONTINUE_FOREGROUND=1
CODEX_AUTO_CONTINUE_BACKGROUND_FALLBACK=1
CODEX_AUTO_CONTINUE_FRONTEND_CHANNEL=auto
CODEX_AUTO_CONTINUE_TERMINAL_PTY=0
CODEX_AUTO_CONTINUE_TERMINAL_ASSUME_READY=0
```

- 停止自动续跑：`systemctl --user disable --now codex-session-auto-continue.service`。
- 只观察不注入：将 `CODEX_AUTO_CONTINUE_FOREGROUND=0` 后重启 watcher。
- `CODEX_AUTO_CONTINUE_BACKGROUND_FALLBACK=1` 是当前默认兜底开关；它只在前台目标不可用或模型需要切换时启动 `resume`。
- `CODEX_AUTO_CONTINUE_FEISHU_NOTIFY=1` 时，前台成功输入“继续”或后台成功启动 `resume` 会进入飞书汇总队列，默认每 600 秒发送一条汇总；发送失败只记录告警并保留队列重试，不阻塞续跑。通知目标必须通过 `CODEX_AUTO_CONTINUE_FEISHU_CHAT_ID` 配置，仓库不提供默认群 ID。
- 普通终端建议在 tmux 中启动 Codex，例如 `tmux new-session -A -s codex` 后运行 `codex`；watcher 会校验 pane tty，再用 `tmux send-keys "继续" C-m`，效果与手动输入一致。
- 裸终端需要同时设置 `CODEX_AUTO_CONTINUE_TERMINAL_PTY=1` 和 `CODEX_AUTO_CONTINUE_TERMINAL_ASSUME_READY=1`。注入使用 `TIOCSTI`，部分发行版内核会禁用它；禁用时不会误写终端输出，自动回退后台 `resume`。风险较高，默认关闭。
- 虚拟员工需配置可执行 hook，例如 `CODEX_AUTO_CONTINUE_VIRTUAL_EMPLOYEE_HOOK=/绝对路径/continue-hook`。hook 从 stdin 接收 JSON，成功返回码 `0`；请求包含 `protocol=1`、`action=continue`、`session_id`、`event_id`、`hook_id`、`payload=继续\r` 和 `input_data64`。禁止 shell 拼接和把 token 写入日志。
- 完全关闭容量代理：停止 `codex-model-fallback-proxy.service`，恢复 Codex `model_providers.OpenAI.base_url`。
- 代理监听地址、端口、模型链和冷却时间分别由 `CODEX_FALLBACK_LISTEN_HOST`、`CODEX_FALLBACK_LISTEN_PORT`、`CODEX_FALLBACK_MODEL_CHAIN`、`CODEX_FALLBACK_COOLDOWN_SECONDS` 配置。

## 请求层容量降级

仅当上游明确返回容量错误，且当前 HTTP/SSE 响应尚未产生正文或工具调用输出时，代理才按以下链路切换模型：

1. `gpt-5.6-sol`
2. `gpt-5.6-terra`
3. `gpt-5.6-luna`

已经产生有效输出后再出现容量错误，不重放当前请求；代理记录模型冷却，使下一次新请求优先使用后备模型。

## 状态与验证

```text
$HOME/.local/state/codex-session-auto-continue/state.json
$HOME/.local/state/codex-session-auto-continue/events.jsonl
$HOME/.local/state/codex-session-auto-continue/foreground.json
$HOME/.local/state/codex-session-auto-continue/logs/
```

```bash
systemctl --user status codex-model-fallback-proxy.service --no-pager
systemctl --user status codex-session-auto-continue.service --no-pager
curl -fsS http://127.0.0.1:4477/_fallback/status | jq
```

代码验证：

```bash
cd "$HOME/model_fallback_proxy_standalone"
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m py_compile model_fallback_proxy.py session_auto_continue.py waveterm_foreground.py foreground_channels.py
.venv/bin/python -m unittest -q
```

首次启用建议先执行一次只读扫描：

```bash
CODEX_AUTO_CONTINUE_FOREGROUND=0 CODEX_AUTO_CONTINUE_BACKGROUND_FALLBACK=0 \
  .venv/bin/python session_auto_continue.py --once
```

该命令只做只读扫描；systemd 服务才按环境变量决定是否启用注入。

## 安装与回滚

```bash
mkdir -p "$HOME/.config/codex-model-fallback-proxy"
install -m 600 .env.example "$HOME/.config/codex-model-fallback-proxy/environment"
# 编辑 environment，至少填写 CODEX_FALLBACK_UPSTREAM_URL；需要飞书通知时再填写 chat_id。
systemctl --user daemon-reload
systemctl --user enable --now codex-model-fallback-proxy.service
systemctl --user enable --now codex-session-auto-continue.service
```

回滚：

1. `systemctl --user disable --now codex-session-auto-continue.service`。
2. 将 `$HOME/.codex/config.toml` 的 `model_providers.OpenAI.base_url` 恢复为上游地址。
3. 删除或注释 `[features]` 中为代理关闭请求压缩的配置。
4. `systemctl --user disable --now codex-model-fallback-proxy.service`。

回滚不自动删除状态与日志，便于审计；确认无误后再人工清理。
