# 前台续跑方案迁移

## 目标

从 0.3.x 的后台 `codex exec resume` 切换到 0.5.x 的多通道前台注入。新方案不抢占当前前台，失败时仍可继续输入其它指令。

## 迁移步骤

1. 停止旧 watcher，避免旧进程继续产生隐藏任务：

   ```bash
   systemctl --user stop codex-session-auto-continue.service
   ```

2. 安装新 unit，确认以下开关存在且为关闭后台兜底：

   ```text
   CODEX_AUTO_CONTINUE_FOREGROUND=1
   CODEX_AUTO_CONTINUE_BACKGROUND_FALLBACK=0
   CODEX_AUTO_CONTINUE_FRONTEND_CHANNEL=auto
   ```

3. 重新加载并启动：

   ```bash
   systemctl --user daemon-reload
   systemctl --user enable --now codex-session-auto-continue.service
   ```

4. 检查 `$HOME/.local/state/codex-session-auto-continue/events.jsonl`。目标无法唯一定位、提示不稳定或 RPC 失败时应记录 `observe_only`/`failed`，不应出现新的后台 `codex exec resume`。

## 安全门禁

- 识别 `originator=codex-tui`、`codex_exec`、`virtual_employee`、`roadclaw`；后面三类默认只有配置 hook 才会实际注入。
- 用户取消、手动中断、审批和危险命令不注入。
- session、block、PTY 或 hook target 必须唯一；JWT 只从目标 WaveTerm Codex 进程环境临时读取，不写日志、不写状态、不放入 argv。
- 普通终端优先 tmux；裸 PTY 必须复核 PID starttime、stdin、cwd、cmdline，并显式开启对应开关。
- 虚拟员工只能通过显式 JSON hook 发送可见“继续”，未配置 hook 时保持 `observe_only`。
- payload 固定为 `继续\r`；不接受事件内容拼接输入。
- 单事件幂等；RPC 失败按 30/60/120/240/300 秒退避。
- 自动续跑不设失败次数上限；只在用户手动取消或中断时停止，状态文件仍限制历史长度。

## 回退

仅在明确需要临时兼容旧行为时设置 `CODEX_AUTO_CONTINUE_BACKGROUND_FALLBACK=1`，并在验证后恢复为 `0`。不应把后台兜底作为长期运行模式。

状态文件和日志默认保留：

```text
$HOME/.local/state/codex-session-auto-continue/state.json
$HOME/.local/state/codex-session-auto-continue/events.jsonl
$HOME/.local/state/codex-session-auto-continue/foreground.json
```
