"""Background tasks: thread execution + notification queue."""

from __future__ import annotations

import json
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.agent.progress import emit_progress
from src.agent.tools import BaseTool
from src.tools import bash_tool
from src.tools.bash_tool import (
    _OUTPUT_HARD_CAP,
    CommandCancelled,
    _audit_command,
    cap_marker,
    run_capped,
)
from src.tools.redaction import redact_secret_values
from src.tools.subprocess_env import _subprocess_env

WORKDIR = Path(__file__).resolve().parents[2]
_TIMEOUT_S = 300
# Output is kept whole (bash's streaming hard cap kills the process past
# 1M chars per stream and marks the kept prefix): ``check_background`` hands
# it to the loop, whose single truncation layer (``agent.tool_result_store``)
# previews it and offloads the full text for ``read_file`` paging — a second
# clip here would leave that copy incomplete.
# Finished tasks are evicted oldest-first past this many entries; running
# ones are never dropped. Bounds the per-process table (the singleton lives
# as long as the engine does) without losing a result before it is read.
_MAX_TASKS = 50
# Concurrently RUNNING tasks. The table cap above never evicts running
# entries, so without these a model could start any number of processes in a
# 2-CPU / 2 GB guest. Per session so one conversation cannot starve another.
_MAX_RUNNING = 4
_MAX_RUNNING_PER_SESSION = 2


def _current_session_id() -> Optional[str]:
    """Session of the attempt calling in (bound per attempt as log context)."""
    try:
        from src.core.logging_setup import _LOG_SESSION_ID

        return _LOG_SESSION_ID.get()
    except Exception:  # noqa: BLE001 - no context means "not attributed"
        return None


class BackgroundManager:
    """Background thread execution + notification queue.

    One per engine process, but every task belongs to the session whose
    attempt started it: results are handed back only to that session's
    attempts (``drain_notifications`` / ``check``), and the task is killed
    when that attempt is cancelled or the session is deleted. Tasks started
    outside any session (CLI) stay visible to every caller, as before.
    """

    def __init__(self) -> None:
        self.tasks: Dict[str, dict] = {}
        self._notifications: List[dict] = []
        self._lock = threading.Lock()

    def reset(self) -> None:
        """Forget every task and pending notification (test isolation).

        The manager is a process-wide singleton, so without this a task
        started by one test surfaces as a ``<background-results>`` message in
        an unrelated test's agent loop.
        """
        with self._lock:
            self.tasks.clear()
            self._notifications.clear()

    def run(
        self,
        command: str,
        cwd: str | Path | None = None,
        *,
        session_id: Optional[str] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> str:
        """Start a background task and return its task_id.

        Args:
            command: Shell command to execute.
            cwd: Working directory for the command; defaults to the engine
                install directory.
            session_id: Owning session (defaults to the calling attempt's).
            cancel_event: The owning attempt's cancel event (defaults to the
                one bound in the calling context); setting it kills the task.

        Returns:
            JSON string containing status and task_id.
        """
        if session_id is None:
            session_id = _current_session_id()
        if cancel_event is None:
            from src.core.cancel import get_cancel_event

            cancel_event = get_cancel_event()
        task_id = uuid.uuid4().hex[:8]
        stop = threading.Event()
        with self._lock:
            running = [t for t in self.tasks.values() if t["status"] == "running"]
            mine = [t for t in running if t.get("session_id") == session_id]
            if len(running) >= _MAX_RUNNING or (
                session_id is not None and len(mine) >= _MAX_RUNNING_PER_SESSION
            ):
                return json.dumps(
                    {
                        "status": "error",
                        "error_code": "too_many_background_tasks",
                        "error": (
                            f"{len(mine) if session_id is not None else len(running)} background "
                            "task(s) are still running. Wait for one to finish "
                            "(check_background) before starting another."
                        ),
                    },
                    ensure_ascii=False,
                )
            self._evict_finished_locked()
            self.tasks[task_id] = {
                "status": "running", "result": None, "command": command,
                "session_id": session_id, "stop": stop,
            }

        def _should_stop() -> bool:
            return stop.is_set() or bool(cancel_event is not None and cancel_event.is_set())

        threading.Thread(
            target=self._execute, args=(task_id, command, cwd, _should_stop), daemon=True
        ).start()
        return json.dumps({"status": "ok", "task_id": task_id, "message": f"Started: {command[:80]}"})

    def cancel_session(self, session_id: str) -> list[str]:
        """Kill the running tasks of ``session_id`` and drop its pending results."""
        with self._lock:
            hit = [
                tid for tid, t in self.tasks.items()
                if t.get("session_id") == session_id and t["status"] == "running"
            ]
            for tid in hit:
                self.tasks[tid]["stop"].set()
            self._notifications = [
                n for n in self._notifications if n.get("session_id") != session_id
            ]
        return hit

    def _evict_finished_locked(self) -> None:
        """Drop the oldest finished tasks while the table exceeds ``_MAX_TASKS``."""
        if len(self.tasks) < _MAX_TASKS:
            return
        for tid in [t for t, entry in self.tasks.items() if entry["status"] != "running"]:
            if len(self.tasks) < _MAX_TASKS:
                break
            self.tasks.pop(tid, None)

    def _execute(
        self,
        task_id: str,
        command: str,
        cwd: str | Path | None,
        should_stop: Any = None,
    ) -> None:
        try:
            # Allowlisted env only: never hand the engine's shared credentials
            # to a shell subprocess.
            r = run_capped(
                command, cwd=str(cwd or WORKDIR), env=_subprocess_env(), timeout_s=_TIMEOUT_S,
                should_stop=should_stop,
            )
            stdout = r.stdout + (cap_marker("stdout") if "stdout" in r.capped else "")
            stderr = r.stderr + (cap_marker("stderr") if "stderr" in r.capped else "")
            output = redact_secret_values((stdout + stderr).strip())
            if r.capped:
                output = (
                    f"[output capped: {', '.join(r.capped)} exceeded "
                    f"{bash_tool._OUTPUT_HARD_CAP} chars; process killed]\n" + output
                )
            status = "completed"
        except subprocess.TimeoutExpired:
            output, status = f"Timeout ({_TIMEOUT_S}s)", "timeout"
        except CommandCancelled:
            output, status = "Cancelled: the attempt that started it was cancelled", "cancelled"
        except Exception as e:
            output, status = str(e), "error"
        with self._lock:
            entry = self.tasks.get(task_id)
            if entry is None:  # table reset while the command ran
                return
            entry["status"] = status
            entry["result"] = output or "(no output)"
            if status == "cancelled":
                return
            self._notifications.append({
                "task_id": task_id, "status": status,
                "command": command[:80], "result": (output or "")[:500],
                "session_id": entry.get("session_id"),
            })

    @staticmethod
    def _visible(task_session: Optional[str], session_id: Optional[str]) -> bool:
        """Whether a task/notification of ``task_session`` belongs to caller ``session_id``."""
        return session_id is None or task_session is None or task_session == session_id

    def check(self, task_id: Optional[str] = None, session_id: Optional[str] = None) -> str:
        if session_id is None:
            session_id = _current_session_id()
        if task_id:
            t = self.tasks.get(task_id)
            if not t or not self._visible(t.get("session_id"), session_id):
                return json.dumps({"status": "error", "error": f"Unknown task {task_id}"})
            return json.dumps({"status": t["status"], "command": t["command"][:60],
                                "result": t.get("result") or "(running)"}, ensure_ascii=False)
        lines = [
            f"{tid}: [{t['status']}] {t['command'][:60]}"
            for tid, t in self.tasks.items()
            if self._visible(t.get("session_id"), session_id)
        ]
        return "\n".join(lines) if lines else "No background tasks."

    def drain_notifications(self, session_id: Optional[str] = None) -> List[dict]:
        """Hand over the finished-task notifications of the calling session.

        Another session's results stay queued for that session: a task one
        conversation started must never surface as "please continue with
        these results" in a different conversation.
        """
        if session_id is None:
            session_id = _current_session_id()
        with self._lock:
            mine = [n for n in self._notifications if self._visible(n.get("session_id"), session_id)]
            taken = {id(n) for n in mine}
            self._notifications = [n for n in self._notifications if id(n) not in taken]
        return [{k: v for k, v in n.items() if k != "session_id"} for n in mine]


_BG = BackgroundManager()


def get_background_manager() -> BackgroundManager:
    """Return the global BackgroundManager singleton."""
    return _BG


class BackgroundRunTool(BaseTool):
    name = "background_run"
    description = (
        "Run a shell command on a background thread and return a task_id "
        "immediately. Use for long-running work (model training, bulk data "
        "processing, large installs) that would exceed bash's timeout. The "
        "command runs in the current run_dir (same working directory and same "
        f"minimal no-credentials environment as bash), is killed after {_TIMEOUT_S}s, "
        "and its combined stdout+stderr is kept whole (same "
        f"{_OUTPUT_HARD_CAP // 1_000_000}M-character-per-stream cap as bash: past it "
        "the process is killed and only the kept prefix is returned, marked). Poll "
        "check_background(task_id=...) to get the status and output; a long "
        "result comes back as a preview with its full text offloaded to a file "
        "you can page with read_file, exactly like any other tool result."
    )
    parameters = {"type": "object", "properties": {
        "command": {"type": "string", "description": "Shell command to run in background"},
    }, "required": ["command"]}
    is_readonly = False

    def execute(self, **kw: Any) -> str:
        command = str(kw["command"])
        # Same working directory as bash: the loop injects run_dir into every
        # tool call, so files the model just wrote are where it expects them.
        cwd = kw.get("run_dir") or None
        # Audit-only dangerous-pattern scan, shared with bash (never blocks).
        audit_findings = _audit_command(command)
        if audit_findings:
            emit_progress(
                stage="security_audit",
                message=f"background_run command matched dangerous patterns: {', '.join(audit_findings)}",
            )
        payload = json.loads(_BG.run(command, cwd=cwd))
        if audit_findings:
            payload["security_audit"] = audit_findings
        # Surface the launch in attempt_stats: the actual work runs on a
        # detached thread, outside tool_ms and the budget clamp, and would
        # otherwise be invisible to observability.
        try:
            from src.core.fetch_stats import record_background

            task_id = payload.get("task_id", "")
            if task_id:
                record_background(task_id, command)
        except Exception:
            pass
        return json.dumps(payload, ensure_ascii=False)


class CheckBackgroundTool(BaseTool):
    name = "check_background"
    description = "Check background task status. Omit task_id to list all."
    parameters = {"type": "object", "properties": {
        "task_id": {"type": "string"},
    }, "required": []}
    repeatable = True

    def execute(self, **kw: Any) -> str:
        return _BG.check(kw.get("task_id"))
