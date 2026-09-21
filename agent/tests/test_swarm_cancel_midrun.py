"""A swarm run responds to cancellation inside a layer, not only between
layers, and a session-level cancel reaches runs its attempt left behind.

* ``run_worker`` checks the run's cancel event at the top of every
  iteration (and hands it to the tool watchdog): a cancel that lands while
  a tool runs ends the worker with ``status="cancelled"`` before the next
  LLM call.
* ``_run_worker_with_retries`` checks it before each retry, so a cancelled
  run does not burn ``max_retries`` more attempts of a failing task.
* ``_execute_run`` records such a task as ``cancelled`` (not ``failed``)
  and finishes the run ``cancelled``.
* The cancel registry is process-wide: ``cancel_session_runs`` stops the
  runs registered to a session even though the runtime instance that
  started them is gone, and ``SessionService.cancel_current`` uses it.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any
from unittest.mock import patch

from src.providers.chat import LLMResponse, ToolCallRequest
from src.swarm import runtime as rt
from src.swarm.models import (
    RunStatus,
    SwarmAgentSpec,
    SwarmRun,
    SwarmTask,
    TaskStatus,
    WorkerResult,
)
from src.swarm.store import SwarmStore
import src.swarm.worker as worker_mod


class _NoopRegistry:
    """Registry with one side-effect-free tool whose execution cancels the run."""

    def __init__(self, cancel_event: threading.Event) -> None:
        self._cancel_event = cancel_event
        self.executions = 0

    def get_definitions(self) -> list[dict]:
        return [{"name": "noop", "description": "noop", "parameters": {}}]

    def get(self, name: str) -> Any:
        return None

    def execute(self, name: str, args: dict) -> str:
        self.executions += 1
        # The cancel lands while the tool is running.
        self._cancel_event.set()
        return "ok"


class _ToolCallingLLM:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, *args: Any, **kwargs: Any) -> "_ToolCallingLLM":
        return self

    def stream_chat(self, messages, tools=None, on_text_chunk=None, timeout=None) -> LLMResponse:
        self.calls += 1
        return LLMResponse(
            content="calling noop",
            tool_calls=[ToolCallRequest(id="c1", name="noop", arguments={})],
        )


def _spec(retries: int = 0) -> SwarmAgentSpec:
    return SwarmAgentSpec(
        id="analyst", role="r", system_prompt="s", tools=[], skills=[],
        max_iterations=5, timeout_seconds=60, max_retries=retries,
    )


def test_worker_stops_at_the_next_iteration_after_a_mid_tool_cancel(tmp_path):
    cancel_event = threading.Event()
    registry = _NoopRegistry(cancel_event)
    llm = _ToolCallingLLM()
    task = SwarmTask(id="t1", agent_id="analyst", prompt_template="Go.")
    with (
        patch.object(worker_mod, "build_swarm_registry", lambda *a, **k: registry),
        patch.object(worker_mod, "ChatLLM", llm),
    ):
        result = worker_mod.run_worker(
            agent_spec=_spec(), task=task, upstream_summaries={}, user_vars={},
            run_dir=tmp_path, cancel_event=cancel_event,
        )

    assert result.status == "cancelled"
    assert llm.calls == 1, "no further LLM call once the run is cancelled"
    assert registry.executions == 1
    assert result.iterations == 1


def test_worker_without_cancel_event_keeps_running(tmp_path):
    """Sanity: an unset / absent event changes nothing (the LLM stub keeps
    calling tools until the iteration limit)."""
    registry = _NoopRegistry(threading.Event())
    registry.execute = lambda name, args: "ok"  # type: ignore[method-assign]
    llm = _ToolCallingLLM()
    task = SwarmTask(id="t1", agent_id="analyst", prompt_template="Go.")
    with (
        patch.object(worker_mod, "build_swarm_registry", lambda *a, **k: registry),
        patch.object(worker_mod, "ChatLLM", llm),
    ):
        result = worker_mod.run_worker(
            agent_spec=_spec(), task=task, upstream_summaries={}, user_vars={},
            run_dir=tmp_path,
        )
    assert result.status != "cancelled"
    assert llm.calls == 5


def test_retry_loop_stops_before_the_next_retry(tmp_path, monkeypatch):
    store = SwarmStore(base_dir=tmp_path)
    runtime = rt.SwarmRuntime(store=store)
    cancel_event = threading.Event()
    calls = {"n": 0}

    def fake_worker(*a, **k):
        calls["n"] += 1
        assert k.get("cancel_event") is cancel_event
        cancel_event.set()  # cancel arrives during the first (failing) run
        return WorkerResult(status="failed", summary="", error="boom")

    monkeypatch.setattr(rt, "run_worker", fake_worker)
    result = runtime._run_worker_with_retries(
        agent_spec=_spec(retries=2), task=SwarmTask(id="t1", agent_id="analyst", prompt_template="x"),
        upstream_summaries={}, user_vars={}, run_dir=tmp_path, event_callback=None,
        run_id="r", cancel_event=cancel_event,
    )

    assert calls["n"] == 1
    assert result.status == "cancelled"
    assert "before retry" in (result.error or "")


def test_run_records_cancelled_task_and_finishes_cancelled(tmp_path, monkeypatch):
    store = SwarmStore(base_dir=tmp_path)
    runtime = rt.SwarmRuntime(store=store, max_workers=1)
    from datetime import datetime, timezone

    run = SwarmRun(
        id="r-cancel", preset_name="demo", user_vars={},
        created_at=datetime.now(timezone.utc).isoformat(),
        agents=[_spec()],
        tasks=[SwarmTask(id="t1", agent_id="analyst", prompt_template="x")],
    )
    store.create_run(run)
    cancel_event = threading.Event()

    def fake_worker(*a, **k):
        k["cancel_event"].set()
        return WorkerResult(status="cancelled", summary="partial", error="run cancelled")

    monkeypatch.setattr(rt, "run_worker", fake_worker)
    runtime._execute_run(run, cancel_event)

    reloaded = store.load_run(run.id)
    assert reloaded.status == RunStatus.cancelled
    assert reloaded.tasks[0].status == TaskStatus.cancelled
    types = [e.type for e in store.read_events(run.id)]
    assert "task_cancelled" in types and "task_failed" not in types


def test_cancel_registry_is_process_wide_and_session_scoped():
    ev = threading.Event()
    with rt._REGISTRY_LOCK:
        rt._CANCEL_EVENTS["run-a"] = ev
    try:
        rt.register_session_run("sess-1", "run-a")
        rt.register_session_run("", "run-a")  # no session: ignored

        assert rt.cancel_session_runs("other") == []
        assert rt.cancel_session_runs("sess-1") == ["run-a"]
        assert ev.is_set()
        # A different runtime instance can cancel it too.
        assert rt.SwarmRuntime(store=SwarmStore(base_dir=Path("/nonexistent"))).cancel_run("run-a") is True
    finally:
        rt._forget_run("run-a")
    assert rt.cancel_run("run-a") is False
    assert rt.cancel_session_runs("sess-1") == []


def test_session_cancel_reaches_a_run_left_behind(tmp_path):
    """The attempt already returned wait_budget_exhausted (no waiter left);
    cancelling the session still stops the run."""
    from src.session.events import EventBus
    from src.session.service import SessionService
    from src.session.store import SessionStore

    svc = SessionService(
        store=SessionStore(base_dir=tmp_path / "sessions"),
        event_bus=EventBus(),
        runs_dir=tmp_path / "runs",
    )
    ev = threading.Event()
    with rt._REGISTRY_LOCK:
        rt._CANCEL_EVENTS["run-b"] = ev
    try:
        rt.register_session_run("sess-2", "run-b")
        assert svc.cancel_current("sess-2") is True
        assert ev.is_set()
    finally:
        rt._forget_run("run-b")
