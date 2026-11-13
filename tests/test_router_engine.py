"""DAG 多上游路由引擎故障注入测试：用本地假供应商精确触发每类异常。"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import model_fallback_proxy.router_engine as engine


def makeConfig() -> dict:
    return {
        "policy_version": 1,
        "defaults": {
            "retries": 1, "retry_interval_ms": 10, "backoff_multiplier": 2.0,
            "max_retry_interval_ms": 50, "connect_timeout_s": 5,
            "first_byte_timeout_s": 5, "cooldown_s": 60,
        },
        "retry_on": {
            "http_status": [429, 500, 502, 503],
            "keywords": {
                "capacity": True, "network_error": True, "stream_break": True,
                "model_not_found": True, "auth_error": False, "corrupted_history": False,
            },
        },
        "providers": {
            "fake_a": {"base_url": "http://127.0.0.1:9", "auth_mode": "passthrough",
                       "api_key_env": "", "enabled": True, "provider_failure_threshold": 2},
            "fake_b": {"base_url": "http://127.0.0.1:9", "auth_mode": "bearer",
                       "api_key_env": "FAKE_B_KEY", "enabled": True,
                       "provider_failure_threshold": 99},
        },
        "router": {
            "m_a1": {"provider": "fake_a", "children": ["m_b1"]},
            "m_a2": {"provider": "fake_a", "children": ["m_b1"]},
            "m_b1": {"provider": "fake_b", "remote_model": "real-b1", "children": ["m_b2"]},
            "m_b2": {"provider": "fake_b", "children": []},
        },
    }


class UpstreamHarness(unittest.IsolatedAsyncioTestCase):
    """起两个假供应商 + 一个 DAG 代理，脚本化每个响应。"""

    async def asyncSetUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        os.environ["FAKE_B_KEY"] = "secret-b"
        os.environ["CODEX_ROUTER_DEFAULT_PROVIDER"] = "fake_a"
        self.scriptA = []
        self.scriptB = []
        self.received = []
        self.serverA, self.serverB = await self._startFakes()
        cfg = makeConfig()
        cfg["providers"]["fake_a"]["base_url"] = str(self.serverA.make_url("")).rstrip("/")
        cfg["providers"]["fake_b"]["base_url"] = str(self.serverB.make_url("")).rstrip("/")
        self.configPath = Path(self.tmpdir) / "router.json"
        self.configPath.write_text(json.dumps(cfg), encoding="utf-8")
        engine.CONFIG_PATH = self.configPath
        state = engine.loadRouterState()
        self.proxyApp = engine.createRoutedApp(state)
        self.proxyServer = TestServer(self.proxyApp)
        self.client = TestClient(self.proxyServer)
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await self.serverA.close()
        await self.serverB.close()

    def patchConfig(self, mutate) -> None:
        cfg = json.loads(self.configPath.read_text(encoding="utf-8"))
        mutate(cfg)
        self.configPath.write_text(json.dumps(cfg), encoding="utf-8")

    async def _startFakes(self):
        async def handlerA(request):
            self.received.append(("A", await request.json(), dict(request.headers)))
            return await self._next(self.scriptA, request)

        async def handlerB(request):
            self.received.append(("B", await request.json(), dict(request.headers)))
            return await self._next(self.scriptB, request)

        appA, appB = web.Application(), web.Application()
        appA.router.add_route("*", "/{tail:.*}", handlerA)
        appB.router.add_route("*", "/{tail:.*}", handlerB)
        serverA = TestServer(appA)
        serverB = TestServer(appB)
        await serverA.start_server()
        await serverB.start_server()
        return serverA, serverB

    @staticmethod
    async def _next(script, request):
        action = script.pop(0) if script else ("json", 200, {"ok": True})
        kind = action[0]
        if kind == "json":
            return web.json_response(action[2], status=action[1])
        if kind == "sse":
            resp = web.StreamResponse(status=action[1],
                                      headers={"Content-Type": "text/event-stream"})
            await resp.prepare(request)
            for chunk in action[2]:
                if chunk is None:
                    resp.force_close()
                    raise aiohttp.ClientOSError("peer closed")
                await resp.write(chunk)
            await resp.write_eof()
            return resp
        if kind == "hang":
            await asyncio.sleep(action[1])
            return web.json_response({"ok": True})
        raise AssertionError(kind)

    async def post(self, model: str):
        return await self.client.post("/v1/responses", json={"model": model, "input": "x"})


class TestRouting(UpstreamHarness):

    async def testHappyPathStaysOnFirstNode(self):
        self.scriptA.append(("json", 200, {"id": "resp-1"}))
        resp = await self.post("m_a1")
        self.assertEqual(resp.status, 200)
        self.assertIn("id", await resp.json())
        self.assertEqual([r[0] for r in self.received], ["A"])

    async def testCapacityRetriesThenDowngradesAcrossProviders(self):
        self.scriptA.append(("json", 429, {"error": {"message": "Selected model is at capacity"}}))
        self.scriptA.append(("json", 503, {"error": {"message": "server overloaded"}}))
        self.scriptB.append(("json", 200, {"id": "from-b"}))
        resp = await self.post("m_a1")
        self.assertEqual(resp.status, 200)
        self.assertEqual([r[0] for r in self.received], ["A", "A", "B"])
        self.assertEqual(self.received[2][1]["model"], "real-b1")
        self.assertEqual(self.received[2][2].get("Authorization"), "Bearer secret-b")
        status = self.proxyApp[engine.ROUTER_STATE_KEY].getStatus()
        self.assertGreaterEqual(status["fallback_count"], 1)
        self.assertIn("fake_a:m_a1", status["cooldowns_seconds"])

    async def testConnectionRefusedTriggersFallback(self):
        self.patchConfig(lambda cfg: cfg["providers"]["fake_a"].__setitem__(
            "base_url", "http://127.0.0.1:1"))
        self.scriptB.append(("json", 200, {"id": "from-b"}))
        resp = await self.post("m_a1")
        self.assertEqual(resp.status, 200)
        self.assertEqual(self.received[0][0], "B")

    async def testProviderFuseSkipsWholeChannel(self):
        def mutate(cfg):
            cfg["providers"]["fake_a"]["base_url"] = "http://127.0.0.1:1"
            cfg["router"]["m_a1"]["children"] = ["m_a2", "m_b1"]
        self.patchConfig(mutate)
        self.scriptB.append(("json", 200, {"id": "x"}))
        resp = await self.post("m_a1")
        self.assertEqual(resp.status, 200)
        state = self.proxyApp[engine.ROUTER_STATE_KEY]
        self.assertIn("fake_a", state.provider_blocked_until)
        before = len(self.received)
        self.scriptB.append(("json", 200, {"id": "y"}))
        resp2 = await self.post("m_a1")
        self.assertEqual(resp2.status, 200)
        self.assertEqual([r[0] for r in self.received[before:]], ["B"])

    async def testTerminalErrorPassesThroughWithoutFallback(self):
        self.scriptA.append(("json", 400, {"error": {"message": "bad request something"}}))
        resp = await self.post("m_a1")
        self.assertEqual(resp.status, 400)
        self.assertEqual([r[0] for r in self.received], ["A"])

    async def testSseCapacityTriggersFallback(self):
        errEvent = (b"event: error\ndata: {\"type\":\"error\","
                    b"\"message\":\"Our servers are currently overloaded\"}\n\n")
        self.scriptA.append(("sse", 200, [errEvent]))
        self.scriptA.append(("sse", 200, [errEvent]))
        self.scriptB.append(("json", 200, {"id": "b-after-sse"}))
        resp = await self.post("m_a1")
        self.assertEqual(resp.status, 200)
        self.assertEqual(await resp.json(), {"id": "b-after-sse"})

    async def testSseStreamBreakAfterOutputIsTerminal(self):
        first = (b"event: response.output_text.delta\ndata: "
                 b"{\"type\":\"response.output_text.delta\",\"delta\":\"hi\"}\n\n")
        self.scriptA.append(("sse", 200, [first, None]))
        self.scriptB.append(("json", 200, {"id": "must-not-be-used"}))
        resp = await self.post("m_a1")
        self.assertEqual(resp.status, 200)
        self.assertIn("stream_break_after_output", await resp.text())
        self.assertEqual([r[0] for r in self.received], ["A"])

    async def testUnknownModelPassthroughDefaultProvider(self):
        self.scriptA.append(("json", 200, {"id": "passthrough"}))
        resp = await self.post("some-new-model")
        self.assertEqual(resp.status, 200)
        self.assertEqual(self.received[0][1]["model"], "some-new-model")

    async def testFirstByteTimeoutDowngrades(self):
        def mutate(cfg):
            cfg["defaults"]["first_byte_timeout_s"] = 0.3
            cfg["defaults"]["retries"] = 0
        self.patchConfig(mutate)
        self.scriptA.append(("hang", 3))
        self.scriptB.append(("json", 200, {"id": "after-timeout"}))
        resp = await self.post("m_a1")
        self.assertEqual(resp.status, 200)
        self.assertEqual([r[0] for r in self.received], ["A", "B"])

    async def testChainExhaustedReturnsLastUpstreamError(self):
        def mutate(cfg):
            cfg["router"]["m_b1"]["children"] = []
            cfg["defaults"]["retries"] = 0
        self.patchConfig(mutate)
        self.scriptA.append(("json", 429, {"error": {"message": "capacity exceeded"}}))
        self.scriptB.append(("json", 503, {"error": {"message": "upstream unavailable"}}))
        resp = await self.post("m_a1")
        self.assertEqual(resp.status, 503)
        self.assertEqual(resp.headers.get("X-Fallback-Exhausted"), "1")


class TestConfigValidation(unittest.TestCase):

    def validBase(self):
        return makeConfig()

    def testRejectCycle(self):
        cfg = self.validBase()
        cfg["router"]["m_b2"]["children"] = ["m_a1"]
        self.assertIn("环", engine.validateConfig(cfg))

    def testRejectUnknownProvider(self):
        cfg = self.validBase()
        cfg["router"]["m_a1"]["provider"] = "nope"
        self.assertIn("不存在的供应商", engine.validateConfig(cfg))

    def testRejectDanglingChild(self):
        cfg = self.validBase()
        cfg["router"]["m_a1"]["children"] = ["ghost"]
        self.assertIn("不在 router 中", engine.validateConfig(cfg))

    def testRejectEmptyRemoteModel(self):
        cfg = self.validBase()
        cfg["router"]["m_a1"]["remote_model"] = "  "
        self.assertIn("remote_model", engine.validateConfig(cfg))

    def testDfsOrderRespectsChildrenPriority(self):
        cfg = self.validBase()
        cfg["router"]["m_a1"]["children"] = ["m_b2", "m_b1"]
        state = engine.RouterState(engine.RouterConfig(cfg))
        order = [c.model for c in state.buildCandidates("m_a1")]
        self.assertEqual(order, ["m_a1", "m_b2", "m_b1"])

    def testRetryDelaysBackoff(self):
        cfg = self.validBase()
        cfg["defaults"].update({"retries": 3, "retry_interval_ms": 100,
                                "backoff_multiplier": 2.0, "max_retry_interval_ms": 300})
        policy = engine.RouterConfig(cfg).nodePolicy("m_a1")
        self.assertEqual(policy.retryDelaysS(), [0.1, 0.2, 0.3])


class TestSpecialRules(UpstreamHarness):
    """特殊规则：400001 上下文溢出 -> 不重试不冷却，直接换下一节点并发 compact 信号。"""

    async def testContextOverflow400001SwitchesModel(self):
        body400 = {"error": {"message": "400001 read body failed, Invalid request error."}}
        self.scriptA.append(("json", 400, body400))
        self.scriptB.append(("json", 200, {"id": "after-400001"}))
        resp = await self.post("m_a1")
        self.assertEqual(resp.status, 200)
        self.assertEqual(await resp.json(), {"id": "after-400001"})
        self.assertEqual([r[0] for r in self.received], ["A", "B"])
        state = self.proxyApp[engine.ROUTER_STATE_KEY]
        status = state.getStatus()
        self.assertNotIn("fake_a:m_a1", status["cooldowns_seconds"])
        self.assertEqual(status["signals"].get("compact"), 1)
        self.assertTrue(any(str(ev["kind"]).startswith("special:context_overflow")
                            for ev in status["events"]))

    async def testContextOverflowAtChainEndReturnsSignalHeader(self):
        def mutate(cfg):
            cfg["router"]["m_a1"]["children"] = ["m_b1"]
            cfg["router"]["m_b1"]["children"] = []
        self.patchConfig(mutate)
        self.scriptA.append(("json", 400, {"error": {"message": "400001 read body failed"}}))
        self.scriptB.append(("json", 400, {"error": {"message": "context_length_exceeded"}}))
        resp = await self.post("m_a1")
        self.assertEqual(resp.status, 400)
        self.assertEqual(resp.headers.get("X-Fallback-Signal"), "compact")
        self.assertEqual(resp.headers.get("X-Fallback-Rule"), "context_overflow")

    async def testCorruptedHistoryDoesNotSwitch(self):
        body = {"error": {"message": "Invalid content type: encrypted_content"}}
        self.scriptA.append(("json", 400, body))
        resp = await self.post("m_a1")
        self.assertEqual(resp.status, 400)
        self.assertEqual(resp.headers.get("X-Fallback-Signal"), "new_session")
        self.assertEqual([r[0] for r in self.received], ["A"])
        state = self.proxyApp[engine.ROUTER_STATE_KEY]
        self.assertEqual(state.getStatus()["signals"].get("new_session"), 1)

    def testValidateRejectsUnknownSignal(self):
        cfg = makeConfig()
        cfg["special_rules"] = [{"name": "x", "patterns": ["y"], "signal": "teleport"}]
        self.assertIn("signal", engine.validateConfig(cfg))

    def testValidateAcceptsBundledRules(self):
        self.assertEqual(engine.validateConfig(json.loads(
            Path(__file__).resolve().parents[1].joinpath("configs/router.json")
            .read_text(encoding="utf-8"))), "")


if __name__ == "__main__":
    unittest.main()
