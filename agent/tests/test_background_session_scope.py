"""Background tasks belong to the session that started them.

The manager is one per engine process: results used to be drained into
whichever attempt ran next (possibly another conversation), running tasks had
no count limit in a 2 GB guest, and cancelling the attempt left its commands
running.
"""

from __future__ import annotations

import contextvars
import json
import threading
import time

import pytest

import src.tools.background_tools as bg
from src.core.logging_setup import bind_log_context
from src.tools.background_tools import BackgroundManager
from src.tools.bash_tool import CommandCancelled, run_capped


def _in_session(session_id: str, fn, *args, **kwargs):
    ctx = contextvars.copy_context()

    def _run():
        bind_log_context(session_id=session_id)
        return fn(*args, **kwargs)

    return ctx.run(_run)


def _wait(mgr: BackgroundManager, task_id: str, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while mgr.tasks[task_id]["status"] == "running" and time.monotonic() < deadline:
        time.sleep(0.02)
    return mgr.tasks[task_id]


def test_notifications_are_drained_only_by_their_session() -> None:
    mgr = BackgroundManager()
    tid = json.loads(_in_session("sess-A", mgr.run, "echo from-A"))["task_id"]
    _wait(mgr, tid)

    assert _in_session("sess-B", mgr.drain_notifications) == []
    mine = _in_session("sess-A", mgr.drain_notifications)
    assert [n["task_id"] for n in mine] == [tid]
    assert "session_id" not in mine[0]
    assert _in_session("sess-A", mgr.drain_notifications) == []


def test_check_hides_other_sessions_tasks() -> None:
    mgr = BackgroundManager()
    tid = json.loads(_in_session("sess-A", mgr.run, "echo a"))["task_id"]
    _wait(mgr, tid)

    assert _in_session("sess-B", mgr.check) == "No background tasks."
    assert "Unknown task" in _in_session("sess-B", mgr.check, tid)
    assert tid in _in_session("sess-A", mgr.check)


def test_contextless_callers_keep_the_legacy_view() -> None:
    mgr = BackgroundManager()
    tid = json.loads(mgr.run("echo cli"))["task_id"]
    _wait(mgr, tid)
    assert [n["task_id"] for n in mgr.drain_notifications()] == [tid]


def test_running_tasks_are_capped_per_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bg, "_MAX_RUNNING_PER_SESSION", 2)
    mgr = BackgroundManager()
    ok = [json.loads(_in_session("s", mgr.run, "sleep 5"))["status"] for _ in range(2)]
    third = json.loads(_in_session("s", mgr.run, "sleep 5"))
    other = json.loads(_in_session("t", mgr.run, "true"))
    try:
        assert ok == ["ok", "ok"]
        assert third["status"] == "error"
        assert third["error_code"] == "too_many_background_tasks"
        assert other["status"] == "ok"
    finally:
        mgr.cancel_session("s")


def test_attempt_cancel_kills_the_task() -> None:
    mgr = BackgroundManager()
    cancel = threading.Event()
    tid = json.loads(
        _in_session("s", mgr.run, "sleep 30; echo late", cancel_event=cancel)
    )["task_id"]
    time.sleep(0.2)
    cancel.set()
    task = _wait(mgr, tid, timeout=5.0)
    assert task["status"] == "cancelled"
    assert _in_session("s", mgr.drain_notifications) == []


def test_session_delete_kills_its_tasks_only() -> None:
    mgr = BackgroundManager()
    mine = json.loads(_in_session("gone", mgr.run, "sleep 30"))["task_id"]
    keep = json.loads(_in_session("stay", mgr.run, "sleep 0.2; echo ok"))["task_id"]
    assert mgr.cancel_session("gone") == [mine]
    assert _wait(mgr, mine, timeout=5.0)["status"] == "cancelled"
    assert _wait(mgr, keep)["status"] == "completed"


def test_run_capped_stops_when_asked(tmp_path) -> None:
    flag = {"stop": False}
    t0 = time.monotonic()

    def _stop() -> bool:
        return flag["stop"] or time.monotonic() - t0 > 0.3

    with pytest.raises(CommandCancelled):
        run_capped("sleep 30", cwd=str(tmp_path), env={"PATH": "/usr/bin:/bin"}, timeout_s=20,
                   should_stop=_stop)
    assert time.monotonic() - t0 < 5


def test_bash_returns_a_cancelled_error_when_the_attempt_is_cancelled(tmp_path) -> None:
    from src.core.cancel import bind_cancel_event
    from src.tools.bash_tool import BashTool

    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    ctx = contextvars.copy_context()

    def _run() -> str:
        bind_cancel_event(cancel)
        return BashTool().execute(command="sleep 30", run_dir=str(tmp_path))

    t0 = time.monotonic()
    payload = json.loads(ctx.run(_run))
    assert payload["error_code"] == "cancelled"
    assert time.monotonic() - t0 < 5
