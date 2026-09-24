"""Event ids are ordered, so a reconnect resumes exactly where it left off.

The router bills every ``llm_usage`` it forwards. With opaque ids, a
Last-Event-ID that had already left the 500-event buffer made the engine
replay the attempt window from its start, and the router's bounded set of
seen ids no longer covered the early usage events: they were billed twice.
Ids are now ``<epoch>-<seq>``; a reconnect resumes after the id even when it
is gone, and a consumer can deduplicate with a high-water mark.
"""

from __future__ import annotations

import asyncio
import re
import threading

from src.session.events import EventBus


def _seq(event_id: str) -> int:
    return int(event_id.rsplit("-", 1)[1])


def test_ids_are_epoch_and_increasing_sequence() -> None:
    bus = EventBus()
    a = bus.emit("s1", "attempt.created", {"attempt_id": "A"})
    b = bus.emit("s2", "attempt.created", {"attempt_id": "B"})
    c = bus.emit("s1", "llm_usage", {"attempt_id": "A"})

    for ev in (a, b, c):
        assert re.fullmatch(r"[0-9a-f]{8}-\d+", ev.event_id)
    assert a.event_id.split("-")[0] == c.event_id.split("-")[0]
    assert _seq(a.event_id) < _seq(b.event_id) < _seq(c.event_id)
    # A new process (bus) is a new epoch: its ids never collide with these.
    assert EventBus().emit("s1", "x").event_id.split("-")[0] != a.event_id.split("-")[0]


def test_reconnect_after_an_evicted_id_bills_each_usage_once() -> None:
    """The review's reproduction: 20 iterations of usage + tool heartbeats +
    streamed text, a router-like consumer, then one reconnect."""
    bus = EventBus()  # engine default: 500 buffered events
    sid, att = "s", "cur"
    delivered: list = []
    hwm = 0

    def take(events) -> None:
        nonlocal hwm
        for ev in events:
            if _seq(ev.event_id) <= hwm:
                continue
            hwm = _seq(ev.event_id)
            delivered.append(ev)

    live = [bus.emit(sid, "attempt.created", {"attempt_id": att})]
    for _ in range(20):
        live.append(bus.emit(sid, "llm_usage", {"attempt_id": att, "input_tokens": 1000}))
        live.append(bus.emit(sid, "tool_call", {"attempt_id": att}))
        live += [bus.emit(sid, "tool_heartbeat", {"attempt_id": att}) for _ in range(25)]
        live.append(bus.emit(sid, "tool_result", {"attempt_id": att}))
        live += [bus.emit(sid, "text_delta", {"attempt_id": att}) for _ in range(300)]
    take(live)
    last_id = delivered[-1].event_id
    missed = [bus.emit(sid, "text_delta", {"attempt_id": att}) for _ in range(40)]
    missed.append(bus.emit(sid, "llm_usage", {"attempt_id": att, "input_tokens": 7}))

    assert all(ev.event_id != last_id for ev in bus._buffers[sid])  # evicted
    replayed = bus.replay(sid, last_id, replay_all=True, since_attempt=att)
    take(replayed)

    assert all(_seq(ev.event_id) > _seq(last_id) for ev in replayed)
    usage = [ev.data["input_tokens"] for ev in delivered if ev.event_type == "llm_usage"]
    assert sum(usage) == 20 * 1000 + 7
    assert missed[-1] in replayed


def test_an_id_from_another_engine_process_falls_back_to_the_window() -> None:
    bus = EventBus()
    created = bus.emit("s", "attempt.created", {"attempt_id": "A"})
    usage = bus.emit("s", "llm_usage", {"attempt_id": "A"})

    assert bus.replay("s", "ffffffff-3", replay_all=True, since_attempt="A") == [created, usage]
    assert bus.replay("s", created.event_id, replay_all=True, since_attempt="A") == [usage]


def test_concurrent_publishers_reach_a_subscriber_in_id_order() -> None:
    bus = EventBus()

    async def main() -> list[int]:
        bus.set_loop(asyncio.get_running_loop())
        agen = bus.subscribe("s")
        first = asyncio.ensure_future(agen.__anext__())
        await asyncio.sleep(0)

        def publisher(n: int) -> None:
            for i in range(200):
                bus.emit("s", "llm_usage", {"t": n, "i": i})

        threads = [threading.Thread(target=publisher, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        got = [await asyncio.wait_for(first, timeout=2.0)]
        while len(got) < 800:
            got.append(await asyncio.wait_for(agen.__anext__(), timeout=2.0))
        for t in threads:
            t.join()
        await agen.aclose()
        return [_seq(ev.event_id) for ev in got]

    seqs = asyncio.run(main())
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == 800


def test_subscribe_never_delivers_an_event_twice() -> None:
    """Replay snapshot and live queue are taken under one lock."""
    bus = EventBus()
    for i in range(5):
        bus.emit("s", "tool_call", {"attempt_id": "A", "i": i})

    async def main() -> list[str]:
        agen = bus.subscribe("s", replay_all=True)
        got = [(await agen.__anext__()).event_id for _ in range(5)]
        bus.emit("s", "tool_call", {"attempt_id": "A", "i": 5})
        got.append((await asyncio.wait_for(agen.__anext__(), timeout=1.0)).event_id)
        await agen.aclose()
        return got

    ids = asyncio.run(main())
    assert len(ids) == len(set(ids)) == 6
