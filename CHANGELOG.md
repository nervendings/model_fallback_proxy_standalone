# 变更记录

## 0.7.0 - 2026-11-16

### 新增

- 路由支持通配：节点名可用 `*` / `?`（如 `gpt-*`）匹配模型名，按「字面量最多 → 模式最长」选最具体的一个。
- 新增全局兜底节点 `*`：无论请求的是已登记模型、还是清单外的新模型，链尾都会追加它；
  链上已有同供应商+同远端模型时自动去重，不会对同一上游打两遍。
  未登记模型现在会先按 `CODEX_ROUTER_DEFAULT_PROVIDER` 直连一次，失败后落到 `*`。
- 管理台 DAG 页新增通配规则条：显示「全局兜底 * → 远端模型 · 供应商」与各通配入口；
  未配置 `*` 时显示黄色提示。
- 新增节点弹窗支持勾选「设为全局兜底 *」，选中的模型作为远端模型。
- 试运行页自动把 `*` 计入尝试序列（与后端一致的去重规则）。
- 状态接口新增 `catch_all` 字段。

### 测试

- 新增 5 个用例：已知链追加 `*`、清单外模型落到 `*`、通配入口命中、同上游去重、通配键不得作为降级目标。


## 0.6.11 - 2026-11-16

### 新增

- 保存配置前的风险提示补两条：路由表不足 2 个节点时明确提示「等于没有降级链」；
  存在没有上游入口的孤立节点时，列出节点名并说明只有直接指定才会用到。

### 说明

- 背景：有人在管理台把 DAG 删到只剩兜底根一个节点，等于关闭了全部降级；
  保存对话框原本只提示「没有首选模型」，这种情况不会触发。


## 0.6.10 - 2026-11-16

### 修复

- 管理台前端被浏览器缓存，改了看不到：`index.html` 静态资源加 `?v=<版本>`，
  服务端渲染时按 `VERSION` 自动注入；`/_fallback/admin/*` 返回 `Cache-Control: no-store`。
  之前旧 `app.js` 可能被继续使用，导致修复后的下拉样式不生效。

### 测试

- 新增 `versionAdminHtml` 单测：无版本号时注入、旧版本号被替换。


## 0.6.9 - 2026-11-16

### 修复

- DAG 页模型下拉在深色主题下白底浅字不可读：改为自绘下拉面板，支持关键词搜索、按供应商分组、键盘 Esc/Enter。
- 节点改名、「降级到」＋、upstream `remote_model`、新增节点弹窗全部改用同一套自绘下拉。
- `.picker-open` 提升卡片层级，弹层不再被相邻卡片遮挡。


## 0.6.8 - 2026-11-16

### 新增

- 管理台支持模型清单抓取：新增 `POST /_fallback/admin/models`，可按供应商抓取 `/models`，
  兼容 OpenAI、方舟 `/api/v3` 与 coding 通道；也可读取本机 `~/.codex/models.json`
  的 Codex 模型 slug。只记录模型名和抓取时间，不保存任何密钥。
- 模型记录写入 `config.model_catalog`，DAG 页顶部显示「已记录 N 个模型名」。
- 新增/改名节点改为下拉选择模型；供应商页每个渠道卡片附带「模型清单」行
  （数量、更新时间、一键抓取、展开模型名）。

### 优化

- DAG 页瘦身：节点参数默认折叠，摘要显示「参数 · 全部继承默认」或「参数 · 自定义 N 项」；
  卡片列宽收窄到 268px 起，首屏可见首选层与后续层，右侧操作按钮不再被裁切。
- 「降级到」下拉可直接添加下游节点；「来自」仅在节点有入边时渲染。
- 窄屏自动单列，无横向滚动。


## 0.6.7 - 2026-11-16

### 变更

- `deepseek-v4-1-flash-261017` 从「仅可选」改为**全链最后一站**：
  原三个根节点（glm-5-3 / zai-org 两个别名）的 children 改为指向它，
  该节点自身 children 置空，成为唯一 sink。
- `model_policy.json` 的 fallback 与 high/low 两档 fallback 末位同步改为该模型；
  auto-continue 的 model_chain 因此也以它为最后一站。
- admin 健康探测修正：支持逐渠道 `health_path`、按 auth_mode 带鉴权、
  404 与 401 分开报（ok=None 不写熔断态）。

### 修复

- 管理台 `/_fallback/admin/` 四个渠道全红：原代码写死 `base_url + /v1/models`
  且不带鉴权。方舟官方是 `/api/v3/models`（拼成 `/api/v3/v1/models` 必 404），
  其余渠道缺 key 一律 401。修正后四渠道全绿。

## 0.6.6 - 2026-11-16

### 新增

- 路由表新增 `deepseek-v4-1-flash-261017` 节点（火山方舟官方 `/api/v3`，按量计费）。
  本地 Codex 由此多一个可选的 DeepSeek V4.1 Flash：`codex -p ark41` 或 `ark41` 快捷入口。
- 新增 provider `ark-official`，与套餐通道 `ark`（`/api/coding/v3`）分开计价；
  鉴权读 `ARK_AI_API_KEY`，不落仓库。

### 说明

- 该节点 children 回落 `deepseek-flash` 链路：方舟抖动/限流时会话不中断，只降级模型。
- 未纳入 `CODEX_FALLBACK_MODEL_CHAIN`，不进自动降级链，避免策略档位被动变化。
## 0.6.5 - 2026-11-13

- 修「闸门放弃时不回填失败签名」：repeated_failure_gave_up 只写空 retryable kind，
  结果落到 other。体检侧无法区分失败类型 —— 实测 404 model_not_found 探针
  被报成「1 个会话历史密文损坏，需人工开新会话」，照提示开新会话纯属白费功夫。
  现在放弃时把真实 signature 写进 last_failure_kind。

## 0.6.4 - 2026-11-13

- 修「模型不存在」无限续跑：404 model_not_found 之前落 other 签名，不进确定性闸门，
  auto-continue 会一直 resume 下去（实测 no-such-model-probe 会话反复续跑）。
  现新增 error_classification.isModelNotFoundError 与 model_not_found 签名，
  并加入 DETERMINISTIC_FAILURE_SIGNATURES；同一签名 6 次/小时即停止并提示换模型。

## 0.6.3 - 2026-11-13

- 修 resume 丢 provider：会话换过 provider（如 qingyanx）后，后台 resume 只传 -m 模型名，
  codex 会尝试把该线程改回默认 provider 并直接失败（实测 01a1d561：Cannot change this thread model provider from qingyanx to OpenAI on resume）。
  现在从 session_meta / thread_settings_applied 记录 model_provider，resume 时以 -c model_provider= 带上；
  pending 队列同步透传，避免排队期间丢失 provider。

## 0.6.2 - 2026-11-13

- 新增 headless 定向兜底（--allow-headless-resume-fallback / CODEX_AUTO_CONTINUE_HEADLESS_FALLBACK）：
  codex exec、子代理、虚拟员工这类会话根本没有可注入终端，前台适配器永远返回 observe_only，
  在「后台兜底全局关闭」下失败即等于放弃 —— 实测近 2h 6 个失败会话里 5 个是这种。
  新开关只对 headless 来源 + hasLiveWriter 判活为假时后台 resume；用户 TUI 仍走前台注入。
- hasLiveWriter：扫 /proc/*/fd 找 rollout 持有者 + 扫 codex 进程 argv 找 thread_id，
  覆盖「TUI 持 SQLite writer 锁但不持有 rollout fd」的活法；空 session_id 一律判活（宁可不 resume）。
- 补两条回归：headless 无写入者时 resume；headless 有写入者时绝不动。

## 0.6.1 - 2026-11-13

- 修 GLM 兜底链断裂：glm-5-3-flash / glm-5-3 两个节点的 provider 由 ark 改为 qingyanx。
  实测 ark 的 CodingPlan 订阅已过期（InvalidSubscription），api/v3 上也未开通 GLM
  （ModelNotOpen），整条自购兜底是死的；改用青岩中转后两条均实测正常返回。
- 新增 qingyanx 供应商（https://api.qingyanx.com/v1，bearer + QINGYANX_API_KEY，
  失败阈值 2、冷却 300s）。因上游限流 8 req/min，GLM 节点保持 retries=0。
- 新增 zai-org/glm-5.3、zai-org/glm-5.3-flash 两个直通别名节点。
  models.json 里登记的就是这两个 slug，没有别名时 TUI /model 选中会落到 sub2api 报 404。
- 同步移除 ~/.codex/model_policy.json 里的 zai-org/glm-5.3* 自动降级项：
  它们只在 qingyanx provider 下可用，放进默认降级链会让降级变成必然 404。

## 0.6.0 - 2026-11-13

- 新增多供应商 DAG 路由：默认链路为 gpt-6-astra → deepseek-flash → qwen3.8-flash → Ark GLM，支持节点级重试、指数退避、节点冷却与供应商熔断。
- 新增可配置异常策略：HTTP 状态码、容量/网络/断流/模型不存在/鉴权/历史损坏关键词均可独立开关。
- 新增特殊规则：400001/上下文超限直接换模型并发出 compact 信号；历史密文损坏发出 new_session 信号，不做无意义重试。
- 新增本地管理台：概览、DAG、重试、供应商、特殊规则、试运行六个页面，配置保存后热加载。
- 新增本地假供应商故障注入测试，覆盖 429、网络拒绝、首字节超时、SSE 容量、流中断、特殊规则与 DAG 环校验。

## 0.5.16 - 2026-11-06

- **修闸门与签名漂移**（0.5.15 只修了一半）：
  `getFailureSignature` 已把 `Invalid content type: encrypted_content` 归为 `corrupted_history`，
  但 `error_classification.isCorruptedHistoryError` 的 `CORRUPTED_HISTORY_MARKERS` 没有同步，
  `handleFailure` 的「停止自动续跑」闸门因此不生效。

  实测代价：`01a17838`（qwen3.8-flash）在 12:48:47–12:49:48 连败 **5 次**，间隔越缩越短。
  0.5.15 的测试只覆盖签名函数，测不出这种两处判定的漂移。
- 补一致性测试：对同一批错误文本断言「签名是 corrupted_history」且「闸门返回 True」，
  并断言通用 400002 / 400001 不被误伤。
- 测试 94 → 96。


## 0.5.15 - 2026-11-06

- **修确定性闸门漏判**：`Invalid content type: encrypted_content. Supported types...`
  未被识别为历史密文损坏，被归为 `other`，导致闸门不生效。
  实测 `01a18eac`（goal 早已 complete）因此被无意义重试 **21 次**。
- `getFailureSignature` 的 corrupted_history 分支补齐三种变体：
  `could not be verified` / `hydration failed` / `could not be decrypted` /
  `invalid content type` + `encrypted_content` 组合。
- 测试 +2，全量 **94 passed**。

## 0.5.14 - 2026-11-06

- **放开终端注入白名单**：`ALLOWED_INPUT_PAYLOADS` 从「只允许 `继续\r`」扩为 4 个固定安全指令
  （`继续\r`、`/compact`、`/compact\r`、`\r`）。

  背景：被 TUI 占住 writer 的会话，daemon `thread/resume` 一律报 `already has an active writer`，
  唯一通道是往该 pty 发 `/compact`；而白名单把它挡死，导致这类会话只能等死。
  实测本会话 `01a1afdb` 登记压缩后 1 小时未执行，手动跑 service 也全是 `active writer`。
- 白名单仍**封闭**：任意文本（`rm -rf /`、管道命令等）一律拒绝，新增项需人工评审。
- 测试 +4（continue 放行 / compact 放行 / 任意文本拒绝 / 白名单规模上限），全量 **92 passed**。

## 0.5.13 - 2026-11-06

- **安全门修复**：`WaveTermForegroundAdapter.inject()` 旧实现完全绕过 `observe_only`，
  调用方以为在观察，实际已经把按键打进用户终端。现加显式早退，返回 `status=observe_only`。
  生产 `CODEX_AUTO_CONTINUE_FOREGROUND=1`（observe_only=False）不受影响。
- `inject()` 支持自定义 `payload`：被外部 TUI 占住 writer 的会话，daemon 压不了，
  唯一可行通道是往该 pty 发 `/compact`；此前 payload 硬编码 `继续
`，这类会话永远救不回来。
- 测试 +4（默认 payload、自定义 payload、observe_only 不发送、dry 通道），全量 **88 passed**。

## 0.5.12 - 2026-11-06

- 彻底隔离测试与生产状态：另有 8 处 `SessionAutoContinue(...)` 构造只传 `state_path`，
  其余路径落默认值，把 `session_id=00000000` 的测试事件持续写进生产 `events.jsonl`
  （实测累计 7 条，污染真实告警流水）。全部补齐 `events_path`（必要时补 `log_root`/`session_root`）。
- 新增守卫测试 `tests/test_no_production_pollution.py`：静态扫描所有测试里的
  `SessionAutoContinue(` 构造，缺 `events_path` 直接失败，防止以后回退。
- 验证：pytest 84 passed；跑全量测试前后生产 `events.jsonl` 大小 delta=0。

## 0.5.11 - 2026-11-06

- 新增「确定性故障闸门」：同一会话在同一小时内重复同一故障签名达到 6 次即停手并交人工。
  实测会话 `01a14e39`（rollout 399MB）每次请求都 `400001 read body failed`，watcher 每 60s 注入
  一次「继续」，累计 **162 次**仍不自愈，只烧 token 与磁盘。
- 新增 `getFailureSignature`：把失败归一为稳定签名（400001_body / http_502 / stream_disconnected /
  corrupted_history / capacity / context_window），不取 request id 与时间戳。
- **只对确定性签名生效**（`400001_body`、`corrupted_history`、`context_window`）；
  502/503/断流/容量这类瞬时故障保持原有「无上限重试」语义，不误伤。
- 测试 +2：确定性 400001 达上限停手；瞬时 502 超限仍继续重试。全量 **83 passed**。

## 项目名称

**继续工程师（JXG / Continue Engineer）**：容量不够自动换档，失败自动重试，前台自动续上。

## 0.5.10 - 2026-11-06

- 修复前台续跑「无限僵尸重试」：`prompt_not_ready` 等可重试原因此前只按 60s 间隔无限重试，
  实测会话 `01a17838` 连续重试 327 次（>5 小时），每次都写状态文件与日志，既救不回会话又放大
  磁盘/CPU 抖动。新增 `FOREGROUND_MAX_RETRY_COUNT = 30`，超限后置 `last_action=foreground_gave_up`、
  丢弃 pending 并 emit warning 提示人工接管。
- 测试 +1（`test_foreground_gives_up_after_max_retries`），全量 81 tests PASS。

## 0.5.9 - 2026-10-16

- 新增「会话历史密文损坏」熔断：服务端 hydration 失败（`encrypted content ... could not be verified`）
  属不可恢复故障，`handleFailure` 不再无差别重试（实测旧行为对 PM 会话重试 61 次），
  直接停止自动续跑、置 `last_action=corrupted_history` 并提示人工接管（开新会话 / 先 /compact）。
- `error_classification.py` 新增 `isCorruptedHistoryError`：只匹配 hydration 明确特征，
  不误伤通用 400002（input.type/tool.type）与容量类错误。
- 测试 +2：分类器单元测试（真/假阳性边界）+ 守护集成测试（不产生续跑命令、不排队、发 error 提示）；
  全量 75 tests PASS，仓库质量门禁 PASS。

## 0.5.8 - 2026-10-09

- 修复前台“继续”自 2026-09-30 起完全无法注入的问题（实测注入成功率归零、71 个任务积压）：
  - `wsh` 解析兼容 systemd 服务环境：优先目标终端 `WAVETERM_WSHBINDIR`、`~/.local/share/waveterm/bin/wsh`，不再依赖服务 PATH。
  - 就绪判定兼容新版 TUI 的 `› Ask Codex to do anything` 占位提示符；通过“运行态指示行”区分工作/空闲。
  - 会话→终端块定位新增 `/proc` 文件句柄兜底（shell snapshot 缺失时仍可定位）。
  - 回显截取窗口从 1600 提升到 4000 字符，避免忙碌标志行被长行截断后漏判。
- 新增“不影响正常对话”安全门：注入前检查会话尾部状态，仅 `awaiting`（失败待恢复）允许注入；
  `active`（回合进行中）/`completed`/`manual`/`unknown` 一律跳过；长回合中 `task_started`
  滑出扫描窗口时按文件写入时间兜底判为 `active`；忙碌终端、输入框草稿、审批界面不注入。
- 新增僵尸清理：等待注入超过 `CODEX_AUTO_CONTINUE_PENDING_MAX_AGE`（默认 6 小时）自动放弃；
  启动时清理重试 ≥1000 次的历史遗留项（本次清掉 71 条）。
- 修复 `buildCandidates` 忽略显式传入模型链的问题；同步对齐 09-19 换链后的测试断言（73 tests PASS）。

## 0.5.7 - 2026-08-16

- 统一错误归一化，终端换行或嵌套错误字段不会漏判。
- 远程 compact 遇到通用 502 Bad Gateway、429/503/529、Bad Gateway 或 Upstream service temporarily unavailable 时，自动进入换档与“继续”流程。
- 仅远程 compact 的可恢复上游故障进入换档；普通失败仍按既有逻辑直接续跑。

## 0.5.6 - 2026-08-13

- 识别远程 compact 的 `502 Bad Gateway: Upstream service temporarily unavailable` 错误。
- 该错误自动进入 `gpt-5.6` 换档和前台“继续”流程；其它模型保持原模型继续。

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
