# 模型降级路由管理台（admin_ui）

`model_fallback_proxy` 的本地配置前端。纯静态三件套（`index.html` / `style.css` / `app.js`），
无框架、无构建、无 CDN，由后端直接挂在 `/_fallback/admin/` 下提供。

用途：

- 选择哪些异常触发重试/降级（HTTP 状态码 + 响应正文关键词类别）。
- 编辑模型降级 DAG（谁是首选、失败后退到谁、谁是最后兜底）。
- 逐个节点配置重试次数、间隔、退避倍数、首字节超时、渠道冷却。
- 只读试运行：按当前配置算出一次请求会依次尝试哪些模型、等多久。
- 观察运行状态：请求/降级/容量计数、渠道冷却、供应商健康灯、最近 50 条降级事件。

## 打开方式

代理服务启动后直接访问：

```
http://<proxy-host>:<port>/_fallback/admin/
```

生产启用 DAG 模式需要在服务环境中设置：

```bash
CODEX_ROUTER_ENABLE=1
CODEX_ROUTER_CONFIG=/home/mini/model_fallback_proxy_standalone/configs/router.json
```

启用后让 Codex 的 OpenAI-compatible `base_url` 指向 `http://127.0.0.1:4477`，再重启
`codex-model-fallback-proxy.service`。Ark 等 `bearer` 供应商只配置密钥环境变量名，密钥值留在
服务环境文件，不写入仓库。

后端静态托管实现见 `router_engine.py` 的 `handleAdminIndex` / `handleAdminAsset`；
访问 `/` 与 `/` 之外的不存在路径都返回 404，只有 `admin_ui/` 下真实存在的文件会被返回。

本地纯前端调试（不连后端，只能看空状态与报错提示）：

```bash
cd src/model_fallback_proxy/admin_ui && python3 -m http.server 8080
# 浏览器打开 http://127.0.0.1:8080/ ，顶部会提示「无法读取后端配置」
```

## 接口契约

前端只调用四个接口，不读任何本地文件、不读密钥。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/_fallback/admin/config` | 返回 `{"config": {...}, "version": "<VERSION 文件内容>"}` |
| PUT | `/_fallback/admin/config` | body 为 config 对象（或 `{"config": ...}`）；成功 `{"ok":true}`，校验失败 400 + `{"ok":false,"error":"中文原因"}` |
| POST | `/_fallback/admin/health_check` | 逐供应商探测 `base_url + /v1/models`，返回 `{"results":[{"provider","ok","latency_ms","detail","checked_at"}]}` |
| GET | `/_fallback/status` | 运行态快照，见下 |

`/_fallback/status` 前端用到的字段：

```json
{
  "ok": true,
  "model_chain": ["glm-5.3"],
  "cooldowns_seconds": {"sub2api:qwen3.8-flash": 172.3},
  "provider_cooldowns_seconds": {"sub2api": 60},
  "provider_health": {"sub2api": {"ok": false, "reason": "connection refused", "checked_at": 1731500000}},
  "request_count": 1284,
  "capacity_count": 17,
  "fallback_count": 23,
  "retry_count": 41,
  "events": [{"ts": 1731500000, "from": "deepseek-flash", "to": "qwen3.8-flash", "kind": "fallback:capacity", "reason": ""}]
}
```

说明：

- `cooldowns_seconds` 的 key 是 `provider:model`（节点级冷却），`provider_cooldowns_seconds` 是供应商级熔断，两者分别展示。
- `ts` / `checked_at` 兼容秒与毫秒时间戳（`> 1e11` 视为毫秒）。
- 状态每 10 秒自动轮询；「刷新状态」按钮手动立即拉一次。

## 方向语义（重要）

配置字段是 `router[model].children = [按优先级排列的降级目标]`，即 **children = 失败后退到谁**。
一个节点可有多个 children，所以整体是 DAG，不是严格树。

页面画法（与用户口径一致）：

| 位置 | 含义 |
|---|---|
| 越靠上 | 越优先：叶子节点，请求最先尝试（通常是免费的公司渠道） |
| 越靠下 | 越兜底：根节点（sink，children 为空），最后才用（通常是自付费渠道，如 `glm-5.3`） |
| 箭头/连线 | 降级方向自上而下，从 children 指出去 |

分层算法是「到 sink 的最长距离」：`depthToSink` 中 sink = 0，数值越大越靠上。
所以同一个节点如果既在浅路径又在深路径上，会按最坏情况放到更靠上的层，避免连线交叉误读。

卡片上的标注：

- `降级去向 →` = 该节点 children 的顺序列表。
- `被谁降级进来 ←` = 反向邻接（哪些节点的 children 里有它）。
- `汇聚点 · N 条入边` = 被多条路径指向，运行时只展开一次。
- `根 · 最后兜底` = children 为空的 sink。
- `第 N 层` = 当前分层序号（顶层为 1）。

连线用 SVG 贝塞尔曲线绘制（`requestAnimationFrame` 在布局稳定后画，窗口 resize 重画），
不使用字符画、不使用空格对齐。

## 配置 schema

```jsonc
{
  "policy_version": 1,
  "defaults": {                      // 节点级字段留空时继承这里
    "retries": 1,
    "retry_interval_ms": 500,
    "backoff_multiplier": 2.0,
    "max_retry_interval_ms": 5000,
    "connect_timeout_s": 15,
    "first_byte_timeout_s": 60.0,
    "cooldown_s": 180
  },
  "retry_on": {
    "http_status": [408, 429, 500, 502, 503, 529],
    "keywords": {
      "capacity": true,             // 容量/过载
      "network_error": true,       // 网络错误
      "stream_break": true,        // 流中断
      "model_not_found": true,     // 模型不存在
      "auth_error": false,         // 确定性错误，默认关
      "corrupted_history": false    // 确定性错误，默认关
    }
  },
  "providers": {
    "sub2api": {
      "base_url": "https://sub2api.minieye.tech",
      "auth_mode": "passthrough",  // passthrough | bearer
      "api_key_env": "",           // 只填变量名，永不填密钥
      "enabled": true,
      "provider_failure_threshold": 3
    }
  },
  "router": {
    "deepseek-flash": {
      "provider": "sub2api",
      "children": ["qwen3.8-flash", "glm-5.3-flash"],
      "entry": true,                // 标记为首选入口（可选）
      "remote_model": "deepseek-v3", // 可选：发给上游时替换 body.model，留空=同名
      "retries": 2,                   // 以下 6 项留空 = 继承 defaults
      "retry_interval_ms": 300,
      "backoff_multiplier": 2,
      "max_retry_interval_ms": 4000,
      "first_byte_timeout_s": 30,
      "cooldown_s": 120
    }
  },
  "special_rules": [
    {
      "name": "context_overflow",
      "patterns": ["400001", "read body failed", "context_length_exceeded", "maximum context length"],
      "actions": ["switch_next", "signal_compact"],
      "cooldown": false,
      "enabled": true
    }
  ]
}
```

### special_rules（特殊规则）

按「错误文案小写包含即命中」判定，命中后**优先于** `retry_on.http_status` / `retry_on.keywords` 判定。
一条规则可含多个 `patterns`，任一命中即算命中；`actions` 可多选，取值集合由后端固定：

| action | 含义 |
|---|---|
| `switch_next` | 降级：按 `children` 顺序换到下一个节点 |
| `retry_same` | 只在本节点按 `retries` 重试，不换模型 |
| `cooldown_node` | 失败后把该 `provider:model` 冷却 `cooldown_s` 秒 |
| `signal_compact` | 标记该会话应压缩上下文（超长报错的正解） |
| `signal_new_session` | 标记历史已损坏，建议开新会话 |

`cooldown`（UI 上的「命中后冷却节点」）与 `actions` 里的 `cooldown_node` 是两处独立开关，
默认都不开：换模型 / 压缩上下文才是超长、历史损坏这类确定性错误的解法，冷却没有意义。
前端保存前会拦非法 action、空 name、空 patterns、重名规则。

状态事件里的 `kind` 可能是 `special:<规则名>`（如 `special:context_overflow`），页面原样展示。

### 不变式（前端保存前本地校验，与后端 `validateConfig` 对齐）

1. 每个节点的 `provider` 必须存在于 `providers` 中。
2. `children` 必须构成 DAG：禁止环、禁止自引用、禁止引用不存在的节点、禁止重复子节点。
3. `remote_model` 只能是非空字符串或省略；写成 `""` 直接拒绝（空串会被上游当成模型名）。
4. 节点策略字段留空 = 继承 `defaults`，UI 输入框 placeholder 显示「继承(当前值)」。
5. 供应商 `auth_mode=bearer` 时必须填 `api_key_env` 变量名；`base_url` 必须以 `http(s)://` 开头。

保存流程：本地校验 → 变更 diff 摘要（含 entry / remote_model / 6 项参数 / 状态码 / 关键词 / 供应商字段 / enabled）
→ `window.confirm` 二次确认（附带非阻断 warnings：路径超过 6 层、没有首选模型、供应商已停用、重试等待偏长）
→ PUT → toast。后端 400 的中文 `error` 会原样展示在 banner 与 toast 里。

### 确定性错误提醒

`auth_error`（401/403 密钥失效）与 `corrupted_history`（历史密文 hydration 失败）默认关闭，
UI 上标「确定性错误」并注明「重试无意义，会浪费额度」。换渠道通常也救不回来，只在确认是转发层问题时才开。

## 功能点与需求对照

| 用户需求 | 页面（hash 路由） | 实现要点 |
|---|---|---|
| 选择对哪些异常重试 | `#/retry` 重试策略 | 9 个状态码快捷标签（429/502/503/529/408/400/401/403/404）+ 自定义增删；6 个关键词类别开关，每项带一句话说明；全局 defaults 7 项 |
| 模型降级顺序做成 DAG，叶子=最高级、根=最后兜底 | `#/dag` 降级链路 | 按到 sink 的最长距离分层渲染，SVG 曲线连边，卡片标「降级去向 →」/「被谁降级进来 ←」/「汇聚点」/「根 · 最后兜底」；图例明示上=首选、下=兜底 |
| 每个节点配置重试次数/逻辑/间隔 | `#/dag` 节点卡片参数区 | 6 项参数（retries / retry_interval_ms / backoff_multiplier / max_retry_interval_ms / first_byte_timeout_s / cooldown_s），留空继承 defaults |
| 按错误文案做特殊处理 | `#/rules` 特殊规则 | 规则增删/改名/启停、patterns 标签增删、actions 多选（5 种固定取值）、cooldown 开关；非法 action 与空 patterns 前端拦截 |
| 失败自动切换供应商/模型是否真的生效 | `#/dryrun` 试运行 + `#/overview` 概览 | 试运行按节点参数算出完整尝试序列（考虑 `enabled=false`、`provider:model` 冷却、DAG 汇聚点去重）；概览展示真实降级事件、冷却剩余秒、供应商健康灯 |

页面清单（左侧固定导航，单页 hash 路由，不同屏堆叠）：

| 路由 | 页面 | 一句话说明 |
|---|---|---|
| `#/overview` | 概览 | 运行状态、供应商健康与最近的降级事件 |
| `#/dag` | 降级链路 | 编辑模型降级 DAG：上方首选，下方最后兜底 |
| `#/retry` | 重试策略 | 选择哪些异常触发重试，并设置全局默认参数 |
| `#/providers` | 供应商 | 维护各渠道的地址、鉴权方式与启用状态 |
| `#/rules` | 特殊规则 | 按错误文案命中，优先于普通状态码/关键词判定 |
| `#/dryrun` | 试运行 | 按当前配置推演一次请求会经历的全部尝试 |

## 已知限制

- 试运行的冷却判断依赖 `/_fallback/status` 的 `cooldowns_seconds`，key 约定为 `provider:model`；供应商级熔断（`provider_cooldowns_seconds`）只展示，不参与试运行跳过判断。
- 试运行是纯前端推算，不等价于真实请求：不模拟上游随机失败、不考虑首字节超时实际发生时刻，只按「节点失败即走下一个」的确定性路径展开。
- 页面同源托管在 `/_fallback/admin/` 下，不提供鉴权；生产环境需要靠反向代理或内网访问控制保护该路径。
- 本环境 `google-chrome --headless=new` 会 abort（exit 21），截图自检只能用旧 `--headless`。
- 编辑的是前端内存中的配置，必须点「保存配置」才落盘；有未保存改动时刷新/关闭页面会触发浏览器确认。

## 自检记录

- `node --check app.js` 通过；四个文件均无字符画边框（`boxchars = 0`）。
- 静态服务自检：`cd src/model_fallback_proxy/admin_ui && python3 -m http.server 8977` 后
  `curl` 三个资源均 200。
- 分页渲染（headless Chrome `--dump-dom`，mock 后端 `mock_server.py`）：`#/rules` 渲染 2 张规则卡片、
  7 个 pattern 标签（4 + 3）、每卡 5 个 action 复选框，`#/retry` 7 个 defaults 输入，
  `#/providers` 2 张供应商卡片。
- 截图（每页一张，headless Chrome `--headless --screenshot`，
  目录 `/home/mini/tmp/outputs/admin_ui_selfcheck/2026-11-13/`）：
  `overview.png` / `dag.png` / `retry.png` / `providers.png` / `rules.png` / `dryrun.png`。
- 交互自检（CDP 驱动 headless Chrome，`cdp_paged2.py`）：
  - `#/rules`：删 1 条 → 2→1 张；点「新增规则」→ 1→2 张；新增 pattern 立即出现在标签里；
    勾选 actions → 选中数 3。保存后 PUT body 的 `special_rules` 与 `defaults.retries=4` 与页面上编辑一致，
    响应 200，toast「配置已保存并生效」。
  - `#/dryrun`：`deepseek-flash` 入口 → 「15 次尝试，18 行，预计等待 36 s」，含汇聚点去重与冷却跳过行。
  - 旧单页版已验证并沿用同一套校验：环 / 不存在节点 / 自引用在本地拦截，不发 PUT；
    保存链路为 diff → `window.confirm` → 单次 PUT。
- 注意：`systemctl --user` 里正在跑的 `codex-model-fallback-proxy`（127.0.0.1:4477）是旧版本，`/_fallback/admin/` 会穿透到上游网关。需要部署本次版本后该路径才会返回本页面。
