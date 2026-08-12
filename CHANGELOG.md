# 变更记录

## 项目名称

**继续工程师（JXG / Continue Engineer）**：容量不够自动换档，失败自动重试，前台自动续上。

## 0.5.5 - 2026-08-12

- 识别 `stream disconnected before completion: An error occurred while processing your request` 服务端瞬时错误。
- 该错误与其它可恢复容量错误统一进入自动换档和前台“继续”流程。

## 0.5.4 - 2026-08-12

- 识别“stream disconnected before completion: Our servers are currently overloaded”及远程 compact 同类错误。
- 代理请求自动重试下一档 `gpt-5.6` 模型；watcher 按现有前台策略自动发送“继续”。
- 非 `gpt-5.6` 模型仍不切换，仅继续当前会话。

## 0.5.3 - 2026-08-10

- 修复容量降级强制启动后台 `codex exec resume` 导致的 `thread-store conflict`。
- Codex TUI 容量失败统一通过当前前台输入“继续”；提示未就绪时持久化排队并延迟重试。
- 后台 resume 兜底默认关闭，避免隐藏进程抢占用户交互；仅非交互会话显式开启时使用。

## 0.5.2 - 2026-08-07

- 同一会话 30 秒内连续成功发送 3 次“继续”后，延迟 30 秒再发送下一次。
- 延迟期间保留最新失败事件，任务成功或手动取消时清空延迟队列。
- 增加限流、延迟恢复和状态清理测试。

## 0.5.1 - 2026-08-07

- 按 `src/tests/scripts/configs/docs/systemd` 规范重组独立仓库，运行入口统一为 Python package 和 runtime wrapper。
- 增加 shell/Python/单元测试/脱敏扫描质量门禁，systemd 不再依赖根目录脚本路径。
- 重写 README，补充请求级降级、SSE 安全重试、无限续跑、多前端、本地通知、平台依赖和独立部署说明。

## 0.5.0 - 2026-08-05

- 增加前台通道路由：WaveTerm、tmux/普通终端、虚拟员工 hook 可独立选择或自动选择。
- 普通终端发送前必须复核 PID starttime、stdin PTY、cwd、Codex cmdline，并使用目标锁避免并发写入。
- 虚拟员工只接受显式 JSON hook，续跑由虚拟员工自己的可见前端完成；未配置 hook 时保持 observe_only。
- watcher 兼容 `codex_exec`/`virtual_employee`/`roadclaw` 来源，不再只识别 `codex-tui`。

## 0.4.0 - 2026-08-05

- 切换为 WaveTerm 前台原生输入，禁止默认后台 `codex exec resume` 抢占交互。
- 增加 session/block/PTY 唯一定位、JWT 临时认证、稳定提示检测和固定 `继续\r` payload。
- 移除 watcher 直接写 `/dev/pts` 的状态行；失败只记录结构化事件，保留人工交互。
- 增加前台适配器幂等、退避、滚屏读取和迁移门禁。

## 0.3.5 - 2026-08-05

- 补充前台优先、后台兜底的运行策略与迁移文档（后续由 0.4.0 正式切换）。

## 0.3.4 - 2026-08-05

- 曾尝试向原 Codex TUI 终端写入醒目状态行；0.4.0 已移除该行为，避免破坏交互。
- 新增 `events.jsonl` 持久记录降级、续跑、排队和放弃事件。
- watcher 重启改为 `KillMode=process`，降低重启时误杀正在续跑任务的风险。

## 0.3.3 - 2026-08-05

- watcher 读取 `turn_context`，容量失败时按 `sol → terra → luna → 5.5` 自动降级重试，最低档再按 10 分钟 3 次放弃。
- 续跑时显式携带当前 reasoning effort，避免降级后思考档位被本机默认值拉低。
- 代理补充结构性 SSE 事件识别，避免过早把 `output_item.added`/`content_part.added` 当成可输出内容。
- HTTP 200 的容量错误 JSON 也会被识别并进入后备模型。

## 0.3.2 - 2026-08-05

- 同一会话的并发失败只保留最新事件，避免重复发送“继续”并过早触发失败熔断。

## 0.3.1 - 2026-08-05

- 启动时解析已有会话的 `session_meta`，修复 watcher 启动后无法接管旧会话后续失败的问题。
- 兼容真实 Codex session JSONL 的顶层 `session_meta` 事件格式。

## 0.3.0 - 2026-08-05

- 容量降级链扩展为 `gpt-5.6-sol → gpt-5.6-terra → gpt-5.6-luna → gpt-5.5`。
- 全链仍容量不足时由 watcher 自动发送 `继续`；10 分钟内连续失败 3 次后放弃。

## 0.2.0 - 2026-08-05

- 已输出后的容量错误记录模型冷却，下一次新请求自动优先后备模型。
- 增加 Codex TUI 失败自动续跑 watcher。
- 非手动终止失败自动发送 `继续`，10 分钟内连续失败 3 次后放弃。
- 增加 systemd 模板、单元测试和运行状态说明。

## 0.1.0 - 2026-07-28

- 建立 Sol/Terra/Luna 容量降级代理基线。
