"""A swarm tail report reaches exactly one request stream.

The tail meter reports what a swarm run spent after its attempt stopped
waiting (``source="swarm_tail"``, stamped with that attempt's id). The
caller bills what arrives on a request's stream, so:

* with an attempt of the session running, the report goes out live;
* with none, it is parked next to the session and sent at the start of the
  session's next attempt — once, still marked ``swarm_tail`` with the
  original attempt id, plus ``deferred: true``;
* the next attempt's replay window carries it, and a reconnect that resumes
  by event id does not deliver it twice.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

import pytest

from src.session import swarm_tail
from src.session.events import EventBus
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


@pytest.fixture()
def svc(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("src.providers.chat.ChatLLM", _StubLLM)
    monkeypatch.setenv("VIBE_MAX_ITERATIONS", "2")
    service = SessionService(
        store=SessionStore(base_dir=tmp_path / "sessions"),
        event_bus=EventBus(),
        runs_dir=tmp_path / "runs",
    )
    service._search_index = SessionSearchIndex(db_path=tmp_path / "sessions.db")
    yield service
    service._search_index.close()


def _capture_callbacks(monkeypatch) -> list:
    """Keep each attempt's session event callback (what the swarm tool gets)."""
    import src.tools as tools_pkg

    real_build = tools_pkg.build_registry
    callbacks: list = []

    def _build(**kwargs: Any):
        callbacks.append(kwargs["event_callback"])
        return real_build(**kwargs)

    monkeypatch.setattr(tools_pkg, "build_registry", _build)
    return callbacks


def _ask(svc: SessionService, session_id: str, prompt: str = "q") -> str:
    async def go() -> str:
        out = await svc.send_message(session_id, prompt)
        import src.session.service as service_mod

        while service_mod._bg_tasks:
            await asyncio.sleep(0.02)
        return out["attempt_id"]

    return asyncio.run(go())


def _tail_report(tokens: int, run_id: str = "run-1") -> dict:
    return {
        "input_tokens": tokens, "output_tokens": tokens // 10,
        "total_tokens": tokens + tokens // 10, "source": "swarm_tail",
        "run_id": run_id, "tail_key": f"{run_id}:{tokens}:{tokens // 10}",
    }


def _tails(events) -> list:
    return [e for e in events if e.event_type == "llm_usage" and e.data.get("source") == "swarm_tail"]


def _from_meter_thread(callback, data: dict) -> None:
    t = threading.Thread(target=callback, args=("llm_usage", data))
    t.start()
    t.join()


def test_tail_with_no_running_attempt_is_sent_once_at_the_next_attempt(svc, monkeypatch) -> None:
    callbacks = _capture_callbacks(monkeypatch)
    sid = svc.create_session(title="t").session_id
    first = _ask(svc, sid)

    # The run ends between two questions: nobody is streaming.
    _from_meter_thread(callbacks[0], _tail_report(8000))
    assert _tails(svc.event_bus.replay(sid, replay_all=True)) == []
    assert (svc.store.base_dir / sid / swarm_tail.PENDING_FILE).exists()

    second = _ask(svc, sid)
    window = svc.event_bus.replay(sid, replay_all=True, since_attempt=second)
    tails = _tails(window)
    assert len(tails) == 1
    assert tails[0].data["attempt_id"] == first
    assert tails[0].data["deferred"] is True
    assert tails[0].data["input_tokens"] == 8000
    assert not (svc.store.base_dir / sid / swarm_tail.PENDING_FILE).exists()

    third = _ask(svc, sid)
    assert _tails(svc.event_bus.replay(sid, replay_all=True, since_attempt=third)) == []
    assert len(_tails(svc.event_bus.replay(sid, replay_all=True))) == 1


def test_tail_while_an_attempt_streams_goes_out_live(svc) -> None:
    sid = svc.create_session(title="t").session_id
    svc._begin_streaming(sid, "B")

    svc._deliver_swarm_tail(sid, {**_tail_report(500), "attempt_id": "A"})

    live = _tails(svc.event_bus.replay(sid, replay_all=True))
    assert len(live) == 1 and "deferred" not in live[0].data
    assert not (svc.store.base_dir / sid / swarm_tail.PENDING_FILE).exists()

    svc._end_streaming(sid, "B")
    svc._deliver_swarm_tail(sid, {**_tail_report(700, "run-2"), "attempt_id": "A"})
    assert len(_tails(svc.event_bus.replay(sid, replay_all=True))) == 1
    assert (svc.store.base_dir / sid / swarm_tail.PENDING_FILE).exists()


def test_deferred_tail_is_billed_once_across_a_reconnect(svc) -> None:
    """R1 + R2: a router-like consumer (high-water mark by event id) sees the
    deferred tail once, even when it reconnects after it."""
    sid = svc.create_session(title="t").session_id
    svc._deliver_swarm_tail(sid, {**_tail_report(3000), "attempt_id": "A"})
    bus = svc.event_bus
    bus.emit(sid, "attempt.created", {"attempt_id": "B"})
    svc._begin_streaming(sid, "B")
    bus.emit(sid, "llm_usage", {"attempt_id": "B", "input_tokens": 10})

    billed = []
    hwm = 0

    def take(events) -> None:
        nonlocal hwm
        for ev in events:
            seq = int(ev.event_id.rsplit("-", 1)[1])
            if seq <= hwm:
                continue
            hwm = seq
            if ev.event_type == "llm_usage":
                billed.append(ev.data["input_tokens"])

    first = bus.replay(sid, replay_all=True, since_attempt="B")
    take(first)
    bus.emit(sid, "llm_usage", {"attempt_id": "B", "input_tokens": 20})
    take(bus.replay(sid, first[-1].event_id, replay_all=True, since_attempt="B"))
    # A consumer that fell back to the whole window gains nothing either.
    take(bus.replay(sid, "legacy-id", replay_all=True, since_attempt="B"))

    assert sorted(billed) == [10, 20, 3000]


def test_parked_tail_then_a_resume_bill_the_run_exactly_once(svc, tmp_path, monkeypatch) -> None:
    """Wait budget runs out → run ends between questions (tail parked) → the
    next attempt resumes the same run: every token is billed once."""
    import time
    import uuid
    from datetime import datetime, timezone

    import src.tools.swarm_tool as swarm_tool
    from src.swarm.models import RunStatus, SwarmAgentSpec, SwarmRun, SwarmTask
    from src.swarm.store import SwarmStore

    monkeypatch.setattr(swarm_tool, "_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(swarm_tool, "_TAIL_POLL_SECONDS", 0.02)
    store = SwarmStore(tmp_path / "swarm-runs")
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    run = SwarmRun(
        id=run_id, preset_name="risk_committee", status=RunStatus.running,
        created_at=datetime.now(timezone.utc).isoformat(),
        agents=[SwarmAgentSpec(id="a", role="A", system_prompt="x")],
        tasks=[SwarmTask(id="t1", agent_id="a", prompt_template="do x")],
    )
    run.total_input_tokens, run.total_output_tokens = 1000, 100
    store.create_run(run)
    sid = svc.create_session(title="t").session_id

    def session_callback(attempt_id: str):
        def cb(event_type: str, data: dict) -> None:  # mirrors SessionService._run_with_agent
            data["attempt_id"] = attempt_id
            if event_type == "llm_usage" and data.get("source") == "swarm_tail":
                svc._deliver_swarm_tail(sid, data)
                return
            svc.event_bus.emit(sid, event_type, data)
        return cb

    def wait(tool, **kw):
        return tool._wait_for_run(
            store=store, run_id=run_id, preset="risk_committee", variables={},
            run_agents=1, run_tasks=1, record=lambda *a, **k: None, **kw,
        )

    svc._begin_streaming(sid, "A")
    monkeypatch.setattr(swarm_tool, "_MAX_WAIT_SECONDS", 0)
    wait(swarm_tool.SwarmTool(event_callback=session_callback("A"), session_id=sid))
    svc._end_streaming(sid, "A")

    run.total_input_tokens, run.total_output_tokens = 9000, 900
    run.status = RunStatus.completed
    store.update_run(run)
    pending = svc.store.base_dir / sid / swarm_tail.PENDING_FILE
    deadline = time.monotonic() + 3
    while not pending.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert pending.exists()

    svc._begin_streaming(sid, "B")
    monkeypatch.setattr(swarm_tool, "_MAX_WAIT_SECONDS", 5)
    wait(swarm_tool.SwarmTool(event_callback=session_callback("B"), session_id=sid), resumed=True)

    usage = [e.data for e in svc.event_bus.replay(sid, replay_all=True) if e.event_type == "llm_usage"]
    assert sum(u["input_tokens"] for u in usage) == 9000
    assert sum(u["output_tokens"] for u in usage) == 900
    assert [u.get("deferred") for u in usage if u["source"] == "swarm_tail"] == [True]


def test_parking_keeps_one_copy_per_tail_key(tmp_path) -> None:
    root = tmp_path / "sessions"
    (root / "s").mkdir(parents=True)
    report = _tail_report(100)

    assert swarm_tail.park(root, "s", report)
    assert swarm_tail.park(root, "s", report)
    assert swarm_tail.park(root, "s", _tail_report(200, "run-2"))

    taken = swarm_tail.take(root, "s")
    assert [t["tail_key"] for t in taken] == ["run-1:100:10", "run-2:200:20"]
    assert swarm_tail.take(root, "s") == []


def test_parking_never_recreates_a_deleted_session(tmp_path) -> None:
    from src.session import tombstone

    root = tmp_path / "sessions"
    assert swarm_tail.park(root, "gone", _tail_report(100)) is False
    assert not (root / "gone").exists()

    (root / "s").mkdir(parents=True)
    tombstone.mark("s", root)
    assert swarm_tail.park(root, "s", _tail_report(100)) is False
    assert not (root / "s" / swarm_tail.PENDING_FILE).exists()
