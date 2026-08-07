#!/usr/bin/env python3
"""模型容量降级代理测试。"""
from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from aiohttp import ClientSession, web

from model_fallback_proxy.model_fallback_proxy import (
    DEFAULT_MODEL_CHAIN,
    buildAttemptPayload,
    createApp,
    isCapacityHttpResponse,
    isCapacitySseEvent,
    isCapacityText,
    isMeaningfulSseEvent,
)


MODEL_CHAIN = ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna")


class RunningApp:
    def __init__(self, app: web.Application) -> None:
        self.app = app
        self.runner = web.AppRunner(app)
        self.url = ""

    async def __aenter__(self) -> "RunningApp":
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        server = site._server
        assert server is not None
        port = server.sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.runner.cleanup()


def buildSse(events: list[tuple[str, dict]]) -> str:
    return "".join(
        f"event: {event_name}\ndata: {json.dumps(payload)}\n\n"
        for event_name, payload in events
    )


class ModelFallbackProxyTest(unittest.IsolatedAsyncioTestCase):
    def test_capacity_markers(self) -> None:
        self.assertTrue(isCapacityText("Selected model is at capacity. Please try a different model."))
        self.assertTrue(isCapacityText('{"codex_error_info":"server_overloaded"}'))
        self.assertFalse(isCapacityText("invalid API key"))

    def test_http_capacity_fallback_accepts_error_headers_and_empty_503(self) -> None:
        self.assertTrue(
            isCapacityHttpResponse(502, b'{"error":"unavailable"}', {"X-Codex-Error-Info": "server_overloaded"})
        )
        self.assertTrue(isCapacityHttpResponse(503, b""))
        self.assertTrue(isCapacityHttpResponse(529, b"temporary upstream failure"))
        self.assertTrue(isCapacityHttpResponse(429, b""))
        self.assertFalse(isCapacityHttpResponse(500, b"temporary upstream failure"))

    def test_default_model_chain_stops_at_luna(self) -> None:
        self.assertEqual(DEFAULT_MODEL_CHAIN, MODEL_CHAIN)

    def test_upstream_url_requires_explicit_safe_environment_value(self) -> None:
        from model_fallback_proxy.model_fallback_proxy import readUpstreamUrl

        with patch.dict(os.environ, {"CODEX_FALLBACK_UPSTREAM_URL": ""}):
            with self.assertRaises(RuntimeError):
                readUpstreamUrl()
        with self.assertRaises(RuntimeError):
            readUpstreamUrl("https://user@example.invalid")

    def test_http_capacity_fallback_accepts_message_only_json_body(self) -> None:
        body = b'{"message":"Selected model is at capacity. Please try a different model."}'
        self.assertTrue(isCapacityHttpResponse(200, body))

    def test_sse_event_classification(self) -> None:
        created = b'event: response.created\ndata: {"type":"response.created"}\n\n'
        output = b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta"}\n\n'
        self.assertFalse(isMeaningfulSseEvent(created))
        self.assertTrue(isMeaningfulSseEvent(output))
        self.assertFalse(isCapacitySseEvent(output))

        raw_error = b'data: {"error":{"code":"server_overloaded"}}\n\n'
        self.assertTrue(isCapacitySseEvent(raw_error))

    def test_capacity_text_in_normal_output_is_not_an_error(self) -> None:
        normal_http = b'{"output_text":"Selected model is at capacity"}'
        normal_sse = (
            b'event: response.output_text.delta\n'
            b'data: {"type":"response.output_text.delta","delta":"server overloaded"}\n\n'
        )
        self.assertFalse(isCapacityHttpResponse(200, normal_http))
        self.assertFalse(isCapacitySseEvent(normal_sse))

    def test_luna_rewrites_unsupported_ultra_effort(self) -> None:
        payload = {"model": "gpt-5.6-sol", "reasoning": {"effort": "ultra", "summary": "auto"}}
        terra_payload = buildAttemptPayload(payload, "gpt-5.6-terra")
        luna_payload = buildAttemptPayload(payload, "gpt-5.6-luna")
        self.assertEqual(terra_payload["reasoning"]["effort"], "ultra")
        self.assertEqual(luna_payload["reasoning"]["effort"], "max")
        self.assertEqual(payload["reasoning"]["effort"], "ultra")

    async def test_http_and_sse_capacity_fall_through_chain(self) -> None:
        attempted_models: list[str] = []

        async def upstreamHandler(request: web.Request) -> web.Response:
            payload = await request.json()
            model_name = payload["model"]
            attempted_models.append(model_name)
            if model_name == "gpt-5.6-sol":
                return web.json_response(
                    {"error": {"message": "Selected model is at capacity.", "code": "server_overloaded"}},
                    status=503,
                )
            if model_name == "gpt-5.6-terra":
                body = buildSse(
                    [
                        ("response.created", {"type": "response.created"}),
                        (
                            "response.failed",
                            {
                                "type": "response.failed",
                                "response": {"status": "failed", "error": {"code": "server_overloaded"}},
                            },
                        ),
                    ]
                )
                return web.Response(text=body, content_type="text/event-stream")
            body = buildSse(
                [
                    ("response.created", {"type": "response.created", "response": {"model": model_name}}),
                    ("response.output_text.delta", {"type": "response.output_text.delta", "delta": "OK"}),
                    ("response.completed", {"type": "response.completed"}),
                ]
            )
            return web.Response(text=body, content_type="text/event-stream")

        upstream_app = web.Application()
        upstream_app.router.add_post("/responses", upstreamHandler)
        async with RunningApp(upstream_app) as upstream:
            proxy_app = createApp(upstream.url, MODEL_CHAIN, cooldown_seconds=30)
            async with RunningApp(proxy_app) as proxy:
                async with ClientSession() as session:
                    response = await session.post(
                        proxy.url + "/responses",
                        json={"model": "gpt-5.6-sol", "stream": True},
                    )
                    response_text = await response.text()
                    self.assertEqual(response.status, 200)
                    self.assertIn('"delta": "OK"', response_text)
                    self.assertNotIn("server_overloaded", response_text)
                    status = await (await session.get(proxy.url + "/_fallback/status")).json()
        self.assertEqual(attempted_models, list(MODEL_CHAIN))
        self.assertEqual(status["fallback_count"], 2)
        self.assertEqual(status["capacity_count"], 2)

    async def test_luna_capacity_does_not_fallback_to_lower_model(self) -> None:
        attempted_models: list[str] = []

        async def upstreamHandler(request: web.Request) -> web.Response:
            payload = await request.json()
            attempted_models.append(payload["model"])
            return web.json_response(
                {"error": {"message": "Selected model is at capacity.", "code": "server_overloaded"}},
                status=503,
            )

        upstream_app = web.Application()
        upstream_app.router.add_post("/responses", upstreamHandler)
        async with RunningApp(upstream_app) as upstream:
            proxy_app = createApp(upstream.url, MODEL_CHAIN, cooldown_seconds=30)
            async with RunningApp(proxy_app) as proxy:
                async with ClientSession() as session:
                    response = await session.post(
                        proxy.url + "/responses",
                        json={"model": "gpt-5.6-luna", "stream": True},
                    )
                    self.assertEqual(response.status, 503)
                    status = await (await session.get(proxy.url + "/_fallback/status")).json()
        self.assertEqual(attempted_models, ["gpt-5.6-luna"])
        self.assertEqual(status["fallback_count"], 0)
        self.assertEqual(status["capacity_count"], 1)

    async def test_structural_sse_events_do_not_block_capacity_fallback(self) -> None:
        attempted_models: list[str] = []

        async def upstreamHandler(request: web.Request) -> web.Response:
            payload = await request.json()
            model_name = payload["model"]
            attempted_models.append(model_name)
            if model_name == "gpt-5.6-sol":
                body = buildSse(
                    [
                        ("response.created", {"type": "response.created"}),
                        ("response.output_item.added", {"type": "response.output_item.added"}),
                        ("response.content_part.added", {"type": "response.content_part.added"}),
                        (
                            "response.failed",
                            {
                                "type": "response.failed",
                                "response": {"status": "failed", "error": {"code": "server_overloaded"}},
                            },
                        ),
                    ]
                )
                return web.Response(text=body, content_type="text/event-stream")
            body = buildSse(
                [
                    ("response.created", {"type": "response.created", "response": {"model": model_name}}),
                    ("response.output_text.delta", {"type": "response.output_text.delta", "delta": "OK"}),
                    ("response.completed", {"type": "response.completed"}),
                ]
            )
            return web.Response(text=body, content_type="text/event-stream")

        upstream_app = web.Application()
        upstream_app.router.add_post("/responses", upstreamHandler)
        async with RunningApp(upstream_app) as upstream:
            proxy_app = createApp(upstream.url, MODEL_CHAIN, cooldown_seconds=30)
            async with RunningApp(proxy_app) as proxy:
                async with ClientSession() as session:
                    response = await session.post(
                        proxy.url + "/responses",
                        json={"model": "gpt-5.6-sol", "stream": True},
                    )
                    response_text = await response.text()
                    self.assertEqual(response.status, 200)
                    self.assertIn('"delta": "OK"', response_text)
                    status = await (await session.get(proxy.url + "/_fallback/status")).json()
        self.assertEqual(attempted_models, ["gpt-5.6-sol", "gpt-5.6-terra"])
        self.assertEqual(status["fallback_count"], 1)
        self.assertEqual(status["capacity_count"], 1)

    async def test_non_capacity_error_does_not_fallback(self) -> None:
        attempted_models: list[str] = []

        async def upstreamHandler(request: web.Request) -> web.Response:
            payload = await request.json()
            attempted_models.append(payload["model"])
            return web.json_response({"error": {"message": "invalid API key"}}, status=401)

        upstream_app = web.Application()
        upstream_app.router.add_post("/responses", upstreamHandler)
        async with RunningApp(upstream_app) as upstream:
            proxy_app = createApp(upstream.url, MODEL_CHAIN, cooldown_seconds=30)
            async with RunningApp(proxy_app) as proxy:
                async with ClientSession() as session:
                    response = await session.post(proxy.url + "/responses", json={"model": MODEL_CHAIN[0]})
                    self.assertEqual(response.status, 401)
                    self.assertIn("invalid API key", await response.text())
        self.assertEqual(attempted_models, [MODEL_CHAIN[0]])

    async def test_capacity_after_output_marks_cooldown_without_replay(self) -> None:
        attempted_models: list[str] = []

        async def upstreamHandler(request: web.Request) -> web.Response:
            payload = await request.json()
            model_name = payload["model"]
            attempted_models.append(model_name)
            if model_name == "gpt-5.6-terra":
                body = buildSse(
                    [
                        ("response.output_text.delta", {"type": "response.output_text.delta", "delta": "terra"}),
                        ("response.completed", {"type": "response.completed"}),
                    ]
                )
                return web.Response(text=body, content_type="text/event-stream")
            body = buildSse(
                [
                    ("response.output_text.delta", {"type": "response.output_text.delta", "delta": "partial"}),
                    (
                        "response.failed",
                        {"type": "response.failed", "error": {"code": "server_overloaded"}},
                    ),
                ]
            )
            return web.Response(text=body, content_type="text/event-stream")

        upstream_app = web.Application()
        upstream_app.router.add_post("/responses", upstreamHandler)
        async with RunningApp(upstream_app) as upstream:
            proxy_app = createApp(upstream.url, MODEL_CHAIN, cooldown_seconds=30)
            async with RunningApp(proxy_app) as proxy:
                async with ClientSession() as session:
                    response = await session.post(
                        proxy.url + "/responses",
                        json={"model": MODEL_CHAIN[0], "stream": True},
                    )
                    response_text = await response.text()
                    self.assertIn("partial", response_text)
                    self.assertIn("server_overloaded", response_text)
                    status = await (await session.get(proxy.url + "/_fallback/status")).json()
                    response = await session.post(
                        proxy.url + "/responses",
                        json={"model": MODEL_CHAIN[0], "stream": True},
                    )
                    response_text = await response.text()
                    self.assertIn("terra", response_text)
        self.assertEqual(attempted_models, ["gpt-5.6-sol", "gpt-5.6-terra"])
        self.assertEqual(status["capacity_count"], 1)
        self.assertEqual(status["fallback_count"], 0)


if __name__ == "__main__":
    unittest.main()
