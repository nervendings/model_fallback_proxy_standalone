#!/usr/bin/env python3
"""Codex 模型容量降级代理。"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web


DEFAULT_UPSTREAM_URL = ""
DEFAULT_LISTEN_HOST = "127.0.0.1"
DEFAULT_LISTEN_PORT = 4477
DEFAULT_MODEL_CHAIN = (
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
)
DEFAULT_COOLDOWN_SECONDS = 180
MAX_INSPECTION_BYTES = 1024 * 1024
HOP_BY_HOP_HEADERS = {
    "connection",
    "content-length",
    "content-encoding",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
CAPACITY_MARKERS = (
    "selected model is at capacity",
    "model is at capacity",
    "server_overloaded",
    "server overloaded",
    "no available accounts",
    "capacity exceeded",
    "capacity_exceeded",
)
TRANSIENT_MODEL_UNAVAILABLE_STATUSES = {429, 503, 529}
IGNORED_SSE_TYPES = {
    "ping",
    "response.created",
    "response.in_progress",
    "response.queued",
    "response.output_item.added",
    "response.content_part.added",
    "response.output_item.done",
    "response.content_part.done",
    "response.output_text.done",
    "response.completed",
}
MODEL_REASONING_EFFORT_REWRITES = {
    "gpt-5.6-luna": {"ultra": "max"},
}


def readModelChain() -> tuple[str, ...]:
    raw_value = os.getenv("CODEX_FALLBACK_MODEL_CHAIN", "")
    values = tuple(item.strip() for item in raw_value.split(",") if item.strip())
    return values or DEFAULT_MODEL_CHAIN


def readUpstreamUrl(explicit_url: str | None = None) -> str:
    upstream_url = str(explicit_url or os.getenv("CODEX_FALLBACK_UPSTREAM_URL", "")).strip()
    if not upstream_url:
        raise RuntimeError("CODEX_FALLBACK_UPSTREAM_URL is required")
    parsed = urlsplit(upstream_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        raise RuntimeError("CODEX_FALLBACK_UPSTREAM_URL must be an http(s) URL without embedded credentials")
    return upstream_url.rstrip("/")


def isCapacityText(raw_value: str | bytes) -> bool:
    if isinstance(raw_value, bytes):
        text_value = raw_value.decode("utf-8", "ignore").lower()
    else:
        text_value = str(raw_value).lower()
    return any(marker in text_value for marker in CAPACITY_MARKERS)


def getCapacityHeaderText(headers: Any) -> str:
    values: list[str] = []
    for name, value in headers.items():
        normalized_name = str(name).lower()
        if "error" in normalized_name or "status" in normalized_name or "model" in normalized_name:
            values.append(str(value))
    return " ".join(values)


def isCapacityHttpResponse(status_code: int, raw_body: bytes, headers: Any | None = None) -> bool:
    has_capacity_marker = isCapacityText(raw_body)
    if headers is not None:
        has_capacity_marker = has_capacity_marker or isCapacityText(getCapacityHeaderText(headers))
    if has_capacity_marker:
        if status_code >= 400:
            return True
        payload = parseJsonBody(raw_body)
        if not isinstance(payload, dict):
            return False
        if isinstance(payload.get("error"), (dict, str)):
            return True
        message = payload.get("message")
        if isinstance(message, str) and isCapacityText(message):
            return True
        codex_error_info = payload.get("codex_error_info")
        if isinstance(codex_error_info, str) and isCapacityText(codex_error_info):
            return True
        return False

    # 上游偶尔仅以 503/529 表示当前模型不可用，且不在正文暴露错误码。
    # 此时尚未向下游输出任何内容，可以安全尝试下一模型。
    return status_code in TRANSIENT_MODEL_UNAVAILABLE_STATUSES


def parseJsonBody(raw_body: bytes) -> dict[str, Any] | None:
    if not raw_body:
        return None
    try:
        value = json.loads(raw_body)
    except (TypeError, ValueError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None


def encodeJsonBody(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def buildAttemptPayload(payload: dict[str, Any], model_name: str) -> dict[str, Any]:
    attempt_payload = dict(payload)
    attempt_payload["model"] = model_name
    reasoning = payload.get("reasoning")
    if isinstance(reasoning, dict):
        attempt_reasoning = dict(reasoning)
        effort = str(attempt_reasoning.get("effort") or "")
        effort_rewrites = MODEL_REASONING_EFFORT_REWRITES.get(model_name, {})
        if effort in effort_rewrites:
            attempt_reasoning["effort"] = effort_rewrites[effort]
        attempt_payload["reasoning"] = attempt_reasoning
    return attempt_payload


def filterRequestHeaders(headers: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    for name, value in headers.items():
        if name.lower() in HOP_BY_HOP_HEADERS or name.lower() == "host":
            continue
        result[name] = value
    # 禁止上游压缩，便于只检查首个错误事件。
    result["Accept-Encoding"] = "identity"
    return result


def filterResponseHeaders(headers: Any) -> dict[str, str]:
    return {
        name: value
        for name, value in headers.items()
        if name.lower() not in HOP_BY_HOP_HEADERS
    }


def getSseType(raw_event: bytes) -> str:
    event_name = ""
    data_lines: list[str] = []
    for raw_line in raw_event.decode("utf-8", "ignore").splitlines():
        if raw_line.startswith("event:"):
            event_name = raw_line.partition(":")[2].strip()
        elif raw_line.startswith("data:"):
            data_lines.append(raw_line.partition(":")[2].lstrip())
    data_text = "\n".join(data_lines)
    if data_text:
        try:
            payload = json.loads(data_text)
        except (TypeError, ValueError):
            payload = None
        if isinstance(payload, dict):
            payload_type = str(payload.get("type") or "").strip()
            if payload_type:
                return payload_type
            response = payload.get("response")
            if isinstance(response, dict):
                response_status = str(response.get("status") or "").strip()
                if response_status:
                    return f"response.{response_status}"
            if isinstance(payload.get("error"), (dict, str)):
                return "error"
    return event_name


def isMeaningfulSseEvent(raw_event: bytes) -> bool:
    event_type = getSseType(raw_event)
    if not event_type:
        text_value = raw_event.decode("utf-8", "ignore").strip()
        return bool(text_value and not text_value.startswith(":"))
    return event_type not in IGNORED_SSE_TYPES


def isCapacitySseEvent(raw_event: bytes) -> bool:
    event_type = getSseType(raw_event)
    if event_type in {"error", "response.failed"}:
        return isCapacityText(raw_event)
    if event_type:
        return False
    # 部分中转会把失败 JSON 放进 text/event-stream，但不带 event/type 字段。
    # 只有同时出现容量标记和错误字段时才判定，避免把正常输出文本误判为失败。
    text_value = raw_event.decode("utf-8", "ignore").lower()
    return isCapacityText(text_value) and any(
        marker in text_value for marker in ("error", "codex_error_info", "server_overloaded")
    )


def isSseResponse(headers: Any) -> bool:
    content_type = str(headers.get("Content-Type") or "").lower()
    return "text/event-stream" in content_type


@dataclass
class ProxyState:
    model_chain: tuple[str, ...]
    cooldown_seconds: int
    blocked_until: dict[str, float] = field(default_factory=dict)
    request_count: int = 0
    fallback_count: int = 0
    capacity_count: int = 0

    def buildCandidates(self, requested_model: str) -> list[str]:
        if requested_model not in self.model_chain:
            return [requested_model]
        start_index = self.model_chain.index(requested_model)
        model_slice = list(self.model_chain[start_index:])
        current_time = time.monotonic()
        available = [
            model_name
            for model_name in model_slice
            if self.blocked_until.get(model_name, 0.0) <= current_time
        ]
        return available or [model_slice[-1]]

    def markCapacity(self, model_name: str) -> None:
        self.capacity_count += 1
        self.blocked_until[model_name] = time.monotonic() + self.cooldown_seconds

    def markHealthy(self, model_name: str) -> None:
        self.blocked_until.pop(model_name, None)

    def getStatus(self) -> dict[str, Any]:
        current_time = time.monotonic()
        cooldowns = {
            model_name: max(0, round(deadline - current_time))
            for model_name, deadline in self.blocked_until.items()
            if deadline > current_time
        }
        return {
            "ok": True,
            "model_chain": list(self.model_chain),
            "cooldowns_seconds": cooldowns,
            "request_count": self.request_count,
            "capacity_count": self.capacity_count,
            "fallback_count": self.fallback_count,
        }


def createAppKey(name: str, value_type: type) -> Any:
    app_key = getattr(web, "AppKey", None)
    return app_key(name, value_type) if app_key is not None else name


UPSTREAM_URL_KEY = createAppKey("upstream_url", str)
PROXY_STATE_KEY = createAppKey("proxy_state", ProxyState)
CLIENT_SESSION_KEY = createAppKey("client_session", aiohttp.ClientSession)


async def writeStreamResponse(
    request: web.Request,
    upstream: aiohttp.ClientResponse,
    initial_body: bytes,
    state: ProxyState,
    model_name: str,
) -> web.StreamResponse:
    downstream = web.StreamResponse(
        status=upstream.status,
        reason=upstream.reason,
        headers=filterResponseHeaders(upstream.headers),
    )
    await downstream.prepare(request)
    if initial_body:
        await downstream.write(initial_body)

    # 已开始向客户端转发时不能重放请求，避免重复工具调用；但仍识别后续容量错误，
    # 使下一次新请求跳过当前模型。
    event_buffer = bytearray()
    capacity_observed = False

    def consumeCompletedEvents() -> bool:
        nonlocal capacity_observed
        while True:
            lf_boundary = event_buffer.find(b"\n\n")
            crlf_boundary = event_buffer.find(b"\r\n\r\n")
            candidates = [index for index in (lf_boundary, crlf_boundary) if index >= 0]
            if not candidates:
                return capacity_observed
            boundary = min(candidates)
            separator_size = 4 if boundary == crlf_boundary else 2
            event = bytes(event_buffer[: boundary + separator_size])
            del event_buffer[: boundary + separator_size]
            if isCapacitySseEvent(event):
                state.markCapacity(model_name)
                logging.warning("capacity observed after output model=%s; skipping on next request", model_name)
                capacity_observed = True
                return True

    async for chunk in upstream.content.iter_any():
        if chunk:
            try:
                await downstream.write(chunk)
            except ConnectionResetError:
                logging.warning("downstream closed while streaming model=%s", model_name)
                return downstream
            if not capacity_observed:
                event_buffer.extend(chunk)
                consumeCompletedEvents()
                if len(event_buffer) > MAX_INSPECTION_BYTES:
                    event_buffer.clear()
    if not capacity_observed and event_buffer and isCapacitySseEvent(bytes(event_buffer)):
        state.markCapacity(model_name)
        logging.warning("capacity observed after output model=%s; skipping on next request", model_name)
        capacity_observed = True
    if not capacity_observed:
        state.markHealthy(model_name)
    try:
        await downstream.write_eof()
    except ConnectionResetError:
        logging.warning("downstream closed before eof model=%s", model_name)
    return downstream


async def inspectSseResponse(
    request: web.Request,
    upstream: aiohttp.ClientResponse,
    state: ProxyState,
    model_name: str,
) -> tuple[str, bytes | web.StreamResponse]:
    buffered = bytearray()
    current_event = bytearray()
    while True:
        raw_line = await upstream.content.readline()
        if not raw_line:
            if current_event:
                buffered.extend(current_event)
                if isCapacitySseEvent(current_event):
                    return "capacity", bytes(buffered)
            return "complete", bytes(buffered)
        current_event.extend(raw_line)
        if raw_line not in {b"\n", b"\r\n"}:
            if len(buffered) + len(current_event) >= MAX_INSPECTION_BYTES:
                buffered.extend(current_event)
                return "stream", await writeStreamResponse(
                    request,
                    upstream,
                    bytes(buffered),
                    state,
                    model_name,
                )
            continue
        buffered.extend(current_event)
        if isCapacitySseEvent(current_event):
            return "capacity", bytes(buffered)
        if isMeaningfulSseEvent(current_event):
            return "stream", await writeStreamResponse(request, upstream, bytes(buffered), state, model_name)
        current_event.clear()


async def sendUpstreamRequest(
    request: web.Request,
    session: aiohttp.ClientSession,
    upstream_url: str,
    headers: dict[str, str],
    body: bytes,
) -> aiohttp.ClientResponse:
    target_url = upstream_url.rstrip("/") + request.rel_url.raw_path
    if request.rel_url.raw_query_string:
        target_url += "?" + request.rel_url.raw_query_string
    return await session.request(
        request.method,
        target_url,
        headers=headers,
        data=body,
        allow_redirects=False,
    )


async def handleProxyRequest(request: web.Request) -> web.StreamResponse:
    state = request.app[PROXY_STATE_KEY]
    session = request.app[CLIENT_SESSION_KEY]
    upstream_url = request.app[UPSTREAM_URL_KEY]
    state.request_count += 1

    raw_body = await request.read()
    payload = parseJsonBody(raw_body)
    requested_model = str(payload.get("model") or "") if payload else ""
    candidates = state.buildCandidates(requested_model) if requested_model else [""]
    request_headers = filterRequestHeaders(request.headers)

    for index, model_name in enumerate(candidates):
        attempt_body = raw_body
        if payload is not None and model_name and model_name != requested_model:
            attempt_payload = buildAttemptPayload(payload, model_name)
            attempt_body = encodeJsonBody(attempt_payload)
        upstream = None
        try:
            upstream = await sendUpstreamRequest(
                request,
                session,
                upstream_url,
                request_headers,
                attempt_body,
            )
            can_fallback = bool(model_name and index + 1 < len(candidates))
            if isSseResponse(upstream.headers):
                action, result = await inspectSseResponse(request, upstream, state, model_name)
                if action == "stream":
                    return result  # type: ignore[return-value]
                buffered_body = result  # type: ignore[assignment]
                if action == "capacity":
                    state.markCapacity(model_name)
                if action == "capacity" and can_fallback:
                    state.fallback_count += 1
                    next_model = candidates[index + 1]
                    logging.warning("capacity fallback model=%s next=%s", model_name, next_model)
                    upstream.release()
                    continue
                if action != "capacity":
                    state.markHealthy(model_name)
                return web.Response(
                    status=upstream.status,
                    reason=upstream.reason,
                    headers=filterResponseHeaders(upstream.headers),
                    body=buffered_body,
                )

            response_body = await upstream.read()
            capacity_error = isCapacityHttpResponse(upstream.status, response_body, upstream.headers)
            if capacity_error:
                state.markCapacity(model_name)
            if capacity_error and can_fallback:
                state.fallback_count += 1
                next_model = candidates[index + 1]
                logging.warning("capacity fallback model=%s next=%s", model_name, next_model)
                upstream.release()
                continue
            if not capacity_error:
                state.markHealthy(model_name)
            return web.Response(
                status=upstream.status,
                reason=upstream.reason,
                headers=filterResponseHeaders(upstream.headers),
                body=response_body,
            )
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logging.error("upstream request failed path=%s error=%s", request.path, type(exc).__name__)
            return web.json_response(
                {
                    "error": {
                        "type": "fallback_proxy_upstream_error",
                        "message": "模型中转暂时不可达，未执行跨模型重试。",
                    }
                },
                status=502,
            )
        finally:
            if upstream is not None and not upstream.closed:
                upstream.release()

    return web.json_response(
        {"error": {"type": "fallback_proxy_error", "message": "没有可用的模型候选。"}},
        status=503,
    )


async def handleHealth(request: web.Request) -> web.Response:
    state = request.app[PROXY_STATE_KEY]
    return web.json_response(state.getStatus())


async def createClientSession(app: web.Application) -> None:
    timeout = aiohttp.ClientTimeout(total=None, connect=30, sock_connect=30, sock_read=360)
    app[CLIENT_SESSION_KEY] = aiohttp.ClientSession(
        timeout=timeout,
        auto_decompress=True,
        read_bufsize=1024 * 1024,
    )


async def closeClientSession(app: web.Application) -> None:
    session = app[CLIENT_SESSION_KEY]
    await session.close()


def createApp(
    upstream_url: str | None = None,
    model_chain: tuple[str, ...] | None = None,
    cooldown_seconds: int | None = None,
) -> web.Application:
    app = web.Application(client_max_size=512 * 1024 * 1024)
    app[UPSTREAM_URL_KEY] = readUpstreamUrl(upstream_url)
    app[PROXY_STATE_KEY] = ProxyState(
        model_chain=model_chain or readModelChain(),
        cooldown_seconds=cooldown_seconds
        if cooldown_seconds is not None
        else int(os.getenv("CODEX_FALLBACK_COOLDOWN_SECONDS", DEFAULT_COOLDOWN_SECONDS)),
    )
    app.on_startup.append(createClientSession)
    app.on_cleanup.append(closeClientSession)
    app.router.add_get("/_fallback/health", handleHealth)
    app.router.add_get("/_fallback/status", handleHealth)
    app.router.add_route("*", "/{tail:.*}", handleProxyRequest)
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    listen_host = os.getenv("CODEX_FALLBACK_LISTEN_HOST", DEFAULT_LISTEN_HOST)
    listen_port = int(os.getenv("CODEX_FALLBACK_LISTEN_PORT", DEFAULT_LISTEN_PORT))
    logging.info("model fallback proxy starting host=%s port=%d", listen_host, listen_port)
    web.run_app(createApp(), host=listen_host, port=listen_port, access_log=None)


if __name__ == "__main__":
    main()
