"""An attempt that fails before its ReAct loop still ends like any other.

Three guards on the same contract:

* ``SessionService`` writes the ``ok=false`` assistant receipt (and emits
  ``attempt.failed``) when the agent raised outside the loop — e.g. in
  ``build_registry`` or ``ChatLLM()`` — so a consumer polling the message
  list sees the failure at once instead of waiting out its whole budget.
* ``AgentLoop.run()`` turns an exception in its preparation segment (run
  dir, request snapshot, trace file) into the same ``failed`` result dict
  and ``attempt_stats`` frame the in-loop failure path produces.
* A cancel that lands before the loop starts (executor queue, registry
  build) is honoured, never cleared: the loop ends ``cancelled``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from src.session.events import EventBus
from src.session.models import AttemptStatus
from src.session.search import SessionSearchIndex
from src.session.service import SessionService
from src.session.store import SessionStore


class _StubLLM:
    model_name = "stub"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def stream_chat(self, messages, tools=None, on_text_chunk=None,
                    on_reasoning_chunk=None, should_cancel=None, **_: Any):
        from src.providers.chat import LLMResponse

        return LLMResponse(content="done")

    def chat(self, messages, **_: Any):
        from src.providers.chat import LLMResponse

        return LLMResponse(content="done")


def _service(tmp_path: Path) -> tuple[SessionService, SessionSearchIndex, list]:
    bus = EventBus()
    svc = SessionService(
        store=SessionStore(base_dir=tmp_path / "sessions"),
        event_bus=bus,
        runs_dir=tmp_path / "runs",
    )
    idx = SessionSearchIndex(db_path=tmp_path / "sessions.db")
    svc._search_index = idx
    events: list[tuple[str, dict]] = []
    original_emit = bus.emit

    def _emit(session_id: str, event_type: str, data: dict) -> None:
        events.append((event_type, data))
        original_emit(session_id, event_type, data)

    bus.emit = _emit  # type: ignore[method-assign]
    return svc, idx, events


def _run_attempt(svc: SessionService, session_id: str, prompt: str = "hi"):
    async def _go():
        return await svc.send_message(session_id, prompt)

    out = asyncio.run(_go_and_wait(svc, session_id, prompt))
    return out


async def _go_and_wait(svc: SessionService, session_id: str, prompt: str):
    out = await svc.send_message(session_id, prompt)
    # Let the spawned attempt task run to completion.
    import src.session.service as service_mod

    while service_mod._bg_tasks:
        await asyncio.sleep(0.02)
    return out


@pytest.fixture()
def prep_env(monkeypatch):
    monkeypatch.setattr("src.providers.chat.ChatLLM", _StubLLM)
    monkeypatch.setenv("VIBE_MAX_ITERATIONS", "2")


# ── SessionService receipt ───────────────────────────────────────────────────


def test_registry_failure_writes_ok_false_receipt(tmp_path, monkeypatch, prep_env):
    import src.tools as tools_pkg

    def _boom(**_: Any):
        raise RuntimeError("mcp config unreadable")

    monkeypatch.setattr(tools_pkg, "build_registry", _boom)
    svc, idx, events = _service(tmp_path)
    sess = svc.create_session(title="t")

    out = _run_attempt(svc, sess.session_id)

    attempt = svc.store.get_attempt(sess.session_id, out["attempt_id"])
    assert attempt.status == AttemptStatus.FAILED
    assert "mcp config unreadable" in (attempt.error or "")

    msgs = svc.get_messages(sess.session_id)
    receipts = [m for m in msgs if m.role == "assistant" and m.linked_attempt_id == out["attempt_id"]]
    assert len(receipts) == 1
    meta = receipts[0].metadata
    assert meta["ok"] is False
    assert meta["status"] == "failed"
    assert "mcp config unreadable" in meta["error"]
    assert receipts[0].content.startswith("Execution failed:")

    failed = [d for et, d in events if et == "attempt.failed"]
    assert failed and failed[0]["attempt_id"] == out["attempt_id"]
    assert sess.session_id not in svc._inflight
    idx.close()


def test_receipt_steps_are_independent(tmp_path, monkeypatch, prep_env):
    """A failing store (full disk) must not stop the event, and vice versa."""
    import src.tools as tools_pkg

    monkeypatch.setattr(tools_pkg, "build_registry", lambda **_: (_ for _ in ()).throw(OSError("disk full")))
    svc, idx, events = _service(tmp_path)
    sess = svc.create_session(title="t")

    def _update_boom(attempt):
        raise OSError("disk full")

    monkeypatch.setattr(svc.store, "update_attempt", _update_boom)

    out = _run_attempt(svc, sess.session_id)

    receipts = [
        m for m in svc.get_messages(sess.session_id)
        if m.role == "assistant" and m.linked_attempt_id == out["attempt_id"]
    ]
    assert len(receipts) == 1 and receipts[0].metadata["ok"] is False
    assert any(et == "attempt.failed" for et, _ in events)
    idx.close()


def test_receipt_not_duplicated_when_failure_follows_the_normal_receipt(tmp_path, monkeypatch, prep_env):
    """An exception after the normal receipt was stored writes no second one."""
    svc, idx, events = _service(tmp_path)
    sess = svc.create_session(title="t")

    def _index_boom(*a, **k):
        raise RuntimeError("fts locked")

    calls = {"n": 0}

    def _index(session_id, role, content):
        calls["n"] += 1
        if role == "assistant":
            raise RuntimeError("fts locked")

    monkeypatch.setattr(svc._search_index, "index_message", _index)

    out = _run_attempt(svc, sess.session_id)

    receipts = [
        m for m in svc.get_messages(sess.session_id)
        if m.role == "assistant" and m.linked_attempt_id == out["attempt_id"]
    ]
    assert len(receipts) == 1
    idx.close()


# ── cancel before the loop registers ─────────────────────────────────────────


def test_cancel_during_registry_build_is_delivered(tmp_path, monkeypatch, prep_env):
    import src.tools as tools_pkg

    real_build = tools_pkg.build_registry
    svc, idx, events = _service(tmp_path)
    sess = svc.create_session(title="t")

    def _build_then_cancel(**kwargs: Any):
        # The cancel arrives while there is no loop to signal yet.
        assert svc.cancel_current(sess.session_id) is True
        return real_build(**kwargs)

    monkeypatch.setattr(tools_pkg, "build_registry", _build_then_cancel)

    out = _run_attempt(svc, sess.session_id)

    attempt = svc.store.get_attempt(sess.session_id, out["attempt_id"])
    assert attempt.status == AttemptStatus.FAILED
    assert "cancelled" in (attempt.error or "")
    assert sess.session_id not in svc._pending_cancel
    idx.close()


def test_cancel_current_without_any_attempt_is_false(tmp_path):
    svc, idx, _ = _service(tmp_path)
    assert svc.cancel_current("nope") is False
    assert not svc._pending_cancel
    idx.close()


# ── AgentLoop preparation segment ────────────────────────────────────────────


def _build_agent(tmp_path: Path, events: list) -> Any:
    from src.agent.loop import AgentLoop
    from src.memory.persistent import PersistentMemory
    from src.tools import build_registry

    pm = PersistentMemory()
    agent = AgentLoop(
        registry=build_registry(persistent_memory=pm, include_shell_tools=False),
        llm=_StubLLM(),
        event_callback=lambda et, data: events.append((et, data)),
        max_iterations=2,
        persistent_memory=pm,
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    agent.memory.run_dir = str(run_dir)
    return agent


def test_prep_failure_returns_failed_result_and_attempt_stats(tmp_path, monkeypatch):
    import src.agent.loop as loop_mod

    def _save_boom(self, run_dir, user_message, meta):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(loop_mod.RunStateStore, "save_request", _save_boom)
    events: list = []
    agent = _build_agent(tmp_path, events)

    result = agent.run(user_message="anything", session_id="")

    assert result["status"] == "failed"
    assert "No space left" in result["reason"]
    assert result["error_code"] == "agent_loop_error"
    assert result["iterations"] == 0
    stats = [d for et, d in events if et == "attempt_stats"]
    assert len(stats) == 1
    assert stats[0]["status"] == "error"
    assert "No space left" in stats[0]["reason"]


def test_prep_failure_before_run_dir_exists_still_reports(tmp_path, monkeypatch):
    import src.agent.loop as loop_mod

    monkeypatch.setattr(loop_mod, "RUNS_DIR", tmp_path / "runs")

    def _mkdir_boom(self, *a, **k):
        raise PermissionError("read-only file system")

    events: list = []
    agent = _build_agent(tmp_path, events)
    agent.memory.run_dir = None
    monkeypatch.setattr(Path, "mkdir", _mkdir_boom)
    try:
        result = agent.run(user_message="anything", session_id="")
    finally:
        monkeypatch.undo()

    assert result["status"] == "failed"
    assert result["run_dir"] is None and result["run_id"] is None
    assert [d["status"] for et, d in events if et == "attempt_stats"] == ["error"]


def test_cancel_set_before_run_is_honoured(tmp_path):
    events: list = []
    agent = _build_agent(tmp_path, events)
    agent.cancel()

    result = agent.run(user_message="anything", session_id="")

    assert result["status"] == "cancelled"
    assert agent._cancel_event.is_set()
