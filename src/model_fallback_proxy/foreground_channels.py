#!/usr/bin/env python3
"""前台续跑通道路由。

WaveTerm 使用原生 socket；普通终端优先使用经过校验的 tmux pane，裸 PTY
仅在内核允许 TIOCSTI 且显式开启时注入；虚拟员工必须提供显式 hook。
"""
from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import termios
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

try:
    from .waveterm_foreground import (
        ANSI_ESCAPE_RE,
        PAYLOAD,
        WaveTermCandidate,
        WaveTermForegroundAdapter,
        WaveTermNativeTransport,
        findSessionBlockId,
    )
except ImportError:  # 兼容直接执行旧脚本入口。
    from waveterm_foreground import (
        ANSI_ESCAPE_RE,
        PAYLOAD,
        WaveTermCandidate,
        WaveTermForegroundAdapter,
        WaveTermNativeTransport,
        findSessionBlockId,
    )


CHANNEL_AUTO = "auto"
CHANNEL_WAVETERM = "waveterm"
CHANNEL_TERMINAL = "terminal"
CHANNEL_VIRTUAL_EMPLOYEE = "virtual_employee"
VALID_CHANNELS = {CHANNEL_AUTO, CHANNEL_WAVETERM, CHANNEL_TERMINAL, CHANNEL_VIRTUAL_EMPLOYEE}
SESSION_ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)
PTY_PREFIXES = ("/dev/pts/", "/dev/tty")
TIOCSTI = getattr(termios, "TIOCSTI", 0x5412)


def is_enabled(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _read_proc_env(entry: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        for item in (entry / "environ").read_bytes().split(b"\0"):
            if b"=" not in item:
                continue
            key, value = item.split(b"=", 1)
            result[key.decode(errors="ignore")] = value.decode(errors="ignore")
    except (OSError, ValueError):
        return {}
    return result


def _read_starttime(pid: int) -> str:
    """读取 Linux proc starttime，防止 PID 重用误写。"""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        remainder = raw.rsplit(")", 1)[1].split()
        return remainder[19]
    except (OSError, IndexError, ValueError):
        return ""


def _read_cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="ignore").strip()
    except OSError:
        return ""


def discover_terminal_candidates() -> list[WaveTermCandidate]:
    """发现非 WaveTerm 的 Codex PTY；WaveTerm 进程由原生通道优先处理。"""
    candidates: dict[tuple[str, str, str], WaveTermCandidate] = {}
    for entry in Path("/proc").glob("[0-9]*"):
        try:
            pid = int(entry.name)
            env = _read_proc_env(entry)
            if env.get("WAVETERM_BLOCKID") and env.get("WAVETERM_JWT"):
                continue
            cmdline = _read_cmdline(pid)
            if "codex" not in cmdline.lower():
                continue
            pty_path = os.readlink(entry / "fd/0")
            if not pty_path.startswith(PTY_PREFIXES):
                continue
            cwd = os.readlink(entry / "cwd")
            session_match = SESSION_ID_RE.search(cmdline)
            session_id = session_match.group(0) if session_match else ""
            candidate = WaveTermCandidate(
                block_id="",
                pty_path=pty_path,
                cwd=cwd,
                pid=pid,
                online=True,
                session_id=session_id,
                channel=CHANNEL_TERMINAL,
                starttime=_read_starttime(pid),
                tmux_target=env.get("TMUX_PANE", ""),
            )
            key = (pty_path, cwd, session_id)
            previous = candidates.get(key)
            # node 包装进程和原生 Codex 子进程同属一条输入链，保留原生子进程。
            native_process = "/@openai/codex/" in cmdline.lower() or "codex-linux" in cmdline.lower()
            previous_cmdline = _read_cmdline(previous.pid).lower() if previous and previous.pid else ""
            previous_native = "/@openai/codex/" in previous_cmdline or "codex-linux" in previous_cmdline
            if previous is None or (native_process and not previous_native):
                candidates[key] = candidate
        except (OSError, ValueError):
            continue
    return list(candidates.values())


class ControlledPtyTransport:
    """只向已验证的目标终端注入固定 payload。"""

    def __init__(
        self,
        state_root: Path | str | None = None,
        *,
        direct_enabled: bool | None = None,
        tmux_enabled: bool = True,
        writer: Any | None = None,
        runner: Any = subprocess.run,
    ) -> None:
        self.state_root = Path(state_root or "~/.local/state/codex-session-auto-continue").expanduser()
        self.direct_enabled = is_enabled(os.environ.get("CODEX_AUTO_CONTINUE_TERMINAL_PTY"), False) if direct_enabled is None else direct_enabled
        self.tmux_enabled = tmux_enabled
        self.writer = writer
        self.runner = runner

    def controller_input(
        self,
        _block_id: str,
        input_data64: str,
        _auth_token: str | None = None,
        candidate: WaveTermCandidate | None = None,
        _event_id: str = "",
    ) -> bool:
        if candidate is None or candidate.channel != CHANNEL_TERMINAL:
            return False
        try:
            payload = base64.b64decode(input_data64, validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return False
        if payload != PAYLOAD or not self._verify_identity(candidate):
            return False
        with self._target_lock(candidate) as locked:
            if not locked:
                return False
            if candidate.tmux_target and self.tmux_enabled and self._send_tmux(candidate):
                return True
            if not self.direct_enabled:
                return False
            return self._write_verified_stdin(candidate, payload.encode("utf-8"))

    def _verify_identity(self, candidate: WaveTermCandidate) -> bool:
        if candidate.pid is None or not candidate.starttime or not candidate.pty_path or not candidate.cwd:
            return False
        if _read_starttime(candidate.pid) != candidate.starttime:
            return False
        try:
            if os.readlink(f"/proc/{candidate.pid}/fd/0") != candidate.pty_path:
                return False
            if os.readlink(f"/proc/{candidate.pid}/cwd") != candidate.cwd:
                return False
        except OSError:
            return False
        cmdline = _read_cmdline(candidate.pid)
        if "codex" not in cmdline.lower():
            return False
        return not candidate.session_id or candidate.session_id.lower() in cmdline.lower()

    @contextmanager
    def _target_lock(self, candidate: WaveTermCandidate) -> Iterator[bool]:
        key = "|".join((str(candidate.pid), candidate.starttime, candidate.pty_path, candidate.cwd)).encode("utf-8")
        lock_path = self.state_root / "locks" / f"{hashlib.sha256(key).hexdigest()}.lock"
        handle = None
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = lock_path.open("a+", encoding="ascii")
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield True
        except (OSError, IOError):
            yield False
        finally:
            if handle is not None:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                finally:
                    handle.close()

    def _send_tmux(self, candidate: WaveTermCandidate) -> bool:
        if not re.fullmatch(r"%[0-9]+", candidate.tmux_target):
            return False
        try:
            check = self.runner(
                ["tmux", "display-message", "-p", "-t", candidate.tmux_target, "#{pane_tty}"],
                text=True,
                capture_output=True,
                timeout=2,
                check=False,
            )
            if check.returncode != 0 or check.stdout.strip() != candidate.pty_path:
                return False
            result = self.runner(
                ["tmux", "send-keys", "-t", candidate.tmux_target, "继续", "C-m"],
                text=True,
                capture_output=True,
                timeout=2,
                check=False,
            )
            return result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def _write_verified_stdin(self, candidate: WaveTermCandidate, payload: bytes) -> bool:
        """用 TIOCSTI 注入输入；不能直接写 slave，否则只会输出到屏幕。"""
        if self.writer is not None:
            try:
                return bool(self.writer(candidate, payload))
            except (OSError, IOError):
                return False
        fd = -1
        try:
            fd = os.open(candidate.pty_path, os.O_RDWR | os.O_NOCTTY)
            for byte in payload:
                fcntl.ioctl(fd, TIOCSTI, bytes((byte,)))
            return True
        except (OSError, IOError):
            return False
        finally:
            if fd >= 0:
                os.close(fd)


class VirtualEmployeeHookTransport:
    """通过显式 hook 把可见“继续”交给虚拟员工前端。"""

    def __init__(self, hook_command: str | Sequence[str] | None = None, *, timeout: float = 4.0, runner: Any = subprocess.run) -> None:
        raw = hook_command if hook_command is not None else os.environ.get("CODEX_AUTO_CONTINUE_VIRTUAL_EMPLOYEE_HOOK", "")
        if isinstance(raw, str):
            try:
                self.argv = tuple(shlex.split(raw))
            except ValueError:
                self.argv = ()
        else:
            self.argv = tuple(str(item) for item in raw)
        self.timeout = timeout
        self.runner = runner

    @property
    def configured(self) -> bool:
        if not self.argv:
            return False
        command_path = Path(self.argv[0])
        if command_path.exists():
            return command_path.is_file() and os.access(command_path, os.X_OK)
        return bool(shutil.which(self.argv[0]))

    def controller_input(
        self,
        _block_id: str,
        input_data64: str,
        _auth_token: str | None = None,
        candidate: WaveTermCandidate | None = None,
        event_id: str = "",
    ) -> bool:
        if candidate is None or candidate.channel != CHANNEL_VIRTUAL_EMPLOYEE or not self.configured:
            return False
        try:
            payload = base64.b64decode(input_data64, validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return False
        if payload != PAYLOAD:
            return False
        body = {
            "protocol": 1,
            "action": "continue",
            "event_id": event_id,
            "session_id": candidate.session_id,
            "hook_id": candidate.hook_id,
            "cwd": candidate.cwd,
            "payload": PAYLOAD,
            "input_data64": input_data64,
        }
        try:
            result = self.runner(
                list(self.argv),
                input=json.dumps(body, ensure_ascii=False),
                text=True,
                capture_output=True,
                timeout=self.timeout,
                check=False,
            )
            return result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False


class FrontendTransportRouter:
    def __init__(self, *, state_root: Path | str | None = None, terminal_direct: bool | None = None, virtual_hook: str | Sequence[str] | None = None) -> None:
        self.wave = WaveTermNativeTransport()
        self.terminal = ControlledPtyTransport(state_root, direct_enabled=terminal_direct)
        self.virtual = VirtualEmployeeHookTransport(virtual_hook)

    def controller_input(
        self,
        block_id: str,
        input_data64: str,
        auth_token: str | None = None,
        candidate: WaveTermCandidate | None = None,
        event_id: str = "",
    ) -> bool:
        if candidate is None:
            return False
        if candidate.channel == CHANNEL_WAVETERM:
            return self.wave.controller_input(block_id, input_data64, auth_token, candidate, event_id)
        if candidate.channel == CHANNEL_TERMINAL:
            return self.terminal.controller_input(block_id, input_data64, auth_token, candidate, event_id)
        if candidate.channel == CHANNEL_VIRTUAL_EMPLOYEE:
            return self.virtual.controller_input(block_id, input_data64, auth_token, candidate, event_id)
        return False


class MultiFrontendAdapter:
    """按请求/配置选择 WaveTerm、普通终端或虚拟员工通道。"""

    def __init__(
        self,
        *,
        channel: str | None = None,
        observe_only: bool = True,
        state_path: Path | str | None = None,
        terminal_direct: bool | None = None,
        terminal_assume_ready: bool | None = None,
        virtual_hook: str | Sequence[str] | None = None,
        sleep_fn: Any | None = None,
        snapshot_root: Path | str | None = None,
    ) -> None:
        selected = str(channel or os.environ.get("CODEX_AUTO_CONTINUE_FRONTEND_CHANNEL", CHANNEL_AUTO)).strip().lower()
        self.channel = selected if selected in VALID_CHANNELS else CHANNEL_AUTO
        self.observe_only = observe_only
        self.terminal_assume_ready = is_enabled(os.environ.get("CODEX_AUTO_CONTINUE_TERMINAL_ASSUME_READY"), False) if terminal_assume_ready is None else terminal_assume_ready
        self.transport = FrontendTransportRouter(
            state_root=Path(state_path).parent if state_path else None,
            terminal_direct=terminal_direct,
            virtual_hook=virtual_hook,
        )
        self.adapter = WaveTermForegroundAdapter(
            transport=self.transport,
            observe_only=observe_only,
            state_path=state_path,
            sleep_fn=sleep_fn,
            snapshot_root=snapshot_root,
        )
        self.sleep_fn = sleep_fn or __import__("time").sleep

    def discover_candidates(self, channel: str | None = None) -> list[WaveTermCandidate]:
        selected = channel or self.channel
        if selected == CHANNEL_WAVETERM:
            return WaveTermForegroundAdapter.discover_candidates()
        if selected == CHANNEL_TERMINAL:
            return discover_terminal_candidates()
        if selected == CHANNEL_AUTO:
            return WaveTermForegroundAdapter.discover_candidates() + discover_terminal_candidates()
        return []

    def _virtual_candidate(self, request: dict[str, Any]) -> WaveTermCandidate | None:
        if not self.transport.virtual.configured:
            return None
        metadata = request.get("virtual_employee")
        if not isinstance(metadata, dict) and self.channel != CHANNEL_VIRTUAL_EMPLOYEE:
            return None
        metadata = metadata if isinstance(metadata, dict) else {}
        file_state = request.get("file_state") if isinstance(request.get("file_state"), dict) else {}
        return WaveTermCandidate(
            block_id="",
            cwd=str(request.get("cwd") or file_state.get("cwd") or ""),
            session_id=str(request.get("session_id") or metadata.get("session_id") or ""),
            channel=CHANNEL_VIRTUAL_EMPLOYEE,
            hook_id=str(metadata.get("hook_id") or metadata.get("job_id") or ""),
        )

    def _read_prompt(self, target: WaveTermCandidate, request: dict[str, Any]) -> str:
        if target.channel == CHANNEL_WAVETERM:
            return self.adapter.read_prompt(target)
        supplied = str(request.get("prompt") or "")
        if supplied:
            return supplied
        if target.channel == CHANNEL_TERMINAL and target.tmux_target:
            try:
                result = subprocess.run(
                    ["tmux", "capture-pane", "-p", "-t", target.tmux_target, "-S", "-80"],
                    text=True,
                    capture_output=True,
                    timeout=2,
                    check=False,
                )
                if result.returncode == 0:
                    return ANSI_ESCAPE_RE.sub("", result.stdout[-1600:])
            except (OSError, subprocess.SubprocessError):
                return ""
        if target.channel == CHANNEL_TERMINAL and self.terminal_assume_ready:
            return "›"
        if target.channel == CHANNEL_VIRTUAL_EMPLOYEE:
            metadata = request.get("virtual_employee")
            # hook 本身就是虚拟员工的可见前端/审批边界；未明确标记 ready=false 时才允许发送。
            if self.transport.virtual.configured and not (isinstance(metadata, dict) and metadata.get("ready") is False):
                return "›"
        return ""

    def continueSession(self, request: dict[str, Any]) -> dict[str, Any]:
        requested_channel = str(request.get("frontend_channel") or self.channel).strip().lower()
        if requested_channel not in VALID_CHANNELS:
            return {"event_id": request.get("event_id", ""), "status": "observe_only", "reason": "unknown_channel"}
        virtual_target = self._virtual_candidate(request) if requested_channel in {CHANNEL_AUTO, CHANNEL_VIRTUAL_EMPLOYEE} else None
        candidates = self.discover_candidates(requested_channel)
        if virtual_target is not None:
            candidates.append(virtual_target)
        session_id = str(request.get("session_id") or "")
        turn_id = str(request.get("turn_id") or "")
        event_id = str(request.get("event_id") or f"{session_id}:{turn_id}").strip(":")
        file_state = request.get("file_state") if isinstance(request.get("file_state"), dict) else {}
        cwd = str(request.get("cwd") or file_state.get("cwd") or "")
        block_id_hint = str(request.get("waveterm_block_id") or request.get("block_id") or "").strip()
        if not block_id_hint:
            block_id_hint = findSessionBlockId(session_id)
        if session_id:
            exact = [candidate for candidate in candidates if candidate.session_id == session_id]
            candidates = exact or [candidate for candidate in candidates if not candidate.session_id and (not cwd or candidate.cwd == cwd)]
            if not exact and block_id_hint:
                hinted = [candidate for candidate in candidates if candidate.block_id == block_id_hint]
                if hinted:
                    candidates = hinted
        wanted = request.get("target") if isinstance(request.get("target"), dict) else {}
        target = self.adapter.find_unique_target(candidates, pty_path=str(wanted.get("pty_path") or request.get("pty_path") or ""), cwd=cwd, pid=request.get("pid"))
        if target is None and len(candidates) == 1:
            candidate = candidates[0]
            if (not session_id or candidate.session_id in {"", session_id}) and (not cwd or candidate.cwd == cwd):
                target = candidate
        prompt = self._read_prompt(target, request) if target is not None else ""
        strong_identity = bool(block_id_hint or (session_id and target is not None and target.session_id == session_id))
        if target is not None and request.get("force_continue") and strong_identity and not self.observe_only:
            return self.adapter.inject(event_id, target)
        first = self.adapter.observe(event_id, target, prompt)
        if first.get("reason") == "prompt_not_stable" and target is not None:
            self.sleep_fn(1.0)
            return self.adapter.observe(event_id, target, self._read_prompt(target, request))
        return first
