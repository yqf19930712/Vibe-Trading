"""Event stream is scoped per attempt for replay and lossless for accounting.

A follow-up question on the same session subscribes with ``replay=active``;
the per-session buffer still holds the previous attempt's ``llm_usage`` and
``attempt_stats``, which the caller bills. Replay must start at the running
attempt. Under back-pressure only stream deltas may be shed.
"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from src.session.events import EventBus, SSEEvent, _SubscriberQueue


def _previous_turn(bus: EventBus, sid: str) -> None:
    for ev, data in [
        ("attempt.created", {}),
        ("attempt.started", {}),
        ("llm_usage", {"input_tokens": 9000, "output_tokens": 3000}),
        ("llm_usage", {"input_tokens": 400000, "output_tokens": 90000, "source": "swarm"}),
        ("attempt_stats", {"status": "ok", "iterations": 12}),
        ("attempt.completed", {}),
    ]:
        bus.emit(sid, ev, {**data, "attempt_id": "A"})


def _collect(bus: EventBus, sid: str, n: int, **kw) -> list[SSEEvent]:
    async def main() -> list[SSEEvent]:
        got = []
        agen = bus.subscribe(sid, None, **kw)
        try:
            for _ in range(n):
                got.append(await asyncio.wait_for(agen.__anext__(), timeout=1.0))
        except asyncio.TimeoutError:
            pass
        finally:
            await agen.aclose()
        return got

    return asyncio.run(main())


def test_replay_active_skips_the_previous_attempt() -> None:
    bus = EventBus()
    _previous_turn(bus, "s")
    bus.emit("s", "message.received", {"role": "user", "content": "follow-up"})
    created = bus.emit("s", "attempt.created", {"attempt_id": "B"})
    started = bus.emit("s", "attempt.started", {"attempt_id": "B"})

    replayed = bus.replay("s", replay_all=True, since_attempt="B")

    assert replayed == [created, started]
    usage = sum(
        e.data.get("input_tokens", 0) + e.data.get("output_tokens", 0)
        for e in replayed
        if e.event_type == "llm_usage"
    )
    assert usage == 0


def test_replay_active_through_subscribe_matches_replay() -> None:
    bus = EventBus()
    _previous_turn(bus, "s")
    created = bus.emit("s", "attempt.created", {"attempt_id": "B"})
    usage = bus.emit("s", "llm_usage", {"input_tokens": 10, "output_tokens": 2, "attempt_id": "B"})

    got = _collect(bus, "s", 2, replay_all=True, since_attempt="B")

    assert got == [created, usage]


def test_late_event_of_the_old_attempt_is_not_replayed() -> None:
    bus = EventBus()
    _previous_turn(bus, "s")
    created = bus.emit("s", "attempt.created", {"attempt_id": "B"})
    # The old attempt was still finishing when B was created.
    bus.emit("s", "llm_usage", {"input_tokens": 5, "output_tokens": 5, "attempt_id": "A"})
    mine = bus.emit("s", "tool_call", {"tool": "bash", "attempt_id": "B"})

    assert bus.replay("s", replay_all=True, since_attempt="B") == [created, mine]


def test_anchor_evicted_falls_back_to_attempt_id_filter() -> None:
    bus = EventBus(max_buffer_size=5)
    _previous_turn(bus, "s")
    bus.emit("s", "attempt.created", {"attempt_id": "B"})
    kept = [bus.emit("s", "tool_call", {"i": i, "attempt_id": "B"}) for i in range(5)]

    replayed = bus.replay("s", replay_all=True, since_attempt="B")

    assert replayed == kept


def test_without_since_attempt_the_legacy_replay_is_unchanged() -> None:
    bus = EventBus()
    _previous_turn(bus, "s")
    assert len(bus.replay("s", replay_all=True)) == 6


# ── buffer and subscriber back-pressure ──────────────────────────────────────


def test_buffer_trim_sheds_text_deltas_before_accounting_events() -> None:
    bus = EventBus(max_buffer_size=10)
    created = bus.emit("s", "attempt.created", {"attempt_id": "A"})
    usage = bus.emit("s", "llm_usage", {"input_tokens": 1, "attempt_id": "A"})
    for i in range(50):
        bus.emit("s", "text_delta", {"delta": str(i), "attempt_id": "A"})
    stats = bus.emit("s", "attempt_stats", {"status": "ok", "attempt_id": "A"})

    buffered = bus.replay("s", replay_all=True)

    assert len(buffered) == 10
    assert created in buffered and usage in buffered and stats in buffered


def test_full_subscriber_queue_drops_deltas_not_usage() -> None:
    q = _SubscriberQueue(maxsize=3)
    deltas = [SSEEvent(event_type="text_delta", data={"delta": "x"}) for _ in range(3)]
    for d in deltas:
        assert q.put(d)
    usage = SSEEvent(event_type="llm_usage", data={"input_tokens": 1})
    stats = SSEEvent(event_type="attempt_stats", data={})

    assert q.put(SSEEvent(event_type="text_delta", data={"delta": "late"})) is False
    assert q.put(usage) is True
    assert q.put(stats) is True
    assert len(q) == 3

    async def drain() -> list[SSEEvent]:
        return [await q.get(timeout=0.1) for _ in range(len(q))]

    out = asyncio.run(drain())
    assert usage in out and stats in out


def test_queue_of_only_lossless_events_grows_past_the_bound() -> None:
    q = _SubscriberQueue(maxsize=2)
    for i in range(5):
        assert q.put(SSEEvent(event_type="llm_usage", data={"i": i}))
    assert len(q) == 5


def test_swarm_worker_text_is_lossy_but_worker_lifecycle_is_not() -> None:
    text = SSEEvent(event_type="swarm.event", data={"event": {"type": "worker_text"}})
    done = SSEEvent(event_type="swarm.event", data={"event": {"type": "worker_completed"}})
    assert text.lossy and not done.lossy


def test_subscriber_times_out_into_heartbeat() -> None:
    bus = EventBus()

    async def main() -> str:
        agen = bus.subscribe("s")
        q_task = asyncio.ensure_future(agen.__anext__())
        await asyncio.sleep(0)
        bus.emit("s", "attempt.started", {"attempt_id": "A"})
        ev = await asyncio.wait_for(q_task, timeout=1.0)
        await agen.aclose()
        return ev.event_type

    assert asyncio.run(main()) == "attempt.started"


# ── endpoint wiring ──────────────────────────────────────────────────────────


def test_events_endpoint_passes_the_running_attempt(monkeypatch, tmp_path) -> None:
    import api_server
    from src.session.models import Attempt, Session
    from src.session.store import SessionStore

    store = SessionStore(tmp_path / "sessions")
    session = Session(title="t")
    store.create_session(session)
    attempt = Attempt(session_id=session.session_id, prompt="q")
    attempt.mark_running()
    store.create_attempt(attempt)
    session.last_attempt_id = attempt.attempt_id
    store.update_session(session)

    seen: dict = {}

    class _Bus:
        async def subscribe(self, session_id, last_event_id=None, **kw):
            seen.update(kw)
            yield SSEEvent(event_type="attempt.created", data={"attempt_id": attempt.attempt_id})

    class _Svc:
        def __init__(self) -> None:
            self.store = store
            self.event_bus = _Bus()

        def get_session(self, sid):
            return store.get_session(sid)

    monkeypatch.delenv("VIBE_MULTITENANT", raising=False)
    monkeypatch.setattr(api_server, "_get_session_service", lambda: _Svc())
    client = TestClient(api_server.app, client=("127.0.0.1", 50000))
    with client.stream("GET", f"/sessions/{session.session_id}/events?replay=active") as resp:
        assert resp.status_code == 200
        for line in resp.iter_lines():
            if line.startswith("event:"):
                break

    assert seen == {"replay_all": True, "since_attempt": attempt.attempt_id}
