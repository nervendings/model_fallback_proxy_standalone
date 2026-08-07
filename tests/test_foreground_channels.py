from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from model_fallback_proxy.foreground_channels import (
    CHANNEL_TERMINAL,
    CHANNEL_VIRTUAL_EMPLOYEE,
    ControlledPtyTransport,
    MultiFrontendAdapter,
    VirtualEmployeeHookTransport,
)
from model_fallback_proxy.waveterm_foreground import PAYLOAD, WaveTermCandidate


class ForegroundChannelTests(unittest.TestCase):
    def _candidate(self, **kwargs) -> WaveTermCandidate:
        values = {
            "pty_path": "/dev/pts/9",
            "cwd": "/tmp/work",
            "pid": 42,
            "session_id": "00000000-0000-4000-8000-000000000002",
            "channel": CHANNEL_TERMINAL,
            "starttime": "12345",
        }
        values.update(kwargs)
        return WaveTermCandidate(block_id="", **values)

    def test_controlled_pty_requires_composite_identity(self) -> None:
        writes: list[bytes] = []
        with tempfile.TemporaryDirectory() as root:
            transport = ControlledPtyTransport(root, direct_enabled=True, writer=lambda _target, data: writes.append(data) or True)
            candidate = self._candidate()
            links = {
                "/proc/42/fd/0": "/dev/pts/9",
                "/proc/42/cwd": "/tmp/work",
            }
            with patch("model_fallback_proxy.foreground_channels._read_starttime", return_value="12345"), patch(
                "model_fallback_proxy.foreground_channels._read_cmdline", return_value="codex 00000000-0000-4000-8000-000000000002"
            ), patch("os.readlink", side_effect=lambda path: links[str(path)]):
                encoded = base64.b64encode(PAYLOAD.encode()).decode()
                self.assertTrue(transport.controller_input("", encoded, candidate=candidate))
            self.assertEqual(writes, [PAYLOAD.encode()])

    def test_controlled_pty_rejects_pid_reuse(self) -> None:
        writes: list[bytes] = []
        with tempfile.TemporaryDirectory() as root:
            transport = ControlledPtyTransport(root, direct_enabled=True, writer=lambda _target, data: writes.append(data) or True)
            candidate = self._candidate()
            with patch("model_fallback_proxy.foreground_channels._read_starttime", return_value="different"):
                encoded = base64.b64encode(PAYLOAD.encode()).decode()
                self.assertFalse(transport.controller_input("", encoded, candidate=candidate))
            self.assertEqual(writes, [])

    def test_bare_terminal_uses_tiocsti_instead_of_writing_slave_output(self) -> None:
        injected: list[bytes] = []
        with tempfile.TemporaryDirectory() as root:
            transport = ControlledPtyTransport(root, direct_enabled=True)
            candidate = self._candidate()
            links = {
                "/proc/42/fd/0": "/dev/pts/9",
                "/proc/42/cwd": "/tmp/work",
            }

            def ioctl(_fd, request, value):
                self.assertEqual(request, 0x5412)
                injected.append(bytes(value))
                return 0

            with patch("model_fallback_proxy.foreground_channels._read_starttime", return_value="12345"), patch(
                "model_fallback_proxy.foreground_channels._read_cmdline", return_value="codex 00000000-0000-4000-8000-000000000002"
            ), patch("os.readlink", side_effect=lambda path: links[str(path)]), patch(
                "model_fallback_proxy.foreground_channels.fcntl.ioctl", side_effect=ioctl
            ), patch("os.open", return_value=11), patch("os.close"):
                encoded = base64.b64encode(PAYLOAD.encode()).decode()
                self.assertTrue(transport.controller_input("", encoded, candidate=candidate))
        self.assertEqual(b"".join(injected), PAYLOAD.encode())

    def test_tmux_transport_uses_validated_pane_and_fixed_input(self) -> None:
        calls: list[list[str]] = []

        def runner(argv, **_kwargs):
            calls.append(list(argv))
            return SimpleNamespace(returncode=0, stdout="/dev/pts/9\n", stderr="")

        with tempfile.TemporaryDirectory() as root:
            transport = ControlledPtyTransport(root, direct_enabled=False, runner=runner)
            candidate = self._candidate(tmux_target="%7")
            with patch("model_fallback_proxy.foreground_channels._read_starttime", return_value="12345"), patch(
                "model_fallback_proxy.foreground_channels._read_cmdline", return_value="codex 00000000-0000-4000-8000-000000000002"
            ), patch("os.readlink", side_effect={"/proc/42/fd/0": "/dev/pts/9", "/proc/42/cwd": "/tmp/work"}.get):
                encoded = base64.b64encode(PAYLOAD.encode()).decode()
                self.assertTrue(transport.controller_input("", encoded, candidate=candidate))
        self.assertEqual(calls[0][:5], ["tmux", "display-message", "-p", "-t", "%7"])
        self.assertEqual(calls[1], ["tmux", "send-keys", "-t", "%7", "继续", "C-m"])

    def test_virtual_employee_hook_receives_structured_request(self) -> None:
        requests: list[dict] = []

        def runner(_argv, **kwargs):
            requests.append(json.loads(kwargs["input"]))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        transport = VirtualEmployeeHookTransport(["/bin/true"], runner=runner)
        candidate = WaveTermCandidate(
            block_id="",
            cwd="/tmp/employee",
            session_id="session-1",
            channel=CHANNEL_VIRTUAL_EMPLOYEE,
            hook_id="job-7",
        )
        encoded = base64.b64encode(PAYLOAD.encode()).decode()
        self.assertTrue(transport.controller_input("", encoded, candidate=candidate, event_id="event-1"))
        self.assertEqual(requests[0]["action"], "continue")
        self.assertEqual(requests[0]["session_id"], "session-1")
        self.assertEqual(requests[0]["hook_id"], "job-7")
        self.assertEqual(requests[0]["payload"], PAYLOAD)

    def test_unknown_channel_is_observe_only(self) -> None:
        adapter = MultiFrontendAdapter(channel="auto", observe_only=False)
        result = adapter.continueSession({"frontend_channel": "unknown", "session_id": "s", "turn_id": "t"})
        self.assertEqual(result["status"], "observe_only")
        self.assertEqual(result["reason"], "unknown_channel")

    def test_terminal_channel_does_not_depend_on_waveterm(self) -> None:
        writes: list[bytes] = []
        candidate = self._candidate(tmux_target="")
        adapter = MultiFrontendAdapter(channel="terminal", observe_only=False, terminal_direct=True)
        adapter.transport.terminal.writer = lambda _target, data: writes.append(data) or True
        with patch.object(adapter, "discover_candidates", return_value=[candidate]), patch(
            "model_fallback_proxy.foreground_channels._read_starttime", return_value="12345"
        ), patch(
            "model_fallback_proxy.foreground_channels._read_cmdline", return_value="codex 00000000-0000-4000-8000-000000000002"
        ), patch(
            "os.readlink", side_effect={"/proc/42/fd/0": "/dev/pts/9", "/proc/42/cwd": "/tmp/work"}.get
        ):
            result = adapter.continueSession(
                {
                    "frontend_channel": "terminal",
                    "session_id": candidate.session_id,
                    "turn_id": "turn-terminal",
                    "cwd": candidate.cwd,
                    "force_continue": True,
                }
            )
        self.assertEqual(result["status"], "injected")
        self.assertEqual(writes, [PAYLOAD.encode()])


if __name__ == "__main__":
    unittest.main()
