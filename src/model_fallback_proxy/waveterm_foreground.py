#!/usr/bin/env python3
"""WaveTerm 前台输入适配器。

默认只观察，不向终端注入。真实发送仅使用 WaveTerm 原生
``ControllerInputCommand`` 接口，禁止直接写入任意 ``/dev/pts``。
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import socket
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence


PAYLOAD = "继续\r"
BACKOFF_SECONDS = (30, 60, 120, 240, 300)
WSH_SCROLLBACK_TIMEOUT_SECONDS = 6.0
SCROLLBACK_TAIL_CHARS = 4000
ANSI_ESCAPE_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
SESSION_ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)
BLOCK_ID_LINE_RE = re.compile(
    r"declare -x WAVETERM_BLOCKID=(?:\"([^\"]+)\"|'([^']+)')$",
    re.IGNORECASE,
)


def findSessionBlockId(session_id: str, snapshot_root: Path | str | None = None) -> str:
    """从 Codex shell snapshot 恢复会话所属 WaveTerm block。只读取 block id，不读取 JWT。"""
    if not SESSION_ID_RE.fullmatch(str(session_id or "").strip()):
        return ""
    root = Path(snapshot_root or "~/.codex/shell_snapshots").expanduser()
    try:
        paths = sorted(root.glob(f"{session_id}.*.sh"), key=lambda item: item.stat().st_mtime, reverse=True)
    except OSError:
        return ""
    for path in paths:
        try:
            for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                match = BLOCK_ID_LINE_RE.fullmatch(line.strip())
                if not match:
                    continue
                block_id = str(match.group(1) or match.group(2) or "").strip()
                if SESSION_ID_RE.fullmatch(block_id):
                    return block_id
        except OSError:
            continue
    # 兜底：直接从持有该会话文件的 Codex 进程恢复 block id。
    return findSessionBlockIdViaProc(session_id)


def findSessionBlockIdViaProc(session_id: str) -> str:
    """扫描 /proc，从打开该会话 jsonl 的进程环境里取 WAVETERM_BLOCKID。"""
    session_id = str(session_id or "").strip()
    if not SESSION_ID_RE.fullmatch(session_id):
        return ""
    for entry in Path("/proc").glob("[0-9]*"):
        try:
            fds = entry / "fd"
            for fd in fds.iterdir():
                try:
                    link = os.readlink(fd)
                except (OSError, TypeError):
                    continue
                if not isinstance(link, str):
                    continue
                name = Path(link).name
                if not name.endswith(".jsonl") or session_id not in name:
                    continue
                env_block = readProcEnvValue(entry, "WAVETERM_BLOCKID")
                if SESSION_ID_RE.fullmatch(env_block):
                    return env_block
        except (OSError, TypeError, ValueError):
            continue
    return ""


def readProcEnvValue(entry: Path, key: str) -> str:
    """读取指定进程环境变量；失败返回空串。"""
    try:
        for item in (entry / "environ").read_bytes().split(b"\0"):
            if b"=" not in item:
                continue
            name, value = item.split(b"=", 1)
            if name.decode(errors="ignore") == key:
                return value.decode(errors="ignore").strip()
    except OSError:
        return ""
    return ""


def resolveWshPath(env: Mapping[str, str]) -> str:
    """解析 wsh 可执行文件：优先目标终端携带的 WAVETERM_WSHBINDIR，再查常见安装目录。"""
    candidates: list[Path] = []
    bindir = str(env.get("WAVETERM_WSHBINDIR") or "").strip()
    if bindir:
        candidates.append(Path(bindir) / "wsh")
    candidates.append(Path.home() / ".local" / "share" / "waveterm" / "bin" / "wsh")
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return shutil.which("wsh") or ""


# 新版 TUI 的可输入提示符：空输入框显示“› Ask Codex to do anything”，
# 忙碌时该提示符同样存在，因此还要靠上方的运行态指示区分（见 isBusyIndicatorLine）。
PROMPT_MARKERS = ("›", "»", "❯")
PROMPT_PLACEHOLDERS = ("ask codex",)
BUSY_INDICATORS = ("esc to interrupt", "• working", "◦ working", "tab to queue message")
# 忙碌行可能因终端宽度被截断为“(2h…”，用耗时模式兜底识别。
BUSY_ELAPSED_RE = re.compile(r"\(\d+\s*[hms](?:\s|\u2026|•|\)|$)")


def isPromptMarkerChar(line: str) -> bool:
    """判断一行是否以输入提示符字符开头（可能带用户草稿文本）。"""
    stripped = line.strip()
    return bool(stripped) and stripped[0] in PROMPT_MARKERS


def isPromptPlaceholderLine(line: str) -> bool:
    """判断提示符行是否为“空输入框”（无草稿文本，或只有默认占位文案）。"""
    stripped = line.strip()
    if not stripped or stripped[0] not in PROMPT_MARKERS:
        return False
    rest = stripped[1:].strip().lower()
    if not rest:
        return True
    return any(rest.startswith(placeholder) for placeholder in PROMPT_PLACEHOLDERS)


def isBusyIndicatorLine(line: str) -> bool:
    """判断一行是否表示当前回合仍在运行（工作中指示、排队提示、被截断的耗时标记等）。"""
    lowered = line.lower()
    if any(indicator in lowered for indicator in BUSY_INDICATORS):
        return True
    return bool(BUSY_ELAPSED_RE.search(line))


@dataclass(frozen=True)
class WaveTermCandidate:
    block_id: str
    pty_path: str = ""
    cwd: str = ""
    pid: int | None = None
    online: bool = True
    session_id: str = ""
    auth_token: str = field(default="", repr=False, compare=False)
    # 通道扩展字段。WaveTerm 继续使用 block_id/auth_token，其它通道使用身份校验字段。
    channel: str = "waveterm"
    starttime: str = ""
    tmux_target: str = ""
    hook_id: str = ""


@dataclass
class InjectionRecord:
    event_id: str
    status: str
    attempts: int = 0
    next_retry_at: float = 0.0
    error: str = ""
    updated_at: float = field(default_factory=time.time)


class WaveTermTransport(Protocol):
    def controller_input(
        self,
        block_id: str,
        input_data64: str,
        auth_token: str | None = None,
        candidate: WaveTermCandidate | None = None,
        event_id: str = "",
    ) -> bool:
        """向指定 block 发送一次原生输入命令。"""


class WaveTermNativeTransport:
    """WaveTerm Unix socket transport，按原生命令协议发送输入。"""

    def __init__(self, socket_path: Path | str | None = None, token: str | None = None, timeout: float = 2.0) -> None:
        self.socket_path = Path(socket_path or "~/.local/share/waveterm/wave.sock").expanduser()
        # SWAPTOKEN 是一次性前端交换信封，不能用于服务端 RPC 认证。
        self.token = token if token is not None else os.environ.get("WAVETERM_JWT", "")
        self.timeout = timeout

    def controller_input(
        self,
        block_id: str,
        input_data64: str,
        auth_token: str | None = None,
        candidate: WaveTermCandidate | None = None,
        event_id: str = "",
    ) -> bool:
        token = auth_token or self.token
        if not block_id or not self.socket_path.exists() or not token:
            return False
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(self.timeout)
                client.connect(str(self.socket_path))
                # 握手和业务命令共用一条 JSONL 连接；每个请求必须等到对应 resid。
                requests = [
                    {"command": "authenticate", "data": token, "reqid": str(uuid.uuid4()), "route": "$control", "timeout": 5000},
                    {"command": "setpeerinfo", "data": f"domain:block:{block_id}", "reqid": str(uuid.uuid4()), "route": "$control", "timeout": 5000},
                    {"command": "controllerinput", "data": {"blockid": block_id, "inputdata64": input_data64}, "reqid": str(uuid.uuid4()), "timeout": 2000},
                ]
                for request in requests:
                    if not self._round_trip(client, request):
                        return False
            return True
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False

    def _round_trip(self, client: socket.socket, request: dict[str, Any]) -> bool:
        request_id = request["reqid"]
        client.sendall((json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"))
        buffer = b""
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            chunk = client.recv(8192)
            if not chunk:
                return False
            buffer += chunk
            while b"\n" in buffer:
                raw, buffer = buffer.split(b"\n", 1)
                if not raw.strip():
                    continue
                parsed = json.loads(raw.decode("utf-8"))
                response_id = parsed.get("resid", parsed.get("reqid"))
                if response_id != request_id:
                    continue
                if parsed.get("error") or parsed.get("ok") is False or parsed.get("success") is False:
                    return False
                data = parsed.get("data")
                return not (isinstance(data, dict) and (data.get("error") or data.get("ok") is False))
        return False


class WaveTermForegroundAdapter:
    """候选发现、稳定提示确认、幂等注入及失败退避。"""

    def __init__(
        self,
        transport: WaveTermTransport | None = None,
        *,
        observe_only: bool = True,
        state_path: Path | str | None = None,
        clock: Any = time.time,
        discover: Any | None = None,
        prompt_reader: Any | None = None,
        sleep_fn: Any | None = None,
        snapshot_root: Path | str | None = None,
    ) -> None:
        self.transport = transport or WaveTermNativeTransport()
        self.observe_only = observe_only
        self.state_path = Path(state_path) if state_path else None
        self.clock = clock
        self.discover = discover or self.discover_candidates
        self.prompt_reader = prompt_reader or self.read_prompt
        self.sleep_fn = sleep_fn or time.sleep
        self.snapshot_root = Path(snapshot_root).expanduser() if snapshot_root else None
        self.records: dict[str, InjectionRecord] = {}
        self._samples: dict[str, tuple[str, str, float]] = {}
        self.audit: list[dict[str, Any]] = []
        self._load_state()

    @staticmethod
    def discover_candidates() -> list[WaveTermCandidate]:
        candidates: dict[tuple[str, str, str], WaveTermCandidate] = {}
        for entry in Path("/proc").glob("[0-9]*"):
            try:
                env = {}
                for item in (entry / "environ").read_bytes().split(b"\0"):
                    if b"=" in item:
                        key, value = item.split(b"=", 1)
                        env[key.decode(errors="ignore")] = value.decode(errors="ignore")
                block_id = env.get("WAVETERM_BLOCKID", "")
                token = env.get("WAVETERM_JWT", "")
                if not block_id or not token:
                    continue
                pid = int(entry.name)
                cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="ignore").strip()
                if "codex" not in cmdline.lower():
                    continue
                pty = os.readlink(entry / "fd/0")
                if not pty.startswith(("/dev/pts/", "/dev/tty")):
                    continue
                cwd = os.readlink(entry / "cwd")
                session_match = re.search(
                    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                    cmdline,
                    re.IGNORECASE,
                )
                session_id = session_match.group(0) if session_match else ""
                candidate = WaveTermCandidate(block_id, pty, cwd, pid, True, session_id, token)
                key = (block_id, pty, session_id)
                previous = candidates.get(key)
                # 同一个 Codex 由 node 包装进程和原生子进程组成，优先保留原生子进程。
                native_process = "/@openai/codex/" in cmdline.lower() or "codex-linux" in cmdline.lower()
                previous_cmdline = ""
                if previous is not None and previous.pid is not None:
                    try:
                        previous_cmdline = (Path(f"/proc/{previous.pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="ignore")).lower()
                    except OSError:
                        previous_cmdline = ""
                previous_native = "/@openai/codex/" in previous_cmdline or "codex-linux" in previous_cmdline
                if previous is None or (native_process and not previous_native):
                    candidates[key] = candidate
            except (OSError, ValueError):
                continue
        return list(candidates.values())

    @staticmethod
    def read_prompt(target: WaveTermCandidate) -> str:
        """读取 WaveTerm 原生 scrollback；失败时返回空串，禁止猜测可写目标。"""
        if target.pid is None or not target.block_id:
            return ""
        try:
            env = dict(os.environ)
            for item in (Path(f"/proc/{target.pid}/environ").read_bytes().split(b"\0")):
                if b"=" not in item:
                    continue
                key, value = item.split(b"=", 1)
                key = key.decode(errors="ignore")
                if key.startswith("WAVETERM_"):
                    env[key] = value.decode(errors="ignore")
            # systemd 服务环境通常没有 wsh 在 PATH；优先用目标终端的
            # WAVETERM_WSHBINDIR 与常见安装路径，最后才回落 PATH 探测。
            wsh = (
                os.environ.get("WAVETERM_WSH")
                or env.get("WAVETERM_WSH")
                or resolveWshPath(env)
                or "wsh"
            )
            result = subprocess.run(
                [wsh, "termscrollback", "-b", target.block_id],
                cwd=target.cwd or None,
                env=env,
                text=True,
                capture_output=True,
                timeout=WSH_SCROLLBACK_TIMEOUT_SECONDS,
                check=False,
            )
            if result.returncode != 0:
                return ""
            # 只保留末尾屏幕，避免历史文本中的 commit/delete 等词误触安全门禁；
            # 4000 字符保证“工作中”标志行（esc to interrupt）不会被长行截断切掉。
            return ANSI_ESCAPE_RE.sub("", result.stdout[-SCROLLBACK_TAIL_CHARS:])
        except (OSError, subprocess.SubprocessError, UnicodeError):
            return ""

    def continueSession(self, request: dict[str, Any]) -> dict[str, Any]:
        session_id = str(request.get("session_id") or "")
        turn_id = str(request.get("turn_id") or "")
        event_id = str(request.get("event_id") or f"{session_id}:{turn_id}").strip(":")
        candidates = list(self.discover())
        requested_session = session_id
        file_state = request.get("file_state") if isinstance(request.get("file_state"), dict) else {}
        cwd = str(request.get("cwd") or file_state.get("cwd") or "")
        block_id_hint = str(request.get("waveterm_block_id") or request.get("block_id") or "").strip()
        if not block_id_hint:
            block_id_hint = findSessionBlockId(requested_session, self.snapshot_root)
        if requested_session:
            exact = [candidate for candidate in candidates if candidate.session_id == requested_session]
            # 交互 TUI 的 argv 不带 session UUID；只允许用 cwd 唯一候选补充定位。
            candidates = exact or [candidate for candidate in candidates if not candidate.session_id and (not cwd or candidate.cwd == cwd)]
            if not exact and block_id_hint:
                hinted = [candidate for candidate in candidates if candidate.block_id == block_id_hint]
                if hinted:
                    candidates = hinted
        wanted = request.get("target") or {}
        target = self.find_unique_target(candidates, pty_path=wanted.get("pty_path", request.get("pty_path", "")), cwd=cwd, pid=request.get("pid"))
        if target is None and len(candidates) == 1:
            candidate = candidates[0]
            session_ok = not requested_session or candidate.session_id in {"", requested_session}
            cwd_ok = not cwd or candidate.cwd == cwd
            if session_ok and cwd_ok:
                target = candidate
        prompt = str(request.get("prompt", ""))
        if target is not None:
            prompt = str(self.prompt_reader(target))
        strong_identity = bool(block_id_hint or (requested_session and target is not None and target.session_id == requested_session))
        if target is not None and request.get("force_continue") and strong_identity and not self.observe_only:
            return self.inject(event_id, target)
        first = self.observe(event_id, target, prompt)
        if first.get("reason") == "prompt_not_stable" and target is not None:
            self.sleep_fn(1.0)
            prompt = str(self.prompt_reader(target))
            return self.observe(event_id, target, prompt)
        return first

    def find_unique_target(self, candidates: Sequence[WaveTermCandidate], *, pty_path: str = "", cwd: str = "", pid: int | None = None) -> WaveTermCandidate | None:
        """按 PTY 优先、再按 cwd+pid 匹配；结果必须唯一。"""
        available = [candidate for candidate in candidates if candidate.online]
        if pty_path:
            matches = [candidate for candidate in available if candidate.pty_path == pty_path]
        else:
            matches = available
        if len(matches) != 1 and cwd:
            matches = [candidate for candidate in available if candidate.cwd == cwd]
        if len(matches) != 1 and cwd and pid is not None:
            matches = [candidate for candidate in matches if candidate.pid == pid]
        return matches[0] if len(matches) == 1 else None

    def observe(self, event_id: str, target: WaveTermCandidate | None, prompt_text: str, *, now: float | None = None) -> dict[str, Any]:
        now = self.clock() if now is None else now
        if target is None:
            return self._audit(event_id, "observe_only", reason="target_not_unique")
        if self._has_unsafe_confirmation(prompt_text):
            self._samples[event_id] = (target.block_id, "unsafe", now)
            return self._audit(event_id, "observe_only", reason="unsafe_prompt")
        ready, context = self._ready_prompt(prompt_text)
        if not ready:
            fingerprint = prompt_text.strip()[-1600:]
            self._samples[event_id] = (target.block_id, fingerprint, now)
            return self._audit(event_id, "observe_only", reason="prompt_not_ready")
        unsafe_markers = ("working", "approval", "approve", "confirm", "permission", "delete", "publish", "commit")
        if any(marker in context.lower() for marker in unsafe_markers):
            self._samples[event_id] = (target.block_id, "unsafe", now)
            return self._audit(event_id, "observe_only", reason="unsafe_prompt")
        # 只用“可输入提示符”作为指纹，忽略底部 context 百分比等动态状态栏。
        fingerprint = "ready"
        previous = self._samples.get(event_id)
        stable = bool(previous and previous[0] == target.block_id and previous[1] == fingerprint and now - previous[2] >= 1.0)
        self._samples[event_id] = (target.block_id, fingerprint, now)
        if not stable:
            return self._audit(event_id, "observe_only", reason="prompt_not_stable")
        if self.observe_only:
            return self._audit(event_id, "observe_only", reason="observe_only")
        return self.inject(event_id, target, now=now)

    @staticmethod
    def _has_unsafe_confirmation(prompt_text: str) -> bool:
        lines = [ANSI_ESCAPE_RE.sub("", line).strip().lower() for line in prompt_text.splitlines()]
        confirmation_words = ("确认", "审批", "批准", "允许", "approve", "approval", "permission", "confirm")
        for line in lines[-16:]:
            if not any(word in line for word in confirmation_words):
                continue
            if any(mark in line for mark in ("?", "？", "吗", "么", "是否")):
                return True
        return False

    @staticmethod
    def _ready_prompt(prompt_text: str) -> tuple[bool, str]:
        lines = [ANSI_ESCAPE_RE.sub("", line).rstrip() for line in prompt_text.splitlines()]
        window = lines[-40:]
        marker_index = -1
        # 真提示符只可能出现在窗口底部，避免把历史正文里的“›”当成输入框。
        for index in range(len(window) - 1, max(len(window) - 13, -1), -1):
            if isPromptMarkerChar(window[index]):
                marker_index = index
                break
        if marker_index < 0:
            return False, ""
        # 提示符行上已存在用户草稿文本时不允许注入，避免覆盖或误发。
        if not isPromptPlaceholderLine(window[marker_index]):
            return False, ""
        # 忙碌窗口：`esc to interrupt`、`• Working (...)` 说明当前回合未结束；
        # 此时注入会排入当前回合并产生多余输入，判定为未就绪（不影响进行中的对话）。
        if any(isBusyIndicatorLine(line) for line in window[-16:]):
            return False, ""
        # 审批/确认界面不会出现空的可输入提示符；只检查提示符后的状态栏，避免历史文本误报。
        suffix = [line.strip() for line in window[marker_index + 1:] if line.strip()]
        return True, "\n".join(suffix)

    def inject(self, event_id: str, target: WaveTermCandidate, *, now: float | None = None) -> dict[str, Any]:
        now = self.clock() if now is None else now
        prior = self.records.get(event_id)
        if prior and prior.status in {"injected", "confirmed"}:
            return self._audit(event_id, prior.status, reason="idempotent")
        if prior and now < prior.next_retry_at:
            return self._audit(event_id, "backoff", reason="retry_deferred")
        encoded = base64.b64encode(PAYLOAD.encode("utf-8")).decode("ascii")
        sender = getattr(self.transport, "controller_input", None)
        if sender is None:
            sender = getattr(self.transport, "send_controller_input", None)
        try:
            ok = bool(sender and sender(target.block_id, encoded, target.auth_token, target, event_id))
        except TypeError:
            try:
                ok = bool(sender and sender(target.block_id, encoded, target.auth_token, target))
            except TypeError:
                try:
                    ok = bool(sender and sender(target.block_id, encoded, target.auth_token))
                except TypeError:
                    ok = bool(sender and sender(target.block_id, encoded))
        if ok:
            self.records[event_id] = InjectionRecord(event_id, "injected", (prior.attempts if prior else 0) + 1, updated_at=now)
            self._persist_state()
            return self._audit(event_id, "injected")
        attempts = (prior.attempts if prior else 0) + 1
        delay = BACKOFF_SECONDS[min(attempts - 1, len(BACKOFF_SECONDS) - 1)]
        self.records[event_id] = InjectionRecord(event_id, "failed", attempts, now + delay, "transport_failed", now)
        self._persist_state()
        return self._audit(event_id, "failed", reason="transport_failed")

    def _audit(self, event_id: str, status: str, *, reason: str = "") -> dict[str, Any]:
        item = {"event_id": event_id, "status": status, "reason": reason, "at": self.clock()}
        self.audit.append(item)
        return item

    def _load_state(self) -> None:
        if not self.state_path or not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.records = {key: InjectionRecord(**value) for key, value in data.get("records", {}).items()}
        except (OSError, ValueError, TypeError):
            self.records = {}

    def _persist_state(self) -> None:
        if not self.state_path:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps({"records": {key: asdict(value) for key, value in self.records.items()}}, ensure_ascii=False, indent=2), encoding="utf-8")
