#!/usr/bin/env python3
"""Codex TUI 自动续跑监控器测试。"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from session_auto_continue import SessionAutoContinue


SESSION_ID = "00000000-0000-4000-8000-000000000001"


class FakeStdin:
    def __init__(self) -> None:
        self.text = ""

    def write(self, value: str) -> int:
        self.text += value
        return len(value)

    def close(self) -> None:
        return None


class FakeProcess:
    next_pid = 400000

    def __init__(self, command: list[str], **_kwargs: Any) -> None:
        self.command = command
        self.stdin = FakeStdin()
        self.pid = FakeProcess.next_pid
        FakeProcess.next_pid += 1
        self.returncode = 0

    def poll(self) -> int:
        return self.returncode


def writeEvent(path: Path, payload: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"type": "event_msg", "payload": payload}, ensure_ascii=False) + "\n")


def writeSessionMeta(path: Path) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "type": "session_meta",
                    "payload": {
                        "session_id": SESSION_ID,
                        "originator": "codex-tui",
                        "cwd": "/tmp",
                    },
                },
                ensure_ascii=False,
            )
            + "\n"
        )


class SessionAutoContinueTest(unittest.TestCase):
    def makeWatcher(
        self,
        root: Path,
        commands: list[FakeProcess],
        allow_background_fallback: bool = True,
        foreground_adapter: Any | None = None,
        feishu_notify: bool = False,
        notification_sender: Any | None = None,
        notification_interval_seconds: float = 600.0,
        time_fn: Any | None = None,
    ) -> SessionAutoContinue:
        def processFactory(command: list[str], **kwargs: Any) -> FakeProcess:
            process = FakeProcess(command, **kwargs)
            commands.append(process)
            return process

        watcher = SessionAutoContinue(
            session_root=root / "sessions",
            state_path=root / "state.json",
            log_root=root / "logs",
            events_path=root / "events.jsonl",
            codex_bin=Path("/usr/bin/codex"),
            process_factory=processFactory,
            time_fn=time_fn or __import__("time").time,
            foreground_adapter=foreground_adapter,
            allow_background_fallback=allow_background_fallback,
            feishu_notify=feishu_notify,
            notification_sender=notification_sender,
            notification_interval_seconds=notification_interval_seconds,
        )
        watcher.bootstrap()
        return watcher

    def appendMetadata(self, path: Path) -> None:
        writeEvent(path, {"type": "session_meta", "session_id": SESSION_ID, "originator": "codex-tui", "cwd": "/tmp"})
        writeEvent(path, {"type": "thread_settings_applied", "thread_settings": {"model": "gpt-5.6-sol", "cwd": "/tmp", "reasoning_effort": "xhigh"}})
        writeEvent(
            path,
            {
                "type": "turn_context",
                "turn_id": "turn-context",
                "cwd": "/tmp",
                "model": "gpt-5.6-sol",
                "effort": "xhigh",
                "collaboration_mode": {"settings": {"model": "gpt-5.6-sol", "reasoning_effort": "xhigh"}},
            },
        )

    def appendFailure(self, path: Path, turn_id: str, error: Any = None, message: str = "upstream failure") -> None:
        if error is None:
            error = {"message": message}
        writeEvent(path, {"type": "task_complete", "turn_id": turn_id, "error": error})

    def appendTurnContext(self, path: Path, model: str, effort: str = "xhigh") -> None:
        writeEvent(
            path,
            {
                "type": "turn_context",
                "turn_id": f"turn-context-{model}",
                "cwd": "/tmp",
                "model": model,
                "effort": effort,
                "collaboration_mode": {"settings": {"model": model, "reasoning_effort": effort}},
            },
        )

    def test_retries_without_failure_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            session_root = root / "sessions"
            session_root.mkdir()
            session_file = session_root / "session.jsonl"
            session_file.touch()
            commands: list[FakeProcess] = []
            watcher = self.makeWatcher(root, commands)
            self.appendMetadata(session_file)
            self.appendFailure(session_file, "turn-1")
            watcher.scan()
            self.assertEqual(len(commands), 1)
            self.assertIn("-m", commands[0].command)
            self.assertIn("gpt-5.6-sol", commands[0].command)
            self.assertEqual(commands[0].stdin.text, "继续\n")

            self.appendFailure(session_file, "turn-2")
            watcher.scan()
            self.assertEqual(len(commands), 2)

            for turn_id in ("turn-3", "turn-4", "turn-5"):
                self.appendFailure(session_file, turn_id)
                watcher.scan()
            self.assertEqual(len(commands), 5)
            self.assertEqual(watcher.state["sessions"][SESSION_ID]["last_action"], "background_continued")

    def test_capacity_failures_walk_chain_then_retry_lowest_model_without_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            session_root = root / "sessions"
            session_root.mkdir()
            session_file = session_root / "session.jsonl"
            session_file.touch()
            commands: list[FakeProcess] = []
            watcher = self.makeWatcher(root, commands)
            self.appendMetadata(session_file)

            capacity_error = {
                "message": "Selected model is at capacity. Please try a different model.",
                "codex_error_info": "server_overloaded",
            }

            self.appendFailure(session_file, "turn-1", error=capacity_error)
            watcher.scan()
            self.assertEqual(commands[0].command[1], "-c")
            self.assertIn('model_reasoning_effort="xhigh"', commands[0].command)
            self.assertIn("gpt-5.6-terra", commands[0].command)

            self.appendTurnContext(session_file, "gpt-5.6-terra")
            self.appendFailure(session_file, "turn-2", error=capacity_error)
            watcher.scan()
            self.assertEqual(len(commands), 2)
            self.assertIn("gpt-5.6-luna", commands[1].command)

            self.appendTurnContext(session_file, "gpt-5.6-luna")
            self.appendFailure(session_file, "turn-3", error=capacity_error)
            watcher.scan()
            self.assertEqual(len(commands), 3)
            self.assertIn("gpt-5.6-luna", commands[2].command)

            self.appendTurnContext(session_file, "gpt-5.6-luna")
            self.appendFailure(session_file, "turn-4", error=capacity_error)
            watcher.scan()
            self.assertEqual(len(commands), 4)
            self.appendTurnContext(session_file, "gpt-5.6-luna")
            self.appendFailure(session_file, "turn-5", error=capacity_error)
            watcher.scan()
            self.assertEqual(len(commands), 5)
            self.assertEqual(watcher.state["sessions"][SESSION_ID]["last_action"], "background_continued")

    def test_capacity_downgrade_uses_resume_instead_of_original_foreground(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            session_root = root / "sessions"
            session_root.mkdir()
            session_file = session_root / "session.jsonl"
            session_file.touch()
            commands: list[FakeProcess] = []
            requests: list[dict[str, Any]] = []
            watcher = self.makeWatcher(
                root,
                commands,
                foreground_adapter=lambda request: requests.append(request) or {"status": "injected"},
            )
            self.appendMetadata(session_file)
            self.appendFailure(
                session_file,
                "turn-capacity",
                error={"message": "Selected model is at capacity", "codex_error_info": "server_overloaded"},
            )
            watcher.scan()
            self.assertEqual(len(commands), 1)
            self.assertIn("gpt-5.6-terra", commands[0].command)
            self.assertEqual(requests, [])

    def test_luna_capacity_failure_requests_force_continue(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            session_root = root / "sessions"
            session_root.mkdir()
            session_file = session_root / "session.jsonl"
            session_file.touch()
            commands: list[FakeProcess] = []
            requests: list[dict[str, Any]] = []
            watcher = self.makeWatcher(
                root,
                commands,
                foreground_adapter=lambda request: requests.append(request) or {"status": "injected"},
            )
            self.appendMetadata(session_file)
            self.appendTurnContext(session_file, "gpt-5.6-luna")
            self.appendFailure(
                session_file,
                "turn-luna-capacity",
                error={"message": "Selected model is at capacity", "codex_error_info": "server_overloaded"},
            )
            watcher.scan()
            self.assertEqual(commands, [])
            self.assertTrue(requests[0]["force_continue"])

    def test_capacity_resume_emits_status_event_without_terminal_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            session_root = root / "sessions"
            session_root.mkdir()
            session_file = session_root / "session.jsonl"
            session_file.touch()
            commands: list[FakeProcess] = []
            watcher = self.makeWatcher(root, commands)
            self.appendMetadata(session_file)
            self.appendFailure(
                session_file,
                "turn-1",
                error={
                    "message": "Selected model is at capacity. Please try a different model.",
                    "codex_error_info": "server_overloaded",
                },
            )
            watcher.scan()
            self.assertEqual(len(commands), 1)
            events = [json.loads(line) for line in (root / "events.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(events[-1]["session_id"], SESSION_ID)
            self.assertNotIn("terminal_notified", events[-1])

    def test_manual_abort_and_manual_error_do_not_resume(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            session_root = root / "sessions"
            session_root.mkdir()
            session_file = session_root / "session.jsonl"
            session_file.touch()
            commands: list[FakeProcess] = []
            watcher = self.makeWatcher(root, commands)
            self.appendMetadata(session_file)
            writeEvent(session_file, {"type": "turn_aborted", "turn_id": "turn-abort", "reason": "interrupted"})
            self.appendFailure(session_file, "turn-manual", "用户取消")
            watcher.scan()
            self.assertEqual(commands, [])
            self.assertEqual(watcher.state["sessions"][SESSION_ID]["last_action"], "manual_abort")

    def test_foreground_injection_is_preferred_without_background_process(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            session_root = root / "sessions"
            session_root.mkdir()
            session_file = session_root / "session.jsonl"
            session_file.touch()
            commands: list[FakeProcess] = []
            requests: list[dict[str, Any]] = []

            def foreground_adapter(request: dict[str, Any]) -> dict[str, Any]:
                requests.append(request)
                return {"status": "injected", "block_id": "block-1"}

            watcher = self.makeWatcher(root, commands, foreground_adapter=foreground_adapter)
            self.appendMetadata(session_file)
            self.appendFailure(session_file, "turn-foreground")
            watcher.scan()
            self.assertEqual(commands, [])
            self.assertEqual(requests[0]["turn_id"], "turn-foreground")
            self.assertEqual(watcher.state["sessions"][SESSION_ID]["last_action"], "foreground_continued")

    def test_foreground_success_notifications_are_batched(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            session_root = root / "sessions"
            session_root.mkdir()
            session_file = session_root / "session.jsonl"
            session_file.touch()
            commands: list[FakeProcess] = []
            notifications: list[dict[str, Any]] = []
            watcher = self.makeWatcher(
                root,
                commands,
                foreground_adapter=lambda _request: {"status": "confirmed"},
                feishu_notify=True,
                notification_sender=notifications.append,
            )
            self.appendMetadata(session_file)
            self.appendFailure(session_file, "turn-notify-1")
            watcher.scan()
            self.appendFailure(session_file, "turn-notify-2")
            watcher.scan()
            self.assertEqual(notifications, [])
            self.assertTrue(watcher.flushSuccessNotifications(force=True))
            self.assertEqual(len(notifications), 1)
            self.assertEqual(
                notifications[0]["triggers"],
                [
                    {
                        "session_id": SESSION_ID,
                        "turn_id": "turn-notify-1",
                        "model": "gpt-5.6-sol",
                        "mode": "foreground",
                        "attempt_count": 1,
                        "ts": notifications[0]["triggers"][0]["ts"],
                    },
                    {
                        "session_id": SESSION_ID,
                        "turn_id": "turn-notify-2",
                        "model": "gpt-5.6-sol",
                        "mode": "foreground",
                        "attempt_count": 2,
                        "ts": notifications[0]["triggers"][1]["ts"],
                    },
                ],
            )
            self.assertEqual(notifications[0]["window_ended_at"] >= notifications[0]["window_started_at"], True)

    def test_background_success_notification_is_queued(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            session_root = root / "sessions"
            session_root.mkdir()
            session_file = session_root / "session.jsonl"
            session_file.touch()
            commands: list[FakeProcess] = []
            notifications: list[dict[str, Any]] = []
            watcher = self.makeWatcher(
                root,
                commands,
                foreground_adapter=lambda _request: {"status": "failed", "reason": "transport_failed"},
                feishu_notify=True,
                notification_sender=notifications.append,
            )
            self.appendMetadata(session_file)
            self.appendFailure(session_file, "turn-background-notify")
            watcher.scan()
            self.assertEqual(len(commands), 1)
            self.assertEqual(notifications, [])
            watcher.flushSuccessNotifications(force=True)
            self.assertEqual(
                notifications,
                [
                    {
                        "triggers": [
                            {
                                "session_id": SESSION_ID,
                                "turn_id": "turn-background-notify",
                                "model": "gpt-5.6-sol",
                                "mode": "background_resume",
                                "attempt_count": 1,
                                "ts": notifications[0]["triggers"][0]["ts"],
                            }
                        ],
                        "window_started_at": notifications[0]["window_started_at"],
                        "window_ended_at": notifications[0]["window_ended_at"],
                        "omitted_count": 0,
                    }
                ],
            )

    def test_notification_flushes_after_ten_minutes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            session_root = root / "sessions"
            session_root.mkdir()
            session_file = session_root / "session.jsonl"
            session_file.touch()
            commands: list[FakeProcess] = []
            notifications: list[dict[str, Any]] = []
            clock = [1000.0]
            watcher = self.makeWatcher(
                root,
                commands,
                foreground_adapter=lambda _request: {"status": "confirmed"},
                feishu_notify=True,
                notification_sender=notifications.append,
                time_fn=lambda: clock[0],
            )
            self.appendMetadata(session_file)
            self.appendFailure(session_file, "turn-window")
            watcher.scan()
            self.assertEqual(notifications, [])
            self.assertEqual(watcher.state["notification_next_flush_at"], 1600.0)
            clock[0] = 1600.1
            watcher.scan()
            self.assertEqual(len(notifications), 1)
            self.assertEqual(notifications[0]["triggers"][0]["turn_id"], "turn-window")

    def test_default_notification_channel_is_local_without_feishu(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            with patch.dict(os.environ, {}, clear=True):
                watcher = SessionAutoContinue(state_path=Path(temporary_dir) / "state.json")
            self.assertEqual(watcher.notification_channel, "local")
            self.assertTrue(watcher.linux_notify)
            self.assertFalse(watcher.feishu_notify)

    def test_local_notification_uses_notify_send_without_feishu_cli(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            with patch("session_auto_continue.shutil.which", return_value="/usr/bin/notify-send") as which_mock:
                with patch("session_auto_continue.subprocess.run") as run_mock:
                    run_mock.return_value.returncode = 0
                    watcher = SessionAutoContinue(
                        state_path=Path(temporary_dir) / "state.json",
                        notification_channel="local",
                        notify_bin="notify-send",
                    )
                    watcher.sendLocalNotification(
                        {
                            "triggers": [
                                {
                                    "session_id": SESSION_ID,
                                    "turn_id": "turn-local-notify",
                                    "model": "gpt-5.6-luna",
                                    "mode": "foreground",
                                    "attempt_count": 2,
                                    "ts": 1000.0,
                                }
                            ],
                            "window_started_at": 1000.0,
                            "window_ended_at": 1600.0,
                        }
                    )
            which_mock.assert_called_once_with("notify-send")
            command = run_mock.call_args.args[0]
            self.assertEqual(command[:2], ["/usr/bin/notify-send", "Codex 自动续跑"])
            self.assertIn("10 分钟汇总", command[2])
            self.assertIn("尝试次数：2次", command[2])

    def test_missing_notify_send_does_not_block(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            watcher = SessionAutoContinue(
                state_path=Path(temporary_dir) / "state.json",
                notification_channel="local",
                notify_bin="notify-send",
            )
            with patch("session_auto_continue.shutil.which", return_value=None), patch(
                "session_auto_continue.subprocess.run"
            ) as run_mock:
                watcher.sendLocalNotification({"triggers": [{"session_id": SESSION_ID, "turn_id": "t"}]})
            run_mock.assert_not_called()

    def test_local_success_is_queued_and_flushed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            notifications: list[dict[str, Any]] = []
            watcher = SessionAutoContinue(
                state_path=Path(temporary_dir) / "state.json",
                notification_channel="local",
                notification_sender=notifications.append,
                time_fn=lambda: 1000.0,
            )
            watcher.notifySuccess(SESSION_ID, "turn-local-queue", "gpt-5.6-luna", "foreground")
            self.assertEqual(len(watcher.state["notification_queue"]), 1)
            self.assertTrue(watcher.flushSuccessNotifications(force=True))
            self.assertEqual(notifications[0]["triggers"][0]["turn_id"], "turn-local-queue")

    def test_feishu_notification_command_uses_configured_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            with patch("session_auto_continue.shutil.which", return_value="/usr/bin/lark-cli") as which_mock:
                with patch("session_auto_continue.subprocess.run") as run_mock:
                    run_mock.return_value.returncode = 0
                    watcher = SessionAutoContinue(
                        state_path=root / "state.json",
                        feishu_notify=True,
                        feishu_chat_id="oc_test_chat",
                        feishu_as="bot",
                    )
                    watcher.sendFeishuNotification(
                        {
                            "triggers": [
                                {
                                    "session_id": SESSION_ID,
                                    "turn_id": "turn-command-check",
                                    "model": "gpt-5.6-luna",
                                    "mode": "foreground",
                                    "attempt_count": 4,
                                    "ts": 1000.0,
                                }
                            ],
                            "window_started_at": 1000.0,
                            "window_ended_at": 1600.0,
                        }
                    )
            which_mock.assert_called_once_with("lark-cli")
            command = run_mock.call_args.args[0]
            self.assertEqual(command[:6], ["/usr/bin/lark-cli", "im", "+messages-send", "--as", "bot", "--chat-id"])
            self.assertIn("oc_test_chat", command)
            self.assertIn("--idempotency-key", command)
            self.assertIn("--text", command)
            message = command[command.index("--text") + 1]
            self.assertIn("10 分钟汇总", message)
            self.assertIn("尝试次数：4次", message)

    def test_background_fallback_requires_explicit_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            session_root = root / "sessions"
            session_root.mkdir()
            session_file = session_root / "session.jsonl"
            session_file.touch()
            commands: list[FakeProcess] = []
            watcher = self.makeWatcher(root, commands, allow_background_fallback=False)
            self.appendMetadata(session_file)
            self.appendFailure(session_file, "turn-no-fallback")
            watcher.scan()
            self.assertEqual(commands, [])
            self.assertEqual(watcher.state["sessions"][SESSION_ID]["last_action"], "foreground_observe_only")

    def test_failed_foreground_adapter_uses_enabled_background_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            session_root = root / "sessions"
            session_root.mkdir()
            session_file = session_root / "session.jsonl"
            session_file.touch()
            commands: list[FakeProcess] = []
            watcher = self.makeWatcher(
                root,
                commands,
                foreground_adapter=lambda _request: {"status": "failed", "reason": "transport_failed"},
            )
            self.appendMetadata(session_file)
            self.appendFailure(session_file, "turn-fallback")
            watcher.scan()
            self.assertEqual(len(commands), 1)
            self.assertEqual(watcher.state["sessions"][SESSION_ID]["last_action"], "background_continued")

    def test_success_resets_consecutive_failure_count(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            session_root = root / "sessions"
            session_root.mkdir()
            session_file = session_root / "session.jsonl"
            session_file.touch()
            commands: list[FakeProcess] = []
            watcher = self.makeWatcher(root, commands)
            self.appendMetadata(session_file)
            self.appendFailure(session_file, "turn-1")
            watcher.scan()
            writeEvent(session_file, {"type": "task_complete", "turn_id": "turn-success"})
            watcher.scan()
            self.appendFailure(session_file, "turn-2")
            watcher.scan()
            self.assertEqual(len(commands), 2)
            self.assertEqual(len(watcher.state["sessions"][SESSION_ID]["failure_times"]), 1)

    def test_failure_during_resume_is_queued_until_process_exits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            session_root = root / "sessions"
            session_root.mkdir()
            session_file = session_root / "session.jsonl"
            session_file.touch()
            commands: list[FakeProcess] = []
            watcher = self.makeWatcher(root, commands)
            self.appendMetadata(session_file)
            self.appendFailure(session_file, "turn-1")
            watcher.scan()
            commands[0].returncode = None
            watcher.processes[SESSION_ID] = commands[0]
            watcher.state["sessions"][SESSION_ID]["active"] = {"pid": commands[0].pid, "turn_id": "turn-1"}
            self.appendFailure(session_file, "turn-2")
            watcher.scan()
            self.assertEqual(len(commands), 1)
            self.assertEqual(len(watcher.state["pending_failures"]), 1)
            commands[0].returncode = 0
            watcher.scan()
            self.assertEqual(len(commands), 2)
            self.assertEqual(watcher.state["pending_failures"], [])

    def test_concurrent_failures_are_coalesced_to_one_resume(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            session_root = root / "sessions"
            session_root.mkdir()
            session_file = session_root / "session.jsonl"
            session_file.touch()
            commands: list[FakeProcess] = []
            watcher = self.makeWatcher(root, commands)
            self.appendMetadata(session_file)
            self.appendFailure(session_file, "turn-1")
            watcher.scan()
            commands[0].returncode = None
            watcher.processes[SESSION_ID] = commands[0]
            watcher.state["sessions"][SESSION_ID]["active"] = {
                "pid": commands[0].pid,
                "turn_id": "turn-1",
            }
            self.appendFailure(session_file, "turn-2")
            self.appendFailure(session_file, "turn-3")
            watcher.scan()
            self.assertEqual([item["turn_id"] for item in watcher.state["pending_failures"]], ["turn-3"])
            commands[0].returncode = 0
            watcher.scan()
            self.assertEqual(len(commands), 2)
            self.assertEqual(watcher.state["pending_failures"], [])

    def test_existing_session_meta_is_hydrated_before_new_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            session_root = root / "sessions"
            session_root.mkdir()
            session_file = session_root / "session.jsonl"
            writeSessionMeta(session_file)
            commands: list[FakeProcess] = []
            watcher = self.makeWatcher(root, commands)
            self.appendFailure(session_file, "turn-existing")
            watcher.scan()
            self.assertEqual(len(commands), 1)
            self.assertEqual(watcher.state["files"][str(session_file)]["session_id"], SESSION_ID)

    def test_initialized_state_migrates_existing_file_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            session_root = root / "sessions"
            session_root.mkdir()
            session_file = session_root / "session.jsonl"
            writeSessionMeta(session_file)
            state_path = root / "state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "initialized": True,
                        "files": {str(session_file): {"offset": session_file.stat().st_size}},
                        "sessions": {},
                        "pending_failures": [],
                    }
                ),
                encoding="utf-8",
            )
            commands: list[FakeProcess] = []
            watcher = SessionAutoContinue(
                session_root=session_root,
                state_path=state_path,
                log_root=root / "logs",
                codex_bin=Path("/usr/bin/codex"),
                process_factory=lambda command, **kwargs: commands.append(FakeProcess(command, **kwargs)) or commands[-1],
                allow_background_fallback=True,
            )
            watcher.run(once=True)
            self.appendFailure(session_file, "turn-migrated")
            watcher.scan()
            self.assertEqual(len(commands), 1)

    def test_foreground_only_migration_clears_stale_background_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            state_path = root / "state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "initialized": True,
                        "files": {},
                        "sessions": {SESSION_ID: {"active": {"pid": 123}, "last_action": "continued"}},
                        "pending_failures": [{"session_id": SESSION_ID, "turn_id": "old"}],
                    }
                ),
                encoding="utf-8",
            )
            watcher = SessionAutoContinue(state_path=state_path, allow_background_fallback=False)
            session = watcher.state["sessions"][SESSION_ID]
            self.assertNotIn("active", session)
            self.assertEqual(session["last_action"], "migrated_foreground_only")
            self.assertEqual(watcher.state["pending_failures"], [])


if __name__ == "__main__":
    unittest.main()
