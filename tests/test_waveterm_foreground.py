import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from model_fallback_proxy.waveterm_foreground import PAYLOAD, WaveTermCandidate, WaveTermForegroundAdapter, findSessionBlockId


class FakeTransport:
    def __init__(self, result=True):
        self.result = result
        self.calls = []

    def controller_input(self, block_id, input_data64, _auth_token=None):
        self.calls.append((block_id, input_data64))
        return self.result


class FakeSocket:
    last_requests = []

    def __init__(self, *_args, **_kwargs):
        self.sent = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def settimeout(self, _value):
        return None

    def connect(self, _path):
        return None

    def sendall(self, data):
        request = json.loads(data.decode())
        self.sent.append(request)
        type(self).last_requests.append(request)

    def recv(self, _size):
        request = self.sent[-1]
        return (json.dumps({"resid": request["reqid"], "data": {}}) + "\n").encode()


class WaveTermForegroundTest(unittest.TestCase):
    def test_snapshot_recovers_wave_term_block_and_force_continues(self):
        transport = FakeTransport()
        session_id = "00000000-0000-4000-8000-000000000003"
        block_id = "00000000-0000-4000-8000-000000000004"
        with tempfile.TemporaryDirectory() as root:
            snapshot_root = Path(root)
            (snapshot_root / f"{session_id}.123.sh").write_text(
                f'declare -x WAVETERM_BLOCKID="{block_id}"\n', encoding="utf-8"
            )
            target = WaveTermCandidate(block_id, "/dev/pts/3", "/tmp", 42, True, "", "jwt")
            other = WaveTermCandidate("other-block", "/dev/pts/4", "/tmp", 43, True, "", "jwt")
            adapter = WaveTermForegroundAdapter(
                transport,
                observe_only=False,
                discover=lambda: [other, target],
                prompt_reader=lambda _target: "working",
                snapshot_root=snapshot_root,
            )
            self.assertEqual(findSessionBlockId(session_id, snapshot_root), block_id)
            result = adapter.continueSession(
                {
                    "session_id": session_id,
                    "turn_id": "turn-force",
                    "file_state": {"cwd": "/tmp"},
                    "force_continue": True,
                }
            )
        self.assertEqual(result["status"], "injected")
        self.assertEqual(transport.calls, [(block_id, base64.b64encode(PAYLOAD.encode()).decode())])

    def test_observe_only_and_stable_prompt(self):
        transport = FakeTransport()
        adapter = WaveTermForegroundAdapter(transport)
        target = WaveTermCandidate("b1", "/dev/pts/3")
        self.assertEqual(adapter.observe("e1", target, "›", now=0)["status"], "observe_only")
        self.assertEqual(adapter.observe("e1", target, "›", now=1)["reason"], "observe_only")
        self.assertFalse(transport.calls)

    def test_inject_is_fixed_and_idempotent(self):
        transport = FakeTransport()
        adapter = WaveTermForegroundAdapter(transport, observe_only=False)
        target = WaveTermCandidate("b1")
        adapter.observe("e1", target, "›", now=0)
        self.assertEqual(adapter.observe("e1", target, "›", now=1)["status"], "injected")
        adapter.inject("e1", target, now=2)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(transport.calls[0], ("b1", base64.b64encode(PAYLOAD.encode()).decode()))

    def test_unique_target_and_backoff(self):
        transport = FakeTransport(False)
        adapter = WaveTermForegroundAdapter(transport, observe_only=False)
        candidates = [WaveTermCandidate("a", "/dev/pts/1"), WaveTermCandidate("b", "/dev/pts/1")]
        self.assertIsNone(adapter.find_unique_target(candidates, pty_path="/dev/pts/1"))
        target = WaveTermCandidate("a", "/dev/pts/1")
        self.assertEqual(adapter.inject("e1", target, now=0)["status"], "failed")
        self.assertEqual(adapter.inject("e1", target, now=1)["status"], "backoff")

    def test_continue_session_requires_unique_target_and_stable_ready_prompt(self):
        transport = FakeTransport()
        now = [0.0]
        target = WaveTermCandidate("b1", "/dev/pts/3", "/tmp", 42, True, "s1", "jwt")
        adapter = WaveTermForegroundAdapter(
            transport,
            observe_only=False,
            discover=lambda: [target],
            prompt_reader=lambda _target: "›",
            sleep_fn=lambda seconds: now.__setitem__(0, now[0] + seconds),
            clock=lambda: now[0],
        )
        result = adapter.continueSession({"session_id": "s1", "turn_id": "t1", "file_state": {"cwd": "/tmp"}})
        self.assertEqual(result["status"], "injected")
        self.assertEqual(len(transport.calls), 1)

    def test_continue_session_uses_unique_cwd_when_tui_has_no_session_id(self):
        transport = FakeTransport()
        now = [0.0]
        target = WaveTermCandidate("b1", "/dev/pts/3", "/tmp", 42, True, "", "jwt")
        adapter = WaveTermForegroundAdapter(
            transport,
            observe_only=False,
            discover=lambda: [target],
            prompt_reader=lambda _target: "›",
            sleep_fn=lambda seconds: now.__setitem__(0, now[0] + seconds),
            clock=lambda: now[0],
        )
        result = adapter.continueSession({"session_id": "s1", "turn_id": "t1", "file_state": {"cwd": "/tmp"}})
        self.assertEqual(result["status"], "injected")

    def test_confirmation_before_prompt_is_not_injected(self):
        transport = FakeTransport()
        adapter = WaveTermForegroundAdapter(transport, observe_only=False)
        target = WaveTermCandidate("b1")
        result = adapter.observe("e1", target, "确认执行 commit 吗？\n›", now=1)
        self.assertEqual(result["reason"], "unsafe_prompt")
        self.assertFalse(transport.calls)

    def test_ready_prompt_detects_new_tui_idle_state(self):
        """新版 TUI 空闲态：占位符提示符 + DeepSeek 状态行也视为可输入。"""
        idle = (
            "› Ask Codex to do anything\n"
            "\n"
            "  DeepSeek-Flash max · Context 63% left · master · ~/x\n"
            "  ? for shortcuts              ⚠ 3 warnings · f2 to view"
        )
        ready, _ = WaveTermForegroundAdapter._ready_prompt(idle)
        self.assertTrue(ready)

    def test_ready_prompt_rejects_busy_tui(self):
        """工作回合未结束时（esc to interrupt）不得注入，避免打扰正常对话。"""
        busy = (
            "• Working (42m 10s • esc to interrupt)\n"
            "  └ Tip: Press f3 to search this conversation.\n"
            "\n"
            "› Ask Codex to do anything\n"
            "\n"
            "  DeepSeek-Flash max · Context 64% left"
        )
        ready, _ = WaveTermForegroundAdapter._ready_prompt(busy)
        self.assertFalse(ready)

    def test_ready_prompt_rejects_user_draft(self):
        """输入框已有用户草稿时不得注入，避免覆盖或误发。"""
        draft = "› 我打了一半的字\n\n  DeepSeek-Flash max · Context 63% left"
        ready, _ = WaveTermForegroundAdapter._ready_prompt(draft)
        self.assertFalse(ready)

    def test_resolve_wsh_prefers_terminal_bindir(self):
        from model_fallback_proxy.waveterm_foreground import resolveWshPath

        with tempfile.TemporaryDirectory() as root:
            bindir = Path(root) / "bin"
            bindir.mkdir()
            (bindir / "wsh").write_text("", encoding="utf-8")
            self.assertEqual(resolveWshPath({"WAVETERM_WSHBINDIR": str(bindir)}), str(bindir / "wsh"))
        # 无 bindir 提示且默认路径不存在时返回空串。
        with tempfile.TemporaryDirectory() as home:
            with patch("model_fallback_proxy.waveterm_foreground.Path.home", return_value=Path(home)), patch(
                "model_fallback_proxy.waveterm_foreground.shutil.which", return_value=None
            ):
                self.assertEqual(resolveWshPath({}), "")

    def test_native_transport_uses_handshake_and_controllerinput(self):
        from model_fallback_proxy.waveterm_foreground import WaveTermNativeTransport

        with patch("model_fallback_proxy.waveterm_foreground.socket.socket", FakeSocket):
            transport = WaveTermNativeTransport(socket_path="/tmp/fake-wave.sock", token="jwt")
            with patch.object(Path, "exists", return_value=True):
                self.assertTrue(transport.controller_input("b1", "5L+XlQ=="))
        requests = FakeSocket.last_requests[-3:]
        self.assertEqual([request["command"] for request in requests], ["authenticate", "setpeerinfo", "controllerinput"])
        self.assertEqual(requests[0]["route"], "$control")
        self.assertEqual(requests[1]["data"], "domain:block:b1")
        self.assertNotIn("route", requests[2])


if __name__ == "__main__":
    unittest.main()
