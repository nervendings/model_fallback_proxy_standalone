"""继续工程师 · 多上游 DAG 路由引擎。

职责：
- 读取 configs/router.json（供应商 + 降级 DAG + 重试策略），支持 mtime 热加载与 admin API 保存。
- 按 DAG 计算候选序列：请求模型（叶子/首选）→ 深度优先按 children 顺序向下兜底（根=最后兜底供应商）。
- 异常分类（HTTP 状态 / 网络错误 / 流中断 / 错误文案）决定是否同节点重试或降级到下一节点。
- 节点级冷却 + 供应商级连续失败熔断；到期半开自动恢复。
- SSE 事件解析复用 model_fallback_proxy 既有实现，避免双份漂移。
"""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

from .error_classification import (
    getRetryableModelFailureKind,
    isCapacityText,
    isCorruptedHistoryError,
    normalizeErrorText,
)
from .model_fallback_proxy import (
    CLIENT_SESSION_KEY,
    buildAttemptPayload,
    encodeJsonBody,
    filterRequestHeaders,
    filterResponseHeaders,
    inspectSseResponse,
    isSseResponse,
    parseJsonBody,
)

REPO_CONFIG_PATH = Path(__file__).resolve().parents[2] / "configs" / "router.json"
CONFIG_PATH = Path(os.getenv("CODEX_ROUTER_CONFIG", str(REPO_CONFIG_PATH)))
DEFAULT_CONNECT_TIMEOUT_S = 15.0
EVENTS_LIMIT = 200

# 关键词类别 -> 判定函数（retry_on.keywords 的键名与前端契约固定）
KEYWORD_CHECKS = {
    "capacity": lambda text: isCapacityText(text) or getRetryableModelFailureKind(text) != "",
    "corrupted_history": isCorruptedHistoryError,
    "model_not_found": lambda text: bool(
        re.search(
            # 覆盖各家真实文案：model not found / model_not_found / model does not exist /
            # "is not supported by any configured account"（sub2api 实际返回）/ no available account
            r"model[_ ]?(not|does not)[ _]?(exist|found|supported)"
            r"|invalid model"
            r"|not supported by any configured"
            r"|no available (account|channel)",
            text,
        )
    ),
    "auth_error": lambda text: bool(
        re.search(r"invalid api key|authentication|unauthorized|permission", text)
    ),
}


def isPatternKey(name: str) -> bool:
    """节点名含 * 或 ? 视为通配键；单独的 * 是全局兜底，不算入口通配。"""
    return "*" in name or "?" in name


def globMatch(pattern: str, name: str) -> bool:
    """只支持 * 与 ?，其余字符按字面匹配；大小写敏感。"""
    if not pattern or not name:
        return False
    parts = []
    for ch in pattern:
        if ch == "*":
            parts.append(".*")
        elif ch == "?":
            parts.append(".")
        else:
            parts.append(re.escape(ch))
    return re.match("^" + "".join(parts) + "$", name) is not None


def defaultConfig() -> dict[str, Any]:
    """兜底配置：等价旧单上游 + 线性链行为，保证缺文件时服务可用。"""
    return {
        "policy_version": 1,
        "defaults": {
            "retries": 1,
            "retry_interval_ms": 500,
            "backoff_multiplier": 2.0,
            "max_retry_interval_ms": 5000,
            "connect_timeout_s": DEFAULT_CONNECT_TIMEOUT_S,
            "first_byte_timeout_s": 60.0,
            "cooldown_s": 180,
        },
        "retry_on": {
            "http_status": [408, 429, 500, 502, 503, 529],
            "keywords": {
                "capacity": True,
                "network_error": True,
                "stream_break": True,
                "model_not_found": True,
                "auth_error": False,
                "corrupted_history": False,
            },
        },
        "providers": {
            "sub2api": {
                "base_url": "https://sub2api.minieye.tech",
                "auth_mode": "passthrough",
                "api_key_env": "",
                "enabled": True,
                "provider_failure_threshold": 3,
            },
        },
        "router": {
            "deepseek-flash": {"provider": "sub2api", "children": []},
            "qwen3.8-flash": {"provider": "sub2api", "children": []},
        },
    }


DEFAULT_SPECIAL_RULES: list[dict[str, Any]] = [
    {
        "name": "context_overflow",
        "patterns": ["400001", "read body failed", "context_length_exceeded",
                     "maximum context length", "range of input length"],
        "retry": False, "cooldown": False, "switch_next": True, "signal": "compact",
        "enabled": True,
    },
    {
        "name": "corrupted_history",
        "patterns": ["could not be decrypted or parsed",
                     "invalid content type: encrypted_content",
                     "encrypted content hydration", "hydration failed"],
        "retry": False, "cooldown": False, "switch_next": False, "signal": "new_session",
        "enabled": True,
    },
]


def _parseSpecialRules(rawRules: Any) -> list[dict[str, Any]]:
    """解析 signal/actions 两种 schema，内部统一为 retry/cooldown/switch_next/signal。"""
    if not isinstance(rawRules, list):
        return [dict(rule) for rule in DEFAULT_SPECIAL_RULES]
    rules: list[dict[str, Any]] = []
    for item in rawRules:
        if not isinstance(item, dict) or not item.get("patterns"):
            continue
        actions = {str(action) for action in item.get("actions", [])}
        signal = str(item.get("signal") or "")
        if "signal_compact" in actions:
            signal = "compact"
        elif "signal_new_session" in actions:
            signal = "new_session"
        switch_next = bool(item.get("switch_next", "switch_next" in actions))
        if "actions" not in item:
            switch_next = bool(item.get("switch_next", True))
        rules.append({
            "name": str(item.get("name") or "rule"),
            "patterns": [str(pat).casefold() for pat in item["patterns"] if str(pat).strip()],
            "retry": bool(item.get("retry", "retry_same" in actions)),
            "cooldown": bool(item.get("cooldown", "cooldown_node" in actions)),
            "switch_next": switch_next,
            "signal": signal,
            "enabled": bool(item.get("enabled", True)),
        })
    return rules or [dict(rule) for rule in DEFAULT_SPECIAL_RULES]


@dataclass
class NodePolicy:
    retries: int
    retry_interval_ms: int
    backoff_multiplier: float
    max_retry_interval_ms: int
    connect_timeout_s: float
    first_byte_timeout_s: float
    cooldown_s: int

    def retryDelaysS(self) -> list[float]:
        """同节点重试的等待秒数列表（长度 = retries）。"""
        delays: list[float] = []
        current = self.retry_interval_ms / 1000.0
        for _ in range(max(0, self.retries)):
            delays.append(current)
            current = min(current * self.backoff_multiplier, self.max_retry_interval_ms / 1000.0)
        return delays


@dataclass
class Candidate:
    model: str  # 客户端语义的模型名（展示/冷却键）
    provider: str
    remote_model: str  # 实际发给上游的 model 字段
    policy: NodePolicy
    path: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.model}"


class RouterConfig:
    def __init__(self, raw: dict[str, Any]) -> None:
        self.raw = raw
        self.defaults = {**defaultConfig()["defaults"], **(raw.get("defaults") or {})}
        retryOn = raw.get("retry_on") or {}
        self.retry_http_status = {
            int(x) for x in retryOn.get("http_status", [408, 429, 500, 502, 503, 529])
        }
        self.retry_keywords = {
            **defaultConfig()["retry_on"]["keywords"],
            **(retryOn.get("keywords") or {}),
        }
        self.providers = raw.get("providers") or {}
        self.router = raw.get("router") or {}
        self.specialRules = _parseSpecialRules(raw.get("special_rules"))

    def matchSpecialRule(self, text: str) -> dict[str, Any] | None:
        normalized = normalizeErrorText(text)
        if not normalized:
            return None
        for rule in self.specialRules:
            if not rule.get("enabled", True):
                continue
            if any(pat in normalized for pat in rule["patterns"]):
                return rule
        return None
    def nodePolicy(self, model: str) -> NodePolicy:
        node = self.router.get(model) or {}
        # 管理台使用平铺字段；旧模板也可能使用 policy 子对象。两种写法都支持，平铺字段优先。
        overrides = dict(node.get("policy") or {})
        for key in self.defaults:
            if key in node:
                overrides[key] = node[key]
        merged = {**self.defaults, **overrides}
        return NodePolicy(
            retries=int(merged["retries"]),
            retry_interval_ms=int(merged["retry_interval_ms"]),
            backoff_multiplier=float(merged["backoff_multiplier"]),
            max_retry_interval_ms=int(merged["max_retry_interval_ms"]),
            connect_timeout_s=float(merged["connect_timeout_s"]),
            first_byte_timeout_s=float(merged["first_byte_timeout_s"]),
            cooldown_s=int(merged["cooldown_s"]),
        )

    def catchAllKey(self) -> str:
        """返回全局兜底节点名（约定为 *），未配置时返回空串。"""
        for key in self.router:
            if key.strip() == "*":
                return key
        return ""

    def patternKeys(self) -> list[str]:
        """可作为入口匹配的通配键，排除全局兜底 * 本身。"""
        return [k for k in self.router if k.strip() != "*" and isPatternKey(k)]

    def matchPattern(self, model: str) -> str:
        """按「字面量最多 → 模式最长」挑最具体的通配键；无命中返回空串。"""
        best = ""
        bestScore = (-1, -1)
        for key in self.patternKeys():
            if not globMatch(key, model):
                continue
            literal = len(key.replace("*", "").replace("?", ""))
            score = (literal, len(key))
            if score > bestScore:
                best, bestScore = key, score
        return best

    def isRetryableStatus(self, status: int) -> bool:
        return status in self.retry_http_status

    def isRetryableText(self, text: str) -> str:
        """返回命中的关键词类别名；空串表示不命中。"""
        normalized = normalizeErrorText(text)
        if not normalized:
            return ""
        for kind, enabled in self.retry_keywords.items():
            if not enabled:
                continue
            if kind in {"network_error", "stream_break"}:
                continue  # 这两类由传输层事件判定，不走文案
            check = KEYWORD_CHECKS.get(kind)
            if check and check(normalized):
                return kind
        return ""


def validateConfig(raw: Any) -> str:
    """返回中文错误信息；空串表示通过。"""
    if not isinstance(raw, dict):
        return "配置必须是 JSON 对象"
    providers = raw.get("providers")
    router = raw.get("router")
    if not isinstance(providers, dict) or not providers:
        return "providers 缺失或为空"
    if not isinstance(router, dict) or not router:
        return "router 缺失或为空"
    for name, provider in providers.items():
        if not isinstance(provider, dict):
            return f"供应商 {name} 配置必须是对象"
        base = str(provider.get("base_url") or "")
        if not base.startswith(("https://", "http://")):
            return f"供应商 {name} 的 base_url 非法"
        if str(provider.get("auth_mode") or "passthrough") not in {"passthrough", "bearer"}:
            return f"供应商 {name} 的 auth_mode 仅支持 passthrough/bearer"
    for name, node in router.items():
        if not isinstance(node, dict):
            return f"节点 {name} 配置必须是对象"
        provider = str(node.get("provider") or "")
        if provider not in providers:
            return f"节点 {name} 引用了不存在的供应商 {provider or '(空)'}"
        children = node.get("children")
        if not isinstance(children, list):
            return f"节点 {name} 的 children 必须是数组"
        if name in children:
            return f"节点 {name} 不能把自己作为降级目标"
        for child in children:
            if child not in router:
                return f"节点 {name} 的降级目标 {child} 不在 router 中"
            if isPatternKey(child):
                return f"通配节点 {child} 只能被模型名匹配命中，不能作为降级目标"
        remote = node.get("remote_model")
        if "remote_model" in node and (
            not isinstance(remote, str) or (remote and not remote.strip())
        ):
            return f"节点 {name} 的 remote_model 必须是非空字符串或省略"
    rawRules = raw.get("special_rules")
    if rawRules is not None:
        if not isinstance(rawRules, list):
            return "special_rules 必须是数组"
        allowedSignals = {"", "compact", "new_session", "switch_model"}
        allowedActions = {
            "switch_next", "retry_same", "cooldown_node",
            "signal_compact", "signal_new_session",
        }
        for item in rawRules:
            if not isinstance(item, dict):
                return "special_rules 每项必须是对象"
            pats = item.get("patterns")
            if not isinstance(pats, list) or not pats:
                return "special_rules 每项必须含非空 patterns 数组"
            signal = str(item.get("signal") or "")
            if signal not in allowedSignals:
                return "special_rules 含未知 signal: " + signal
            actions = item.get("actions", [])
            if not isinstance(actions, list) or any(str(action) not in allowedActions for action in actions):
                return "special_rules 含未知 actions"
    color: dict[str, int] = {}

    def visit(node_name: str) -> bool:
        mark = color.get(node_name, 0)
        if mark == 1:
            return False
        if mark == 2:
            return True
        color[node_name] = 1
        for child in router[node_name].get("children", []):
            if not visit(child):
                return False
        color[node_name] = 2
        return True

    for node_name in router:
        if not visit(node_name):
            return "降级关系存在环，请调整 children 使其构成 DAG（叶子=首选，根=兜底）"
    return ""


class RouterState:
    """运行态：配置 + 节点冷却 + 供应商熔断 + 事件队列 + 热加载。"""

    def __init__(self, config: RouterConfig) -> None:
        self.config = config
        self.node_blocked_until: dict[str, float] = {}
        self.provider_blocked_until: dict[str, float] = {}
        self.provider_consecutive_fail: dict[str, int] = {}
        self.provider_health: dict[str, dict[str, Any]] = {}
        self.events: collections.deque[dict[str, Any]] = collections.deque(maxlen=EVENTS_LIMIT)
        self.request_count = 0
        self.fallback_count = 0
        self.capacity_count = 0
        self.retry_count = 0
        self.model_chain: list[str] = []
        self.signals: dict[str, int] = {}
        self._mtime = None
        self._lock = threading.Lock()

    @classmethod
    def load(cls) -> "RouterState | None":
        if not CONFIG_PATH.exists():
            return None
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logging.error("router config invalid: %s", exc)
            return None
        error = validateConfig(raw)
        if error:
            logging.error("router config rejected: %s", error)
            return None
        state = cls(RouterConfig(raw))
        try:
            stat = CONFIG_PATH.stat()
            state._mtime = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            pass
        return state

    def reloadIfChanged(self) -> bool:
        """配置文件 mtime 变化时热加载；失败保留旧配置继续服务。"""
        try:
            stat = CONFIG_PATH.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            return False
        if stamp == self._mtime:
            return False
        fresh = RouterState.load()
        if fresh is None:
            self._mtime = stamp
            return False
        with self._lock:
            fresh.node_blocked_until = self.node_blocked_until
            fresh.provider_blocked_until = self.provider_blocked_until
            fresh.provider_health = self.provider_health
            fresh.events = self.events
            fresh.signals = self.signals
            fresh.request_count = self.request_count
            fresh.fallback_count = self.fallback_count
            fresh.capacity_count = self.capacity_count
            fresh.retry_count = self.retry_count
            self.config = fresh.config
            self._mtime = stamp
        logging.info("router config reloaded from %s", CONFIG_PATH)
        return True

    def saveConfig(self, raw: dict[str, Any]) -> str:
        """校验并原子落盘，成功后热应用。返回中文错误；空串=成功。"""
        error = validateConfig(raw)
        if error:
            return error
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(CONFIG_PATH)
        self._mtime = None
        self.reloadIfChanged()
        return ""

    def providersMeta(self, provider: str) -> dict[str, Any]:
        return self.config.providers.get(provider) or {}

    def markNodeFailure(self, candidate: Candidate, kind: str) -> None:
        cooldown = candidate.policy.cooldown_s
        self.node_blocked_until[candidate.key] = time.monotonic() + cooldown
        if kind == "capacity":
            self.capacity_count += 1
        self._bumpProviderFailure(candidate.provider, kind, cooldown)

    def markHealthy(self, provider: str, model_key: str) -> None:
        self.node_blocked_until.pop(model_key, None)
        self.provider_consecutive_fail[provider] = 0
        self.provider_health[provider] = {
            "ok": True,
            "reason": "",
            "checked_at": int(time.time()),
        }

    def _bumpProviderFailure(self, provider: str, kind: str, cooldown: int) -> None:
        threshold = int(self.providersMeta(provider).get("provider_failure_threshold", 3))
        fails = self.provider_consecutive_fail.get(provider, 0) + 1
        self.provider_consecutive_fail[provider] = fails
        self.provider_health[provider] = {
            "ok": False,
            "reason": kind,
            "checked_at": int(time.time()),
        }
        providerCooldown = int(self.providersMeta(provider).get("provider_cooldown_s", 0) or 0)
        if threshold > 0 and fails >= threshold:
            self.provider_blocked_until[provider] = time.monotonic() + max(
                providerCooldown or cooldown, 30
            )
            self.provider_consecutive_fail[provider] = 0

    def nodeAvailable(self, provider: str, model_key: str) -> bool:
        now = time.monotonic()
        if self.providersMeta(provider).get("enabled", True) is False:
            return False
        if self.provider_blocked_until.get(provider, 0.0) > now:
            return False
        return self.node_blocked_until.get(model_key, 0.0) <= now

    def buildCandidates(self, requested_model: str) -> list[Candidate]:
        """DAG 展开：请求模型优先，向下按 children 兜底，最后接全局兜底节点 *。

        入口解析顺序：精确节点名 → 最具体的通配键（gpt-*）→ 默认供应商直连。
        全局兜底节点 * 追加到任意链尾；若链上已有同一供应商+同一远端模型的节点，
        不重复追加，避免同一次请求对同一上游打两遍。
        """
        router = self.config.router
        visited: set[str] = set()
        candidates: list[Candidate] = []

        def append(name: str, path: list[str], modelLabel: str | None = None,
                   policySource: str | None = None) -> None:
            node = router[name]
            candidates.append(
                Candidate(
                    model=modelLabel or name,
                    provider=str(node["provider"]),
                    remote_model=str(node.get("remote_model") or (modelLabel or name)),
                    policy=self.config.nodePolicy(policySource or name),
                    path=path,
                )
            )

        def walk(model: str, path: list[str]) -> None:
            if model in visited:
                return
            visited.add(model)
            append(model, path + [model])
            for child in router[model].get("children", []):
                walk(child, path + [model])

        if requested_model and requested_model in router:
            walk(requested_model, [])
        elif requested_model:
            pattern = self.config.matchPattern(requested_model)
            if pattern:
                visited.add(pattern)
                append(pattern, [pattern], modelLabel=requested_model, policySource=pattern)
                for child in router[pattern].get("children", []):
                    walk(child, [pattern])
            else:
                defaultProvider = str(os.getenv("CODEX_ROUTER_DEFAULT_PROVIDER", "sub2api"))
                if requested_model and defaultProvider in self.config.providers:
                    candidates.append(
                        Candidate(
                            model=requested_model,
                            provider=defaultProvider,
                            remote_model=requested_model,
                            policy=self.config.nodePolicy(requested_model),
                            path=[requested_model],
                        )
                    )

        catchAll = self.config.catchAllKey()
        if catchAll and catchAll not in visited:
            node = router[catchAll]
            signature = (str(node["provider"]), str(node.get("remote_model") or catchAll))
            already = any((c.provider, c.remote_model) == signature for c in candidates)
            if not already or not candidates:
                walk(catchAll, [])

        return candidates

    def recordEvent(self, fromKey: str, toKey: str, kind: str, reason: str) -> None:
        self.events.appendleft(
            {
                "ts": time.strftime("%H:%M:%S"),
                "from": fromKey,
                "to": toKey,
                "kind": kind,
                "reason": (reason or "")[:120],
            }
        )

    def noteSignal(self, signal: str, nodeKey: str) -> None:
        """记录特殊规则要求的会话级动作信号（供自动压缩/新会话联动）。"""
        if signal:
            self.signals[signal] = self.signals.get(signal, 0) + 1
            logging.info("special-rule signal=%s node=%s", signal, nodeKey)

    def getStatus(self) -> dict[str, Any]:
        now = time.monotonic()
        return {
            "ok": True,
            "mode": "dag",
            "policy_version": self.config.raw.get("policy_version"),
            "catch_all": self.config.catchAllKey(),
            "model_chain": self.model_chain,
            "cooldowns_seconds": {
                key: max(0, round(deadline - now))
                for key, deadline in self.node_blocked_until.items()
                if deadline > now
            },
            "provider_cooldowns_seconds": {
                key: max(0, round(deadline - now))
                for key, deadline in self.provider_blocked_until.items()
                if deadline > now
            },
            "provider_health": self.provider_health,
            "request_count": self.request_count,
            "capacity_count": self.capacity_count,
            "fallback_count": self.fallback_count,
            "retry_count": self.retry_count,
            "signals": dict(self.signals),
            "events": list(self.events)[:50],
        }


class _StreamStateAdapter:
    """把 RouterState 适配成 model_fallback_proxy 流式工具所需的 mark* 接口。"""

    def __init__(self, state: RouterState, candidate: Candidate,
                 onStarted=None, onBreak=None) -> None:
        self._state = state
        self._candidate = candidate
        self.onStreamStarted = onStarted
        self.onStreamBreak = onBreak

    def markCapacity(self, model_name: str) -> None:
        self._state.markNodeFailure(self._candidate, "capacity")

    def markHealthy(self, model_name: str) -> None:
        self._state.markHealthy(self._candidate.provider, self._candidate.key)


def buildAuthHeaders(baseHeaders: dict[str, str], provider: dict[str, Any]) -> dict[str, str]:
    """passthrough 原样透传客户端鉴权；bearer 用 api_key_env 指向的环境变量替换。"""
    headers = dict(baseHeaders)
    if str(provider.get("auth_mode") or "passthrough") == "passthrough":
        return headers
    api_key_env = str(provider.get("api_key_env") or "").strip()
    apiKey = os.environ.get(api_key_env, "").strip() if api_key_env else ""
    if not apiKey:
        raise RuntimeError("missing_env_key:" + (api_key_env or "(empty)"))
    for name in list(headers):
        if name.lower() == "authorization":
            del headers[name]
    headers["Authorization"] = "Bearer " + apiKey
    return headers


def buildUpstreamUrl(provider: dict[str, Any], rawPath: str, rawQuery: str = "") -> str:
    """拼接上游 URL，兼容 base_url 自带路径前缀与 /v1 重复的渠道。

    - provider.path_strip_prefix：去掉入站路径前缀，如 ark 的 /api/v3 只认
      /chat/completions、/responses，而客户端发的是 /v1/chat/completions。
    - base_url 已带 /v1 时，自动收敛相邻重复的 /v1，避免 /v1/v1/responses 404。
    """
    base = str(provider.get("base_url", "")).rstrip("/")
    path = rawPath or "/"
    strip = str(provider.get("path_strip_prefix") or "").rstrip("/")
    if strip and (path == strip or path.startswith(strip + "/")):
        path = path[len(strip):] or "/"
    target = base + path
    target = re.sub(r"(?<=/v1)/v1(?=/)", "", target)
    if rawQuery:
        target += "?" + rawQuery
    return target


async def attemptUpstream(
    session: aiohttp.ClientSession,
    request: web.Request,
    provider: dict[str, Any],
    headers: dict[str, str],
    body: bytes,
    policy: NodePolicy,
) -> aiohttp.ClientResponse:
    target = buildUpstreamUrl(
        provider, request.rel_url.raw_path, request.rel_url.raw_query_string
    )
    timeout = aiohttp.ClientTimeout(
        total=None,
        connect=policy.connect_timeout_s,
        sock_connect=policy.connect_timeout_s,
        sock_read=policy.first_byte_timeout_s,
    )
    return await session.request(
        request.method,
        target,
        headers=headers,
        data=body,
        allow_redirects=False,
        timeout=timeout,
    )


def jsonError(
    status: int, kind: str, message: str, headers: dict[str, str] | None = None
) -> web.Response:
    return web.json_response(
        {"error": {"type": kind, "message": message}},
        status=status,
        headers=headers or None,
    )


async def finishStreamWithError(downstream: Any, exc: BaseException) -> web.StreamResponse:
    """响应头已发出后上游断开：向已准备流补一条 SSE error 事件并收尾。

    此时候选链无法重放本回合（避免重复工具调用），返回已准备好的 downstream。
    """
    message = "已输出部分响应后上游断开，本回合不可安全重放，请重试该操作。"
    body = encodeJsonBody({
        "error": {"type": "stream_break_after_output", "message": message},
    }).decode("utf-8")
    if downstream is not None:
        try:
            await downstream.write(
                ("event: error" + chr(10) + "data: " + body + chr(10) + chr(10)).encode("utf-8")
            )
            await downstream.write_eof()
        except Exception:  # noqa: BLE001 - 客户端可能已断开，收尾失败不再上抛
            logging.warning("stream break finalize failed: %s", type(exc).__name__)
        return downstream
    return web.Response(
        status=502,
        body=encodeJsonBody({"error": {"type": "stream_break_after_output", "message": message}}),
        headers={"X-Fallback-Outcome": "stream_break_after_output"},
    )


async def evaluateUpstreamResponse(
    request: web.Request,
    state: RouterState,
    upstream: aiohttp.ClientResponse,
    candidate: Candidate,
    streamStarted: dict[str, bool],
) -> tuple[str, Any]:
    """返回 (kind, result)。kind: ok / retryable / terminal。"""
    def _onStarted(_model, downstream=None):
        streamStarted["v"] = True
        if downstream is not None:
            streamStarted["resp"] = downstream

    async def _onBreak(exc, downstream, _model):
        streamStarted["v"] = True
        streamStarted["broken"] = True
        streamStarted["resp"] = downstream
        await finishStreamWithError(downstream, exc)
        return downstream

    adapter = _StreamStateAdapter(state, candidate, onStarted=_onStarted, onBreak=_onBreak)
    try:
        if isSseResponse(upstream.headers):
            action, result = await inspectSseResponse(request, upstream, adapter, candidate.model)
            if streamStarted.get("broken"):
                return ("responded", result)
            if action == "stream":
                return ("ok", result)
            body = result if isinstance(result, bytes) else b""
            rule = state.config.matchSpecialRule(normalizeErrorText(body))
            if rule:
                return ("special", {"rule": rule, "status": upstream.status, "body": body})
            hit = state.config.isRetryableText(normalizeErrorText(body)) or (
                "capacity" if action == "capacity" else ""
            )
            if hit:
                return ("retryable", hit)
            return ("terminal", web.Response(
                status=upstream.status,
                reason=upstream.reason,
                headers=filterResponseHeaders(upstream.headers),
                body=body,
            ))
        responseBody = await upstream.read()
    except (asyncio.TimeoutError, aiohttp.ClientError) as exc:
        if streamStarted.get("v"):
            return ("responded", await finishStreamWithError(streamStarted.get("resp"), exc))
        return ("retryable", "stream_break:" + type(exc).__name__)
    if upstream.status < 400:
        return ("ok", web.Response(
            status=upstream.status,
            reason=upstream.reason,
            headers=filterResponseHeaders(upstream.headers),
            body=responseBody,
        ))
    # 非流响应只在 HTTP 错误状态下命中特殊规则；成功响应中的 request id 可能偶然含 400001。
    rule = state.config.matchSpecialRule(normalizeErrorText(responseBody))
    if rule:
        return ("special", {"rule": rule, "status": upstream.status, "body": responseBody})
    hitKind = ""
    if state.config.isRetryableStatus(upstream.status):
        hitKind = "http_" + str(upstream.status)
    else:
        textHit = state.config.isRetryableText(normalizeErrorText(responseBody))
        if textHit:
            hitKind = textHit
    if hitKind:
        return ("retryable", (upstream.status, responseBody))
    return ("terminal", web.Response(
        status=upstream.status,
        reason=upstream.reason,
        headers=filterResponseHeaders(upstream.headers),
        body=responseBody,
    ))


async def handleRoutedRequest(request: web.Request) -> web.StreamResponse:
    state: RouterState = request.app[ROUTER_STATE_KEY]
    session: aiohttp.ClientSession = request.app[CLIENT_SESSION_KEY]
    state.reloadIfChanged()
    state.request_count += 1

    rawBody = await request.read()
    payload = parseJsonBody(rawBody)
    requestedModel = str(payload.get("model") or "") if payload else ""
    candidates = state.buildCandidates(requestedModel)
    if not candidates:
        # 未纳管模型：走默认供应商直连，保持旧行为，不阻断会话
        defaultProvider = str(os.getenv("CODEX_ROUTER_DEFAULT_PROVIDER", "sub2api"))
        if defaultProvider not in state.config.providers:
            return jsonError(400, "unknown_model",
                             "模型 " + (requestedModel or "(空)") + " 不在路由表且无默认供应商")
        candidates = [Candidate(
            model=requestedModel,
            provider=defaultProvider,
            remote_model=requestedModel,
            policy=state.config.nodePolicy(requestedModel),
        )]

    baseHeaders = filterRequestHeaders(request.headers)
    exhaustedResponse: web.Response | None = None
    lastBody = b""

    for index, candidate in enumerate(candidates):
        provider = state.config.providers.get(candidate.provider) or {}
        hasFallbackLeft = index + 1 < len(candidates)
        if not state.nodeAvailable(candidate.provider, candidate.key):
            if hasFallbackLeft:
                state.recordEvent(requestedModel, candidate.key, "cooldown_skip", "冷却/熔断中，直接降级")
                continue
            if exhaustedResponse is None:
                return jsonError(503, "all_models_cooling",
                                 "所有候选均在冷却中，请稍候或在管理界面调整。")
            break
        attemptPayload = buildAttemptPayload(
            payload or {"model": candidate.remote_model}, candidate.remote_model
        )
        attemptBody = encodeJsonBody(attemptPayload)
        delays = candidate.policy.retryDelaysS()
        for attempt in range(len(delays) + 1):
            outcome: tuple[str, Any]
            streamStarted = {"v": False, "broken": False, "resp": None}
            try:
                headers = buildAuthHeaders(baseHeaders, provider)
                upstream = await attemptUpstream(session, request, provider, headers, attemptBody, candidate.policy)
            except asyncio.TimeoutError:
                outcome = ("retryable", "network_timeout")
            except aiohttp.ClientError as exc:
                outcome = ("retryable", "network_error:" + type(exc).__name__)
            except RuntimeError as exc:
                return jsonError(502, "provider_config_error",
                                 "供应商 " + candidate.provider + " 配置错误: " + str(exc))
            else:
                outcome = await evaluateUpstreamResponse(
                    request, state, upstream, candidate, streamStarted
                )

            kind, result = outcome
            if kind == "responded":
                return result  # type: ignore[return-value]
            if kind == "special":
                rule = result["rule"]
                nextKey = candidates[index + 1].key if hasFallbackLeft else "(chain_end)"
                state.recordEvent(
                    candidate.key,
                    nextKey,
                    "special:" + rule["name"],
                    "signal=" + (rule["signal"] or "-"),
                )
                state.noteSignal(rule["signal"], candidate.key)
                if rule["cooldown"]:
                    state.markNodeFailure(candidate, "special:" + rule["name"])
                if rule["retry"] and attempt < len(delays):
                    state.retry_count += 1
                    state.recordEvent(
                        candidate.key,
                        candidate.key,
                        "retry:special:" + rule["name"],
                        "特殊规则要求同节点重试",
                    )
                    await asyncio.sleep(delays[attempt])
                    continue
                specialResponse = web.Response(
                    status=result["status"] or 502,
                    headers={
                        "X-Fallback-Signal": rule["signal"] or "switch_model",
                        "X-Fallback-Rule": rule["name"],
                        "X-Fallback-Exhausted": "1",
                    },
                    body=result["body"],
                )
                if rule["switch_next"] and hasFallbackLeft:
                    state.fallback_count += 1
                    break
                return specialResponse
            if kind == "ok":
                if isinstance(result, web.StreamResponse):
                    result.headers["X-Served-By"] = candidate.key
                return result  # type: ignore[return-value]
            if kind == "terminal":
                return result  # type: ignore[return-value]
            detail = result if isinstance(result, str) else "http_" + str(result[0])
            if isinstance(result, tuple):
                lastBody = result[1]
                exhaustedResponse = web.Response(
                    status=result[0],
                    headers={"X-Fallback-Exhausted": "1"},
                    body=result[1],
                )
            if attempt < len(delays):
                state.retry_count += 1
                state.recordEvent(candidate.key, candidate.key, "retry:" + detail,
                                  "第 " + str(attempt + 1) + "/" + str(len(delays)) + " 次重试")
                await asyncio.sleep(delays[attempt])
                continue
            state.markNodeFailure(candidate, detail)
            nextKey = candidates[index + 1].key if hasFallbackLeft else "(chain_end)"
            state.recordEvent(candidate.key, nextKey, "fallback:" + detail, "")
            state.fallback_count += 1
            break

    if exhaustedResponse is not None:
        return exhaustedResponse
    if lastBody:
        return web.Response(status=502, headers={"X-Fallback-Exhausted": "1"}, body=lastBody)
    return jsonError(503, "fallback_exhausted", "降级链全部失败，请稍后重试或在管理界面调整策略。",
                     headers={"X-Fallback-Exhausted": "1"})


# ---- admin API ----

async def handleAdminConfigGet(request: web.Request) -> web.Response:
    state: RouterState = request.app[ROUTER_STATE_KEY]
    state.reloadIfChanged()
    return web.json_response({"config": state.config.raw, "version": readVersion()})


async def handleAdminConfigPut(request: web.Request) -> web.Response:
    state: RouterState = request.app[ROUTER_STATE_KEY]
    raw = await request.read()
    try:
        parsed = json.loads(raw)
    except ValueError:
        return web.json_response({"ok": False, "error": "body 不是合法 JSON"}, status=400)
    config = parsed.get("config") if isinstance(parsed, dict) and "config" in parsed else parsed
    if not isinstance(config, dict):
        return web.json_response({"ok": False, "error": "config 必须是对象"}, status=400)
    error = state.saveConfig(config)
    if error:
        return web.json_response({"ok": False, "error": error}, status=400)
    logging.info("router config updated via admin api")
    return web.json_response({"ok": True})


def defaultHealthPath(base: str) -> str:
    """按 base_url 推断模型列表路径：已含 /v1 用 /models，否则 /v1/models。"""
    return "/models" if base.rstrip("/").endswith("/v1") else "/v1/models"


def defaultModelsPath(base: str) -> str:
    """按 base_url 推断模型清单路径（各家 /models 位置不同）。"""
    trimmed = base.rstrip("/")
    if trimmed.endswith("/v1") or trimmed.endswith("/v3") or trimmed.endswith("/coding/v3"):
        return "/models"
    return "/v1/models"


def extractModelIds(payload: Any) -> list[str]:
    """从 /models 响应里提取模型 ID，兼容 OpenAI、方舟和常见兼容层。"""
    items: Any = None
    if isinstance(payload, dict):
        for key in ("data", "models", "items"):
            if key in payload:
                items = payload[key]
                break
        if isinstance(items, dict):
            items = items.get("models") or items.get("data") or items.get("items")
    elif isinstance(payload, list):
        items = payload
    found: list[str] = []
    if not isinstance(items, list):
        return found
    for item in items:
        if isinstance(item, str):
            value = item
        elif isinstance(item, dict):
            value = str(item.get("id") or item.get("model") or item.get("name") or "")
        else:
            value = ""
        value = value.strip()
        if value and value not in found:
            found.append(value)
    found.sort()
    return found


def readCodexModelSlugs() -> list[str]:
    """读取本机 Codex 模型清单（默认 ~/.codex/models.json）里的 slug。"""
    override = os.environ.get("CODEX_MODELS_JSON", "").strip()
    path = Path(override) if override else Path.home() / ".codex" / "models.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    raw = payload.get("models") if isinstance(payload, dict) else payload
    if not isinstance(raw, list):
        return []
    slugs: list[str] = []
    for item in raw:
        if isinstance(item, str):
            slug = item
        elif isinstance(item, dict):
            slug = str(item.get("slug") or item.get("id") or "")
        else:
            slug = ""
        slug = slug.strip()
        if slug and slug not in slugs:
            slugs.append(slug)
    return slugs


async def handleAdminModelList(request: web.Request) -> web.Response:
    """抓取模型清单并记录到配置 model_catalog。

    body: {"provider": "sub2api"} 抓取某个供应商的 /models；
          {"source": "codex"} 读取本机 Codex 模型清单。
    只保存模型名，不保存任何密钥。
    """
    state: RouterState = request.app[ROUTER_STATE_KEY]
    session: aiohttp.ClientSession = request.app[CLIENT_SESSION_KEY]
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - 空 body / 非 JSON 一律当默认
        body = {}
    if not isinstance(body, dict):
        body = {}
    updated_at = int(time.time())

    if str(body.get("source") or "") == "codex":
        models = readCodexModelSlugs()
        if not models:
            return web.json_response(
                {"ok": False, "error": "未找到 Codex 模型清单 ~/.codex/models.json"}, status=404
            )
        catalog = dict(state.config.raw.get("model_catalog") or {})
        catalog["__codex__"] = {"models": models, "updated_at": updated_at, "source": "codex"}
        state.config.raw["model_catalog"] = catalog
        error = state.saveConfig(state.config.raw)
        if error:
            return web.json_response({"ok": False, "error": error}, status=400)
        return web.json_response(
            {"ok": True, "source": "codex", "models": models,
             "count": len(models), "updated_at": updated_at}
        )

    provider_name = str(body.get("provider") or "").strip()
    provider = state.config.providers.get(provider_name)
    if not isinstance(provider, dict):
        return web.json_response(
            {"ok": False, "error": "未知供应商：" + (provider_name or "(空)")}, status=400
        )
    base = str(provider.get("base_url") or "").rstrip("/")
    path = str(provider.get("models_path") or provider.get("health_path") or defaultModelsPath(base))
    headers = buildHealthHeaders(provider, request.headers.get("Authorization", ""))
    timeout = aiohttp.ClientTimeout(total=20, connect=5, sock_connect=5, sock_read=15)
    try:
        async with session.get(base + path, headers=headers, timeout=timeout) as resp:
            text_value = await resp.text()
            if resp.status >= 400:
                return web.json_response(
                    {"ok": False, "error": "HTTP " + str(resp.status) + " " + path}, status=502
                )
            try:
                payload = json.loads(text_value)
            except ValueError:
                return web.json_response(
                    {"ok": False, "error": "模型清单不是合法 JSON"}, status=502
                )
    except Exception as exc:  # noqa: BLE001 - 抓取失败要回显原因
        return web.json_response(
            {"ok": False, "error": type(exc).__name__ + ": " + str(exc)[:200]}, status=502
        )

    models = extractModelIds(payload)
    if not models:
        return web.json_response(
            {"ok": False, "error": "模型清单为空（检查 models_path 与鉴权）"}, status=502
        )
    catalog = dict(state.config.raw.get("model_catalog") or {})
    catalog[provider_name] = {
        "models": models,
        "updated_at": updated_at,
        "source": base + path,
    }
    state.config.raw["model_catalog"] = catalog
    error = state.saveConfig(state.config.raw)
    if error:
        return web.json_response({"ok": False, "error": error}, status=400)
    return web.json_response(
        {"ok": True, "provider": provider_name, "models": models,
         "count": len(models), "updated_at": updated_at}
    )


def buildHealthHeaders(provider: dict[str, Any], inbound_auth: str = "") -> dict[str, str]:
    """体检鉴权：bearer 读 api_key_env，passthrough 透传客户端 Authorization。

    2026-11-16：原来完全不带头，需要 key 的渠道一律 401，管理台四个渠道全红、
    全是假故障，健康灯失去意义。
    """
    headers = {"Accept": "application/json"}
    if str(provider.get("auth_mode") or "passthrough") == "bearer":
        key_env = str(provider.get("api_key_env") or "").strip()
        api_key = os.environ.get(key_env, "").strip() if key_env else ""
        if api_key:
            headers["Authorization"] = "Bearer " + api_key
        return headers
    if inbound_auth:
        headers["Authorization"] = inbound_auth
        return headers
    # passthrough 渠道（把客户端 key 原样转发）自身没有固定凭据，管理台体检
    # 拿不到客户端头时，用 health_api_key_env 指定的本机 key 自证渠道可用。
    # 不配就保持无鉴权探测，401 会如实报警（不掩盖问题）。
    key_env = str(provider.get("health_api_key_env") or "").strip()
    api_key = os.environ.get(key_env, "").strip() if key_env else ""
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    return headers


async def handleAdminHealthCheck(request: web.Request) -> web.Response:
    """逐供应商探测可用性，结果同步写入运行态 provider_health。

    2026-11-16 修正（管理台四个渠道全红、全是假故障）：
      1. 原代码写死 base_url + /v1/models。方舟官方是 /api/v3/models，拼出来是
         /api/v3/v1/models 必然 404 —— 红灯是探测写错，不是渠道坏。改为 providers
         可配 health_path，缺省按 base_url 是否已含 /v1 推断。
      2. 原代码不带任何鉴权，需要 key 的渠道一律 401，健康灯永远红。改为按
         auth_mode 带凭据（bearer 读 api_key_env，passthrough 透传客户端头）。
      3. 404 与 401 必须分开：前者是探测路径配错（ok=None），后者才是渠道真实
         不可用。ok=None 不得写进熔断用健康态，否则会误熔断整条链路。
    """
    state: RouterState = request.app[ROUTER_STATE_KEY]
    session: aiohttp.ClientSession = request.app[CLIENT_SESSION_KEY]
    inbound_auth = request.headers.get("Authorization", "")
    results: list[dict[str, Any]] = []
    for name, provider in state.config.providers.items():
        started = time.monotonic()
        entry: dict[str, Any] = {"provider": name, "ok": False, "latency_ms": None, "detail": ""}
        try:
            base = str(provider.get("base_url", "")).rstrip("/")
            path = str(provider.get("health_path") or defaultHealthPath(base))
            headers = buildHealthHeaders(provider, inbound_auth)
            timeout = aiohttp.ClientTimeout(total=10, connect=5, sock_connect=5, sock_read=8)
            async with session.get(base + path, headers=headers, timeout=timeout) as resp:
                entry["latency_ms"] = int((time.monotonic() - started) * 1000)
                entry["detail"] = "HTTP " + str(resp.status) + " " + path
                if resp.status in (401, 403):
                    # 凭据不可用：渠道确实不可用，红灯是真实结论。
                    entry["ok"] = False
                elif resp.status == 404:
                    # 探测路径不存在：说明 health_path 配错，不能据此判定渠道故障。
                    entry["ok"] = None
                    entry["detail"] = "探测路径不存在（配 health_path）: " + path
                else:
                    entry["ok"] = resp.status < 400
        except Exception as exc:  # noqa: BLE001 - 体检必须吞掉一切异常并展示
            entry["latency_ms"] = int((time.monotonic() - started) * 1000)
            entry["detail"] = type(exc).__name__
        entry["checked_at"] = int(time.time())
        if entry["ok"] is None:
            # 探测路径错误：只更新原因，不覆盖健康判定，避免误熔断整条链路。
            state.provider_health[name] = {
                "ok": state.provider_health.get(name, {}).get("ok"),
                "reason": entry["detail"],
                "checked_at": entry["checked_at"],
            }
        else:
            state.provider_health[name] = {
                "ok": entry["ok"],
                "reason": "" if entry["ok"] else entry["detail"],
                "checked_at": entry["checked_at"],
            }
        results.append(entry)
    return web.json_response({"results": results})


def readVersion() -> str:
    try:
        return (Path(__file__).resolve().parents[2] / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"


def createAppKey(name: str, value_type: type) -> Any:
    app_key = getattr(web, "AppKey", None)
    return app_key(name, value_type) if app_key is not None else name


ROUTER_STATE_KEY = createAppKey("router_state", RouterState)


async def handleAdminResetCooldowns(request: web.Request) -> web.Response:
    state: RouterState = request.app[ROUTER_STATE_KEY]
    state.node_blocked_until.clear()
    state.provider_blocked_until.clear()
    state.provider_consecutive_fail.clear()
    state.recordEvent("(manual)", "(all)", "reset", "手动重置冷却与熔断")
    return web.json_response({"ok": True})


ADMIN_UI_DIR = Path(__file__).resolve().parent / "admin_ui"
# 管理台前端随版本更新，禁止浏览器启发式缓存，避免改了看不到
ADMIN_UI_NO_CACHE_HEADERS = {"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"}
_ADMIN_ASSET_REF_RE = re.compile(r'((?:href|src)=")(style\.css|app\.js)(?:\?v=[^"]*)?"')


def versionAdminHtml(html: str) -> str:
    """给 index.html 的静态资源加版本号，避免浏览器继续用旧 app.js。"""
    version = readVersion()

    def _repl(match: "re.Match[str]") -> str:
        return '%s%s?v=%s"' % (match.group(1), match.group(2), version)

    return _ADMIN_ASSET_REF_RE.sub(_repl, html)



async def handleAdminIndex(request: web.Request) -> web.Response:
    index = ADMIN_UI_DIR / "index.html"
    if not index.exists():
        return web.json_response(
            {"ok": False, "error": "admin_ui 未部署，请检查 " + str(ADMIN_UI_DIR)},
            status=404,
        )
    html = versionAdminHtml(index.read_text(encoding="utf-8"))
    return web.Response(
        text=html,
        content_type="text/html",
        headers=ADMIN_UI_NO_CACHE_HEADERS,
    )


async def handleAdminAsset(request: web.Request) -> web.Response:
    name = request.match_info.get("name", "")
    asset = (ADMIN_UI_DIR / name).resolve()
    if not str(asset).startswith(str(ADMIN_UI_DIR.resolve())) or not asset.is_file():
        return web.Response(status=404, text="not found")
    suffixTypes = {".js": "application/javascript", ".css": "text/css", ".svg": "image/svg+xml"}
    return web.Response(
        text=asset.read_text(encoding="utf-8"),
        content_type=suffixTypes.get(asset.suffix, "text/plain"),
        headers=ADMIN_UI_NO_CACHE_HEADERS,
    )


async def createRoutedSession(app: web.Application) -> None:
    app[CLIENT_SESSION_KEY] = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=None, connect=30, sock_connect=30, sock_read=360),
        auto_decompress=True,
        read_bufsize=1024 * 1024,
    )


async def closeRoutedSession(app: web.Application) -> None:
    await app[CLIENT_SESSION_KEY].close()


def loadRouterState() -> RouterState:
    state = RouterState.load()
    if state is None:
        logging.warning("router config missing/invalid at %s, using defaults", CONFIG_PATH)
        state = RouterState(RouterConfig(defaultConfig()))
    # 兼容旧监控字段：model_chain = 根节点（无 children 的兜底模型）
    state.model_chain = [
        name for name, node in state.config.router.items() if not node.get("children")
    ]
    return state


def createRoutedApp(state: RouterState | None = None) -> web.Application:
    app = web.Application(client_max_size=512 * 1024 * 1024)
    app[ROUTER_STATE_KEY] = state or loadRouterState()
    app.on_startup.append(createRoutedSession)
    app.on_cleanup.append(closeRoutedSession)
    app.router.add_get("/_fallback/health", handleHealthCompat)
    app.router.add_get("/_fallback/status", handleStatus)
    app.router.add_get("/_fallback/admin/config", handleAdminConfigGet)
    app.router.add_put("/_fallback/admin/config", handleAdminConfigPut)
    app.router.add_post("/_fallback/admin/health_check", handleAdminHealthCheck)
    app.router.add_post("/_fallback/admin/reset_cooldowns", handleAdminResetCooldowns)
    app.router.add_post("/_fallback/admin/models", handleAdminModelList)
    app.router.add_get("/_fallback/admin/", handleAdminIndex)
    app.router.add_get("/_fallback/admin/{name}", handleAdminAsset)
    app.router.add_route("*", "/{tail:.*}", handleRoutedRequest)
    return app


async def handleHealthCompat(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def handleStatus(request: web.Request) -> web.Response:
    state: RouterState = request.app[ROUTER_STATE_KEY]
    state.reloadIfChanged()
    return web.json_response(state.getStatus())
