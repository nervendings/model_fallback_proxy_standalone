"""注入 payload 可覆盖：被 TUI 占 writer 的会话需要发 /compact 而不是「继续」。"""
from __future__ import annotations

import base64
import unittest
from pathlib import Path

from model_fallback_proxy.waveterm_foreground import PAYLOAD, WaveTermCandidate, WaveTermForegroundAdapter


class RecordingTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def controller_input(self, block_id, encoded, auth_token, target, event_id):
        self.calls.append((block_id, base64.b64decode(encoded).decode("utf-8")))
        return True


def makeAdapter(tmp: Path) -> tuple[WaveTermForegroundAdapter, RecordingTransport]:
    transport = RecordingTransport()
    adapter = WaveTermForegroundAdapter(
        transport=transport,
        state_path=tmp / "fg.json",
        clock=lambda: 1000.0,
        observe_only=False,  # 生产用 CODEX_AUTO_CONTINUE_FOREGROUND=1 关闭只观察
    )
    return adapter, transport


class InjectPayloadTest(unittest.TestCase):
    def test_default_payload_is_continue(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            adapter, transport = makeAdapter(Path(tmp))
            target = WaveTermCandidate(block_id="b1", auth_token="t")
            adapter.inject("e1", target)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(transport.calls[0][1], PAYLOAD)

    def test_compact_payload_can_be_sent(self) -> None:
        """被 TUI 持有 writer 的会话只能靠 /compact 救，必须支持自定义 payload。"""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            adapter, transport = makeAdapter(Path(tmp))
            target = WaveTermCandidate(block_id="b2", auth_token="t")
            adapter.inject("e2", target, payload="/compact\r")
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(transport.calls[0][1], "/compact\r")


class ObserveOnlyGuardTest(unittest.TestCase):
    def test_observe_only_never_sends(self) -> None:
        """observe_only=True 必须真的不发送。

        2026-11-06 审计发现旧 inject() 完全绕过该开关：调用方以为在观察，
        实际已经把按键打进用户终端。这是注入安全门，不能只靠调用方自觉。
        """
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            transport = RecordingTransport()
            adapter = WaveTermForegroundAdapter(
                transport=transport,
                state_path=Path(tmp) / "fg.json",
                clock=lambda: 1000.0,
                observe_only=True,
            )
            result = adapter.inject(
                "e3", WaveTermCandidate(block_id="b3", auth_token="t"), payload="/compact\r"
            )
        self.assertEqual(transport.calls, [], "observe_only 下不得发送任何输入")
        self.assertEqual(result["status"], "observe_only")


if __name__ == "__main__":
    unittest.main()
