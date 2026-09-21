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
from src.tools.bash_tool import _OUTPUT_HARD_CAP, _audit_command, _cap_output
from src.tools.redaction import redact_secret_values
from src.tools.subprocess_env import _subprocess_env

WORKDIR = Path(__file__).resolve().parents[2]
_TIMEOUT_S = 300
# Output is kept whole (up to bash's memory hard cap, marked when it fires):
# ``check_background`` hands it to the loop, whose single truncation layer
# (``agent.tool_result_store``) previews it and offloads the full text for
# ``read_file`` paging — a second clip here would leave that copy incomplete.
# Finished tasks are evicted oldest-first past this many entries; running
# ones are never dropped. Bounds the per-process table (the singleton lives
# as long as the engine does) without losing a result before it is read.
_MAX_TASKS = 50


class BackgroundManager:
    """Background thread execution + notification queue."""

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

    def run(self, command: str, cwd: str | Path | None = None) -> str:
        """Start a background task and return its task_id.

        Args:
            command: Shell command to execute.
            cwd: Working directory for the command; defaults to the engine
                install directory.

        Returns:
            JSON string containing status and task_id.
        """
        task_id = uuid.uuid4().hex[:8]
        with self._lock:
            self._evict_finished_locked()
            self.tasks[task_id] = {"status": "running", "result": None, "command": command}
        threading.Thread(target=self._execute, args=(task_id, command, cwd), daemon=True).start()
        return json.dumps({"status": "ok", "task_id": task_id, "message": f"Started: {command[:80]}"})

    def _evict_finished_locked(self) -> None:
        """Drop the oldest finished tasks while the table exceeds ``_MAX_TASKS``."""
        if len(self.tasks) < _MAX_TASKS:
            return
        for tid in [t for t, entry in self.tasks.items() if entry["status"] != "running"]:
            if len(self.tasks) < _MAX_TASKS:
                break
            self.tasks.pop(tid, None)

    def _execute(self, task_id: str, command: str, cwd: str | Path | None) -> None:
        try:
            # Allowlisted env only: never hand the engine's shared credentials
            # to a shell subprocess.
            r = subprocess.run(command, shell=True, cwd=str(cwd or WORKDIR), env=_subprocess_env(),
                               capture_output=True, text=True, timeout=_TIMEOUT_S,
                               encoding="utf-8", errors="replace")
            output = redact_secret_values(
                (_cap_output(r.stdout, "stdout") + _cap_output(r.stderr, "stderr")).strip()
            )
            status = "completed"
        except subprocess.TimeoutExpired:
            output, status = f"Timeout ({_TIMEOUT_S}s)", "timeout"
        except Exception as e:
            output, status = str(e), "error"
        with self._lock:
            entry = self.tasks.get(task_id)
            if entry is None:  # table reset while the command ran
                return
            entry["status"] = status
            entry["result"] = output or "(no output)"
            self._notifications.append({
                "task_id": task_id, "status": status,
                "command": command[:80], "result": (output or "")[:500],
            })

    def check(self, task_id: Optional[str] = None) -> str:
        if task_id:
            t = self.tasks.get(task_id)
            if not t:
                return json.dumps({"status": "error", "error": f"Unknown task {task_id}"})
            return json.dumps({"status": t["status"], "command": t["command"][:60],
                                "result": t.get("result") or "(running)"}, ensure_ascii=False)
        lines = [f"{tid}: [{t['status']}] {t['command'][:60]}" for tid, t in self.tasks.items()]
        return "\n".join(lines) if lines else "No background tasks."

    def drain_notifications(self) -> List[dict]:
        with self._lock:
            notifs = list(self._notifications)
            self._notifications.clear()
        return notifs


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
        f"{_OUTPUT_HARD_CAP // 1_000_000}M-character memory guard as bash). Poll "
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
