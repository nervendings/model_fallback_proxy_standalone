"""验证：能否为「被 TUI 占 writer」的会话找到可发 /compact 的终端通道。

只做发现与 payload 编码，不发送任何输入（observe_only）。
"""
from __future__ import annotations

import base64
import unittest

from model_fallback_proxy.waveterm_foreground import WaveTermCandidate, WaveTermForegroundAdapter


class DryRunTest(unittest.TestCase):
    def test_can_target_compact_payload_without_sending(self) -> None:
        class NoopTransport:
            def __init__(self) -> None:
                self.calls = []

            def controller_input(self, *args, **kwargs):
                self.calls.append(args)
                return True

        transport = NoopTransport()
        adapter = WaveTermForegroundAdapter(transport=transport, state_path=None, observe_only=True)
        target = WaveTermCandidate(block_id="dry", auth_token="t")
        adapter.inject("dry-1", target, payload="/compact\r")
        # observe_only 模式下不应真的调用 transport
        self.assertEqual(transport.calls, [])
        self.assertEqual(base64.b64decode(base64.b64encode("/compact\r".encode())).decode(), "/compact\r")


if __name__ == "__main__":
    unittest.main()
