#!/usr/bin/env python3
"""Codex TUI 失败自动续跑监控器。"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable


DEFAULT_SESSION_ROOT = Path.home() / ".codex" / "sessions"
DEFAULT_STATE_PATH = Path.home() / ".local" / "state" / "codex-session-auto-continue" / "state.json"
DEFAULT_LOG_ROOT = Path.home() / ".local" / "state" / "codex-session-auto-continue" / "logs"
DEFAULT_EVENTS_PATH = Path.home() / ".local" / "state" / "codex-session-auto-continue" / "events.jsonl"
DEFAULT_CODEX_BIN = Path(shutil.which("codex") or "codex")
DEFAULT_PROMPT = "继续"
DEFAULT_REASONING_EFFORT = "xhigh"
DEFAULT_BACKGROUND_FALLBACK = True
DEFAULT_FEISHU_CHAT_ID = ""
DEFAULT_FEISHU_AS = "bot"
DEFAULT_LARK_CLI = Path("lark-cli")
FEISHU_NOTIFY_TIMEOUT_SECONDS = 15
DEFAULT_FEISHU_NOTIFY_INTERVAL_SECONDS = 600.0
FEISHU_NOTIFY_RETRY_SECONDS = 60.0
MAX_FEISHU_NOTIFICATION_ITEMS = 256
FAILURE_WINDOW_SECONDS = 600
FAILURE_HISTORY_LIMIT = 32
MAX_HANDLED_TURNS = 64
# codex_exec 是 RoadClaw/虚拟员工常用入口；未配置虚拟员工 hook 时仍只观察。
WATCHED_ORIGINATORS = {"codex-tui", "codex_exec", "virtual_employee", "roadclaw"}
MODEL_CHAIN = (
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
)
LEGACY_MODEL_ALIASES = {"gpt-5.5": "gpt-5.6-luna"}
CAPACITY_MARKERS = (
    "selected model is at capacity",
    "model is at capacity",
    "server_overloaded",
    "server overloaded",
    "no available accounts",
    "capacity exceeded",
    "capacity_exceeded",
)
MANUAL_TERMINATION_MARKERS = (
    "user aborted",
    "user_aborted",
    "user cancelled",
    "user_cancelled",
    "cancelled by user",
    "canceled by user",
    "cancelled_by_user",
    "canceled_by_user",
    "aborted by user",
    "interrupted by user",
    "用户取消",
    "用户中断",
    "手动终止",
)


def defaultState() -> dict[str, Any]:
    return {"initialized": False, "files": {}, "sessions": {}, "pending_failures": []}


def isManualTermination(error: Any) -> bool:
    if not error:
        return False
    if isinstance(error, dict):
        text = " ".join(str(value) for value in error.values())
    else:
        text = str(error)
    normalized = text.lower()
    return any(marker in normalized for marker in MANUAL_TERMINATION_MARKERS)


def isCapacityError(error: Any) -> bool:
    if not error:
        return False
    if isinstance(error, dict):
        text = " ".join(str(value) for value in error.values())
    else:
        text = str(error)
    normalized = text.lower()
    return any(marker in normalized for marker in CAPACITY_MARKERS)


def isFailureEvent(payload: dict[str, Any]) -> bool:
    return payload.get("type") == "task_complete" and bool(payload.get("error")) and not isManualTermination(payload.get("error"))


def getNextModel(model: str) -> str:
    current_model = LEGACY_MODEL_ALIASES.get(str(model or "").strip(), str(model or "").strip())
    if not current_model:
        return MODEL_CHAIN[0]
    try:
        current_index = MODEL_CHAIN.index(current_model)
    except ValueError:
        return current_model
    if current_index + 1 < len(MODEL_CHAIN):
        return MODEL_CHAIN[current_index + 1]
    return current_model


def getReasoningEffort(_file_state: dict[str, Any]) -> str:
    return DEFAULT_REASONING_EFFORT


def isEnabled(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def getFloatEnv(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


class SessionAutoContinue:
    def __init__(
        self,
        session_root: Path = DEFAULT_SESSION_ROOT,
        state_path: Path = DEFAULT_STATE_PATH,
        log_root: Path = DEFAULT_LOG_ROOT,
        events_path: Path = DEFAULT_EVENTS_PATH,
        codex_bin: Path = DEFAULT_CODEX_BIN,
        poll_seconds: float = 2.0,
        process_factory: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
        time_fn: Callable[[], float] = time.time,
        foreground_adapter: Callable[[dict[str, Any]], dict[str, Any]] | Any | None = None,
        allow_background_fallback: bool = DEFAULT_BACKGROUND_FALLBACK,
        feishu_notify: bool | None = None,
        feishu_chat_id: str | None = None,
        feishu_as: str | None = None,
        notification_sender: Callable[[dict[str, Any]], None] | None = None,
        notification_interval_seconds: float | None = None,
    ) -> None:
        self.session_root = session_root
        self.state_path = state_path
        self.log_root = log_root
        self.events_path = events_path
        self.codex_bin = codex_bin
        self.poll_seconds = max(0.2, poll_seconds)
        self.process_factory = process_factory
        self.time_fn = time_fn
        self.foreground_adapter = foreground_adapter
        self.allow_background_fallback = allow_background_fallback
        self.feishu_notify = (
            isEnabled(os.environ.get("CODEX_AUTO_CONTINUE_FEISHU_NOTIFY"))
            if feishu_notify is None
            else bool(feishu_notify)
        )
        self.feishu_chat_id = str(
            feishu_chat_id
            or os.environ.get("CODEX_AUTO_CONTINUE_FEISHU_CHAT_ID", DEFAULT_FEISHU_CHAT_ID)
        ).strip()
        self.feishu_as = str(
            feishu_as or os.environ.get("CODEX_AUTO_CONTINUE_FEISHU_AS", DEFAULT_FEISHU_AS)
        ).strip().lower()
        if self.feishu_as not in {"bot", "user"}:
            self.feishu_as = DEFAULT_FEISHU_AS
        self.notification_interval_seconds = max(
            1.0,
            float(
                notification_interval_seconds
                if notification_interval_seconds is not None
                else getFloatEnv(
                    "CODEX_AUTO_CONTINUE_FEISHU_NOTIFY_INTERVAL",
                    DEFAULT_FEISHU_NOTIFY_INTERVAL_SECONDS,
                )
            ),
        )
        self.notification_sender = notification_sender or self.sendFeishuNotification
        self.state = self.loadState()
        if not self.allow_background_fallback:
            self.migrateToForegroundOnly()
        self.processes: dict[str, subprocess.Popen[str]] = {}

    def loadState(self) -> dict[str, Any]:
        try:
            with self.state_path.open(encoding="utf-8") as handle:
                state = json.load(handle)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return defaultState()
        if not isinstance(state, dict):
            return defaultState()
        state.setdefault("initialized", False)
        state.setdefault("files", {})
        state.setdefault("sessions", {})
        state.setdefault("pending_failures", [])
        state.setdefault("notification_queue", [])
        state.setdefault("notification_window_started_at", 0.0)
        state.setdefault("notification_next_flush_at", 0.0)
        state.setdefault("notification_omitted_count", 0)
        return state

    def migrateToForegroundOnly(self) -> None:
        """清除旧后台续跑状态，避免迁移后旧 PID 阻塞前台交互。"""
        changed = bool(self.state.get("pending_failures"))
        self.state["pending_failures"] = []
        for session in self.state.get("sessions", {}).values():
            if not isinstance(session, dict):
                continue
            if session.pop("active", None) is not None:
                changed = True
            if session.get("last_action") in {"continued", "background_continued"}:
                session["last_action"] = "migrated_foreground_only"
                changed = True
        if changed:
            self.saveState()

    def saveState(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.state_path.with_suffix(".tmp")
        with temporary_path.open("w", encoding="utf-8") as handle:
            json.dump(self.state, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, self.state_path)

    def appendStatusEvent(self, event: dict[str, Any]) -> None:
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        existed = self.events_path.exists()
        with self.events_path.open("a", encoding="utf-8") as handle:
            json.dump(event, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
        if not existed:
            os.chmod(self.events_path, 0o600)

    def emitNotice(self, session_id: str, turn_id: str, message: str, level: str = "info") -> None:
        event = {
            "ts": self.time_fn(),
            "level": level,
            "session_id": session_id,
            "turn_id": turn_id,
            "message": message,
        }
        session = self.getSessionState(session_id)
        session["last_notice"] = event
        try:
            self.appendStatusEvent(event)
        except OSError as exc:
            logging.warning("auto-continue status event write failed error=%s", type(exc).__name__)

    def iterSessionFiles(self) -> list[Path]:
        if not self.session_root.is_dir():
            return []
        return sorted(self.session_root.rglob("*.jsonl"))

    def bootstrap(self) -> None:
        for path in self.iterSessionFiles():
            file_state = self.getFileState(path)
            self.hydrateFileMetadata(path, file_state)
            file_state["offset"] = path.stat().st_size
        self.state["initialized"] = True
        self.saveState()
        logging.info("auto-continue baseline initialized files=%d", len(self.state["files"]))

    def hydrateFileMetadata(self, path: Path, file_state: dict[str, Any]) -> None:
        """读取会话文件头，兼容真实 session_meta 顶层事件。"""
        try:
            with path.open("rb") as handle:
                for _ in range(256):
                    raw_line = handle.readline()
                    if not raw_line:
                        break
                    try:
                        event = json.loads(raw_line.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if not isinstance(event, dict):
                        continue
                    payload = event.get("payload")
                    if not isinstance(payload, dict):
                        continue
                    if event.get("type") == "session_meta":
                        payload = {"type": "session_meta", **payload}
                    self.updateMetadata(file_state, payload)
        except OSError:
            return

    def hydrateExistingFiles(self) -> None:
        for path in self.iterSessionFiles():
            file_state = self.getFileState(path)
            if "session_id" in file_state and "watch" in file_state:
                continue
            self.hydrateFileMetadata(path, file_state)

    def getFileState(self, path: Path) -> dict[str, Any]:
        files = self.state["files"]
        key = str(path)
        value = files.get(key)
        if not isinstance(value, dict):
            value = {"offset": 0, "watch": False}
            files[key] = value
        value.setdefault("offset", 0)
        value.setdefault("watch", False)
        return value

    def getSessionState(self, session_id: str) -> dict[str, Any]:
        sessions = self.state["sessions"]
        value = sessions.get(session_id)
        if not isinstance(value, dict):
            value = {"failure_times": [], "handled_turn_ids": []}
            sessions[session_id] = value
        value.setdefault("failure_times", [])
        value.setdefault("handled_turn_ids", [])
        value.setdefault("auto_continue_attempts", 0)
        return value

    def isProcessAlive(self, pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def reapProcesses(self) -> None:
        for session_id, process in list(self.processes.items()):
            if process.poll() is None:
                continue
            logging.info("auto-continue resume exited session=%s rc=%d", session_id, int(process.returncode or 0))
            self.processes.pop(session_id, None)
            session = self.getSessionState(session_id)
            session.pop("active", None)
        for session_id, session in list(self.state["sessions"].items()):
            if not isinstance(session, dict) or session_id in self.processes:
                continue
            active = session.get("active")
            if isinstance(active, dict) and not self.isProcessAlive(int(active.get("pid") or 0)):
                session.pop("active", None)
        self.drainPendingFailures()

    def isSessionActive(self, session_id: str) -> bool:
        process = self.processes.get(session_id)
        if process is not None and process.poll() is None:
            return True
        session = self.getSessionState(session_id)
        active = session.get("active")
        return isinstance(active, dict) and self.isProcessAlive(int(active.get("pid") or 0))

    def updateMetadata(self, file_state: dict[str, Any], payload: dict[str, Any]) -> None:
        event_type = payload.get("type")
        declared_channel = str(payload.get("frontend_channel") or "").strip().lower()
        if declared_channel in {"auto", "waveterm", "terminal", "virtual_employee"}:
            file_state["frontend_channel"] = declared_channel
        if isinstance(payload.get("virtual_employee"), dict):
            file_state["virtual_employee"] = dict(payload["virtual_employee"])
            file_state["watch"] = True
            file_state["frontend_channel"] = "virtual_employee"
        if event_type == "session_meta":
            session_id = str(payload.get("session_id") or payload.get("id") or "").strip()
            file_state["session_id"] = session_id
            originator = str(payload.get("originator") or "").strip()
            file_state["originator"] = originator
            file_state["watch"] = originator in WATCHED_ORIGINATORS
            if originator in {"codex_exec", "virtual_employee", "roadclaw"}:
                file_state["frontend_channel"] = "virtual_employee"
            file_state["cwd"] = str(payload.get("cwd") or Path.home())
        elif event_type == "thread_settings_applied":
            settings = payload.get("thread_settings")
            if isinstance(settings, dict):
                model = str(settings.get("model") or "").strip()
                effort = str(settings.get("reasoning_effort") or settings.get("effort") or "").strip()
                cwd = str(settings.get("cwd") or "").strip()
                if model:
                    file_state["model"] = model
                if effort:
                    file_state["reasoning_effort"] = effort
                if cwd:
                    file_state["cwd"] = cwd
        elif event_type == "turn_context":
            model = str(payload.get("model") or "").strip()
            effort = str(payload.get("effort") or "").strip()
            if not effort:
                collaboration_mode = payload.get("collaboration_mode")
                if isinstance(collaboration_mode, dict):
                    settings = collaboration_mode.get("settings")
                    if isinstance(settings, dict):
                        effort = str(settings.get("reasoning_effort") or settings.get("effort") or "").strip()
            cwd = str(payload.get("cwd") or "").strip()
            if model:
                file_state["model"] = model
            if effort:
                file_state["reasoning_effort"] = effort
            if cwd:
                file_state["cwd"] = cwd

    def startResume(self, session_id: str, turn_id: str, model: str, cwd: str, reasoning_effort: str | None = None) -> int:
        resume_dir = self.log_root / session_id / turn_id
        resume_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = resume_dir / "stdout.log"
        stderr_path = resume_dir / "stderr.log"
        last_message_path = resume_dir / "last_message.txt"
        working_directory = Path(cwd)
        if not working_directory.is_dir():
            working_directory = Path.home()
        command = [str(self.codex_bin)]
        effective_reasoning_effort = str(reasoning_effort or "").strip() or DEFAULT_REASONING_EFFORT
        if effective_reasoning_effort:
            command.extend(["-c", f'model_reasoning_effort="{effective_reasoning_effort}"'])
        command.extend([
            "-m",
            model,
            "exec",
            "resume",
            "--skip-git-repo-check",
            "-o",
            str(last_message_path),
            session_id,
            "-",
        ])
        stdout_handle = stdout_path.open("w", encoding="utf-8")
        stderr_handle = stderr_path.open("w", encoding="utf-8")
        try:
            process = self.process_factory(
                command,
                cwd=str(working_directory),
                stdin=subprocess.PIPE,
                stdout=stdout_handle,
                stderr=stderr_handle,
                text=True,
                start_new_session=True,
            )
            assert process.stdin is not None
            process.stdin.write(DEFAULT_PROMPT + "\n")
            process.stdin.close()
        except Exception:
            stdout_handle.close()
            stderr_handle.close()
            raise
        stdout_handle.close()
        stderr_handle.close()
        self.processes[session_id] = process
        return int(process.pid)

    @staticmethod
    def formatNotificationTime(timestamp: float) -> str:
        return time.strftime("%H:%M:%S", time.localtime(float(timestamp)))

    def sendFeishuNotification(self, batch: dict[str, Any]) -> None:
        if not self.feishu_chat_id:
            return
        lark_cli = shutil.which("lark-cli") or str(DEFAULT_LARK_CLI)
        raw_triggers = batch.get("triggers")
        if not isinstance(raw_triggers, list):
            # 兼容旧调用方传入单条触发记录。
            raw_triggers = [batch]
        triggers = [item for item in raw_triggers if isinstance(item, dict)]
        if not triggers:
            return
        window_started_at = float(batch.get("window_started_at") or triggers[0].get("ts") or self.time_fn())
        window_ended_at = float(batch.get("window_ended_at") or self.time_fn())
        lines = [
            "【Codex 自动续跑】10 分钟汇总",
            f"时间窗：{self.formatNotificationTime(window_started_at)} - {self.formatNotificationTime(window_ended_at)}",
            f"成功触发：{len(triggers)} 次",
        ]
        for index, trigger in enumerate(triggers, 1):
            session_id = str(trigger.get("session_id") or "")
            turn_id = str(trigger.get("turn_id") or "")
            model = str(trigger.get("model") or "未知")
            mode = str(trigger.get("mode") or "background_resume")
            mode_text = "前台输入继续" if mode == "foreground" else "后台 resume"
            session_text = session_id[:8] + "…" if session_id else "未知"
            turn_text = turn_id[:16] + "…" if len(turn_id) > 16 else (turn_id or "未知")
            attempt_count = int(trigger.get("attempt_count") or 0)
            lines.append(
                f"{index}. 会话：{session_text}｜模型：{model}｜方式：{mode_text}｜"
                f"turn：{turn_text}｜尝试次数：{attempt_count}次"
            )
        omitted_count = int(batch.get("omitted_count") or 0)
        if omitted_count:
            lines.append(f"其余 {omitted_count} 次触发已合并省略。")
        message = "\n".join(lines)
        idempotency_seed = "codex-auto-continue-summary:" + json.dumps(
            triggers,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        idempotency_key = "cac-" + hashlib.sha1(idempotency_seed.encode("utf-8")).hexdigest()[:24]
        result = subprocess.run(
            [
                lark_cli,
                "im",
                "+messages-send",
                "--as",
                self.feishu_as,
                "--chat-id",
                self.feishu_chat_id,
                "--text",
                message,
                "--idempotency-key",
                idempotency_key,
            ],
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=FEISHU_NOTIFY_TIMEOUT_SECONDS,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError("lark-cli messages-send failed")

    def notifySuccess(
        self,
        session_id: str,
        turn_id: str,
        model: str,
        mode: str,
    ) -> None:
        session = self.getSessionState(session_id)
        attempt_count = int(session.get("auto_continue_attempts") or 0) + 1
        session["auto_continue_attempts"] = attempt_count
        if not self.feishu_notify:
            return
        now = self.time_fn()
        queue = self.state.setdefault("notification_queue", [])
        if not isinstance(queue, list):
            queue = []
            self.state["notification_queue"] = queue
        if not queue:
            self.state["notification_window_started_at"] = now
            self.state["notification_next_flush_at"] = now + self.notification_interval_seconds
        queue.append(
            {
                "session_id": session_id,
                "turn_id": turn_id,
                "model": model,
                "mode": mode,
                "attempt_count": attempt_count,
                "ts": now,
            }
        )
        if len(queue) > MAX_FEISHU_NOTIFICATION_ITEMS:
            overflow = len(queue) - MAX_FEISHU_NOTIFICATION_ITEMS
            del queue[:overflow]
            self.state["notification_omitted_count"] = int(
                self.state.get("notification_omitted_count") or 0
            ) + overflow

    def flushSuccessNotifications(self, force: bool = False) -> bool:
        """按 10 分钟窗口发送成功触发汇总；失败则保留队列等待重试。"""
        if not self.feishu_notify:
            return False
        queue = self.state.get("notification_queue")
        if not isinstance(queue, list) or not queue:
            return False
        now = self.time_fn()
        next_flush_at = float(self.state.get("notification_next_flush_at") or 0.0)
        if not force and next_flush_at > now:
            return False
        batch = {
            "triggers": list(queue),
            "window_started_at": float(self.state.get("notification_window_started_at") or now),
            "window_ended_at": now,
            "omitted_count": int(self.state.get("notification_omitted_count") or 0),
        }
        try:
            self.notification_sender(batch)
        except Exception as exc:
            self.state["notification_next_flush_at"] = now + FEISHU_NOTIFY_RETRY_SECONDS
            logging.warning(
                "auto-continue Feishu notification failed items=%d error=%s",
                len(queue),
                type(exc).__name__,
            )
            return False
        self.state["notification_queue"] = []
        self.state["notification_window_started_at"] = 0.0
        self.state["notification_next_flush_at"] = 0.0
        self.state["notification_omitted_count"] = 0
        return True

    def tryForegroundResume(
        self,
        session_id: str,
        turn_id: str,
        file_state: dict[str, Any],
        payload: dict[str, Any],
        model: str,
        reasoning_effort: str,
    ) -> dict[str, Any]:
        """调用前台适配器；适配器不可用时也只返回 observe_only。"""
        adapter = self.foreground_adapter
        if adapter is None:
            return {"status": "observe_only", "reason": "adapter_unavailable"}
        request = {
            "session_id": session_id,
            "turn_id": turn_id,
            "event_id": str(payload.get("event_id") or payload.get("id") or f"{session_id}:{turn_id}"),
            "cwd": str(file_state.get("cwd") or ""),
            "frontend_channel": str(file_state.get("frontend_channel") or ""),
            "file_state": dict(file_state),
            "payload": payload,
            "model": model,
            "reasoning_effort": reasoning_effort,
            "force_continue": bool(
                isCapacityError(payload.get("error")) and model == MODEL_CHAIN[-1]
            ),
        }
        if isinstance(file_state.get("virtual_employee"), dict):
            request["virtual_employee"] = dict(file_state["virtual_employee"])
        if isinstance(payload.get("virtual_employee"), dict):
            request["virtual_employee"] = dict(payload["virtual_employee"])
        try:
            if callable(adapter):
                result = adapter(request)
            else:
                result = adapter.continueSession(request)
        except Exception as exc:  # 适配器故障应进入可控兜底，而非打死 watcher。
            logging.warning("foreground adapter failed session=%s error=%s", session_id, type(exc).__name__)
            return {"status": "failed", "reason": type(exc).__name__}
        if not isinstance(result, dict):
            return {"status": "observe_only", "reason": "invalid_adapter_result"}
        return result

    def drainPendingFailures(self) -> None:
        pending = self.state.get("pending_failures")
        if not isinstance(pending, list) or not pending:
            return
        # 同一会话的并发终态只保留最新一条，避免一次恢复失败触发多次续跑。
        latest_by_session: dict[str, dict[str, Any]] = {}
        for item in pending:
            if not isinstance(item, dict):
                continue
            session_id = str(item.get("session_id") or "").strip()
            if session_id:
                latest_by_session[session_id] = item
        pending = list(latest_by_session.values())
        remaining: list[dict[str, Any]] = []
        for item in pending:
            if not isinstance(item, dict):
                continue
            session_id = str(item.get("session_id") or "").strip()
            if self.isSessionActive(session_id):
                remaining.append(item)
                continue
            file_state = {
                "session_id": session_id,
                "watch": True,
                "model": str(item.get("model") or "gpt-5.6-sol"),
                "reasoning_effort": str(item.get("reasoning_effort") or "").strip(),
                "cwd": str(item.get("cwd") or Path.home()),
                "frontend_channel": str(item.get("frontend_channel") or ""),
            }
            if isinstance(item.get("virtual_employee"), dict):
                file_state["virtual_employee"] = dict(item["virtual_employee"])
            payload = item.get("payload")
            if isinstance(payload, dict):
                self.handleFailure(file_state, payload, allow_pending=False)
        self.state["pending_failures"] = remaining

    def handleFailure(self, file_state: dict[str, Any], payload: dict[str, Any], allow_pending: bool = True) -> None:
        session_id = str(file_state.get("session_id") or "").strip()
        turn_id = str(payload.get("turn_id") or "").strip()
        if not session_id or not turn_id:
            return
        session = self.getSessionState(session_id)
        handled_turn_ids = [str(value) for value in session["handled_turn_ids"]]
        if turn_id in handled_turn_ids:
            return
        if self.isSessionActive(session_id):
            if allow_pending:
                pending = self.state.setdefault("pending_failures", [])
                # 按 session 替换旧 pending，而不是累积 turn，保证一次只续跑一次。
                pending[:] = [
                    item for item in pending
                    if not isinstance(item, dict) or str(item.get("session_id") or "") != session_id
                ]
                pending.append({
                    "session_id": session_id,
                    "turn_id": turn_id,
                    "model": file_state.get("model"),
                    "reasoning_effort": file_state.get("reasoning_effort"),
                    "cwd": file_state.get("cwd"),
                    "frontend_channel": file_state.get("frontend_channel"),
                    "virtual_employee": file_state.get("virtual_employee"),
                    "payload": payload,
                })
                self.emitNotice(
                    session_id,
                    turn_id,
                    f"已有续跑进程，新的失败已排队合并；等待当前续跑结束后继续。session={session_id[:8]}…",
                    "warning",
                )
            logging.warning("auto-continue queued concurrent failure session=%s turn=%s", session_id, turn_id)
            return
        handled_turn_ids.append(turn_id)
        session["handled_turn_ids"] = handled_turn_ids[-MAX_HANDLED_TURNS:]
        current_model = str(file_state.get("model") or session.get("last_model") or MODEL_CHAIN[0]).strip() or MODEL_CHAIN[0]
        current_model = LEGACY_MODEL_ALIASES.get(current_model, current_model)
        file_state["model"] = current_model
        reasoning_effort = getReasoningEffort(file_state)
        capacity_failure = isCapacityError(payload.get("error"))
        lowest_model_capacity_failure = bool(
            capacity_failure and current_model == MODEL_CHAIN[-1]
        )
        resume_model = current_model
        now = self.time_fn()
        # 容量错误先沿链路下探；到最低档后继续重试，同时只保留有限历史。
        if capacity_failure and current_model in MODEL_CHAIN and current_model != MODEL_CHAIN[-1]:
            resume_model = getNextModel(current_model)
            file_state["model"] = resume_model
            session["last_failure_kind"] = "capacity"
            session["failure_times"] = []
            notice_message = f"检测到模型容量不足，自动降级 {current_model} → {resume_model}，并发送“继续”。"
        else:
            failure_times = [float(value) for value in session["failure_times"] if float(value) >= now - FAILURE_WINDOW_SECONDS]
            failure_times.append(now)
            session["failure_times"] = failure_times[-FAILURE_HISTORY_LIMIT:]
            session["last_failure_kind"] = "capacity" if capacity_failure else "other"
            if capacity_failure:
                if lowest_model_capacity_failure:
                    notice_message = f"最低模型 {current_model} 仍容量不足，直接重试“继续”；不再降级或放弃。"
                else:
                    notice_message = f"模型 {current_model} 容量不足，自动发送“继续”并持续重试。"
            else:
                notice_message = "检测到非手动失败，自动发送“继续”并持续重试。"
        session["last_model"] = resume_model
        model_switch_required = bool(
            capacity_failure
            and current_model in MODEL_CHAIN
            and resume_model != current_model
        )
        if model_switch_required:
            # 前台只支持输入“继续”，不能改变当前 TUI 模型；降级必须走带 -m 的 resume。
            foreground_result = {"status": "observe_only", "reason": "model_switch_requires_resume"}
        else:
            foreground_result = self.tryForegroundResume(
                session_id, turn_id, file_state, payload, resume_model, reasoning_effort
            )
        foreground_status = str(foreground_result.get("status") or "").strip().lower()
        if foreground_status in {"injected", "confirmed"}:
            session["last_action"] = "foreground_continued"
            session["foreground"] = foreground_result
            self.emitNotice(session_id, turn_id, f"前台适配器已发送“继续”（{foreground_status}）。", "info")
            self.notifySuccess(session_id, turn_id, resume_model, "foreground")
            return
        fallback_allowed = self.allow_background_fallback and foreground_status in {"observe_only", "failed"}
        if not fallback_allowed:
            session["last_action"] = "foreground_" + (foreground_status or "unavailable")
            session["foreground"] = foreground_result
            self.emitNotice(
                session_id,
                turn_id,
                f"前台适配器未注入（{foreground_status or 'unknown'}），后台兜底未启用；请人工接管。",
                "warning",
            )
            return
        try:
            pid = self.startResume(
                session_id,
                turn_id,
                resume_model,
                str(file_state.get("cwd") or Path.home()),
                reasoning_effort,
            )
        except Exception as exc:
            session["last_action"] = "resume_start_failed"
            self.emitNotice(
                session_id,
                turn_id,
                f"自动续跑启动失败：{type(exc).__name__}；请人工接管。session={session_id[:8]}…",
                "error",
            )
            logging.exception("auto-continue start failed session=%s error=%s", session_id, type(exc).__name__)
            return
        session["active"] = {"pid": pid, "turn_id": turn_id, "started_at": now}
        session["last_action"] = "background_continued"
        session["foreground"] = foreground_result
        self.emitNotice(session_id, turn_id, notice_message, "warning" if capacity_failure else "info")
        self.notifySuccess(session_id, turn_id, resume_model, "background_resume")
        logging.warning(
            "auto-continue sent prompt session=%s turn=%s model=%s reasoning_effort=%s",
            session_id,
            turn_id,
            resume_model,
            reasoning_effort,
        )

    def handleEvent(self, file_state: dict[str, Any], payload: dict[str, Any]) -> None:
        self.updateMetadata(file_state, payload)
        if not file_state.get("watch"):
            return
        event_type = payload.get("type")
        session_id = str(file_state.get("session_id") or "").strip()
        if event_type == "turn_aborted" and session_id:
            session = self.getSessionState(session_id)
            session["failure_times"] = []
            session["auto_continue_attempts"] = 0
            session["last_action"] = "manual_abort"
            return
        if event_type == "task_complete" and session_id and not payload.get("error"):
            session = self.getSessionState(session_id)
            session["failure_times"] = []
            session["auto_continue_attempts"] = 0
            session["last_action"] = "completed"
            return
        if isFailureEvent(payload):
            self.handleFailure(file_state, payload)

    def scan(self) -> None:
        self.reapProcesses()
        for path in self.iterSessionFiles():
            file_state = self.getFileState(path)
            offset = int(file_state.get("offset") or 0)
            size = path.stat().st_size
            if size < offset:
                offset = 0
            if size == offset:
                continue
            with path.open("rb") as handle:
                handle.seek(offset)
                content = handle.read()
            consumed = 0
            for raw_line in content.splitlines(keepends=True):
                if not raw_line.endswith(b"\n"):
                    break
                consumed += len(raw_line)
                try:
                    event = json.loads(raw_line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                payload = event.get("payload") if isinstance(event, dict) else None
                if isinstance(payload, dict):
                    if event.get("type") == "session_meta":
                        payload = {"type": "session_meta", **payload}
                    self.handleEvent(file_state, payload)
            file_state["offset"] = offset + consumed
        self.reapProcesses()
        self.flushSuccessNotifications()
        self.saveState()

    def run(self, once: bool = False) -> None:
        if not self.state.get("initialized"):
            self.bootstrap()
            if once:
                return
        else:
            self.hydrateExistingFiles()
            self.saveState()
        while True:
            self.scan()
            if once:
                return
            time.sleep(self.poll_seconds)


def parseArgs() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Codex 前台通道失败自动续跑监控器")
    parser.add_argument("--session-root", type=Path, default=DEFAULT_SESSION_ROOT)
    parser.add_argument("--state-path", type=Path, default=DEFAULT_STATE_PATH)
    parser.add_argument("--log-root", type=Path, default=DEFAULT_LOG_ROOT)
    parser.add_argument("--events-path", type=Path, default=DEFAULT_EVENTS_PATH)
    parser.add_argument("--codex-bin", type=Path, default=DEFAULT_CODEX_BIN)
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument(
        "--allow-background-fallback",
        action="store_true",
        default=isEnabled(os.environ.get("CODEX_AUTO_CONTINUE_BACKGROUND_FALLBACK")),
        help="前台适配器 observe_only/failed 时允许后台 codex exec resume 兜底",
    )
    parser.add_argument(
        "--feishu-notify",
        action="store_true",
        default=isEnabled(os.environ.get("CODEX_AUTO_CONTINUE_FEISHU_NOTIFY")),
        help="每次成功触发续跑后发送一条飞书通知",
    )
    parser.add_argument(
        "--feishu-chat-id",
        default=os.environ.get("CODEX_AUTO_CONTINUE_FEISHU_CHAT_ID", DEFAULT_FEISHU_CHAT_ID),
        help="飞书通知目标群 chat_id",
    )
    parser.add_argument(
        "--feishu-as",
        choices=("bot", "user"),
        default=os.environ.get("CODEX_AUTO_CONTINUE_FEISHU_AS", DEFAULT_FEISHU_AS),
        help="飞书通知身份",
    )
    parser.add_argument(
        "--feishu-notify-interval-seconds",
        type=float,
        default=getFloatEnv(
            "CODEX_AUTO_CONTINUE_FEISHU_NOTIFY_INTERVAL",
            DEFAULT_FEISHU_NOTIFY_INTERVAL_SECONDS,
        ),
        help="飞书成功触发汇总窗口，默认 600 秒",
    )
    parser.add_argument(
        "--enable-foreground",
        action="store_true",
        default=isEnabled(os.environ.get("CODEX_AUTO_CONTINUE_FOREGROUND")),
        help="启用前台通道输入；未启用时只观察并记录事件",
    )
    parser.add_argument(
        "--frontend-channel",
        choices=("auto", "waveterm", "terminal", "virtual_employee"),
        default=os.environ.get("CODEX_AUTO_CONTINUE_FRONTEND_CHANNEL", "auto"),
        help="前台通道：auto/waveterm/terminal/virtual_employee",
    )
    parser.add_argument(
        "--terminal-pty",
        action="store_true",
        default=isEnabled(os.environ.get("CODEX_AUTO_CONTINUE_TERMINAL_PTY")),
        help="允许普通终端在身份校验通过后写入 PTY；tmux 通道不依赖此开关",
    )
    parser.add_argument(
        "--terminal-assume-ready",
        action="store_true",
        default=isEnabled(os.environ.get("CODEX_AUTO_CONTINUE_TERMINAL_ASSUME_READY")),
        help="普通裸终端无法读取屏幕时，允许按失败事件推断已到输入提示符",
    )
    parser.add_argument(
        "--virtual-employee-hook",
        default=os.environ.get("CODEX_AUTO_CONTINUE_VIRTUAL_EMPLOYEE_HOOK", ""),
        help="虚拟员工可见续跑 hook 命令；JSON 从 stdin 传入，禁止 shell 拼接",
    )
    parser.add_argument(
        "--foreground-state-path",
        type=Path,
        default=Path.home() / ".local" / "state" / "codex-session-auto-continue" / "foreground.json",
    )
    parser.add_argument("--once", action="store_true")
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parseArgs()
    from foreground_channels import MultiFrontendAdapter

    foreground_adapter = MultiFrontendAdapter(
        channel=args.frontend_channel,
        observe_only=not args.enable_foreground,
        state_path=args.foreground_state_path,
        terminal_direct=args.terminal_pty,
        terminal_assume_ready=args.terminal_assume_ready,
        virtual_hook=args.virtual_employee_hook or None,
    )
    watcher = SessionAutoContinue(
        session_root=args.session_root,
        state_path=args.state_path,
        log_root=args.log_root,
        events_path=args.events_path,
        codex_bin=args.codex_bin,
        poll_seconds=args.poll_seconds,
        foreground_adapter=foreground_adapter,
        allow_background_fallback=args.allow_background_fallback,
        feishu_notify=args.feishu_notify,
        feishu_chat_id=args.feishu_chat_id,
        feishu_as=args.feishu_as,
        notification_interval_seconds=args.feishu_notify_interval_seconds,
    )
    watcher.run(once=args.once)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
