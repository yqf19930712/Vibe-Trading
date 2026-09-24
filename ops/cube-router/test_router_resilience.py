"""Resilience tests for cube-router: transient transport faults, engine
restarts and event attribution.

Run: VIBE_ROUTER_SECRET=x VIBE_ROUTER_TOKEN=y VIBE_CUBE_TEMPLATE_ID=tpl-test \
     python -m pytest test_router_resilience.py

No CubeAPI, no sandbox, no engine: every upstream call is an async fake.

  · The answer poll tolerates failing polls within a bounded window and
    fails fast when the launcher says the engine process is gone.
  · The event pump reopens a dropped stream with Last-Event-ID and never
    forwards an event id twice.
  · Only the current attempt's events reach the caller: a continued
    session's replayed llm_usage / attempt_stats are dropped and counted.
"""
from __future__ import annotations

import asyncio
import os
import time

os.environ.setdefault("VIBE_ROUTER_SECRET", "test-secret")
os.environ.setdefault("VIBE_ROUTER_TOKEN", "test-token")
os.environ.setdefault("VIBE_CUBE_TEMPLATE_ID", "tpl-test")

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

import router  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


class _Resp:
    def __init__(self, status_code: int, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


ANSWER = [{"role": "assistant", "content": "答案", "linked_attempt_id": "a1",
           "metadata": {"ok": True}}]


def _inst(tk: str = "tk") -> router.Instance:
    return router.Instance(tk, "sbx", None, "key")


# ── answer poll: bounded tolerance for failing polls ─────────────────────────


class TestWaitAnswerTolerance:
    @pytest.fixture(autouse=True)
    def _fast(self, monkeypatch):
        monkeypatch.setattr(router, "POLL_INTERVAL_S", 0.0)

    def _script(self, monkeypatch, outcomes, health=None):
        """Replay ``outcomes`` (exception or _Resp) one per poll."""
        seen = {"polls": 0, "health": 0}

        async def vibe(inst, method, path, **kw):
            assert path.endswith("/messages")
            item = outcomes[min(seen["polls"], len(outcomes) - 1)]
            seen["polls"] += 1
            if isinstance(item, Exception):
                raise item
            return item

        async def launcher_health(inst):
            seen["health"] += 1
            return health

        monkeypatch.setattr(router, "_vibe", vibe)
        monkeypatch.setattr(router, "_launcher_health", launcher_health)
        return seen

    def test_one_read_timeout_does_not_kill_the_ask(self, monkeypatch):
        seen = self._script(monkeypatch, [httpx.ReadTimeout("proxy hiccup"), _Resp(200, ANSWER)],
                            health={"engine": "running"})
        stats: dict = {}
        out = _run(router._wait_answer(_inst(), "sid", "a1", 30, stats=stats))
        assert out == "答案"
        assert stats["poll_errors"] == 1
        assert seen["health"] == 1

    def test_proxy_errors_and_garbage_bodies_are_tolerated_too(self, monkeypatch):
        self._script(monkeypatch, [
            _Resp(502, None, "bad gateway"),
            httpx.RemoteProtocolError("peer closed"),
            _Resp(200, ValueError("not json")),
            _Resp(200, ANSWER),
        ], health=None)
        stats: dict = {}
        assert _run(router._wait_answer(_inst(), "sid", "a1", 30, stats=stats)) == "答案"
        assert stats["poll_errors"] == 3

    def test_streak_resets_after_a_good_poll(self, monkeypatch):
        monkeypatch.setattr(router, "POLL_FAIL_MAX_CONSECUTIVE", 2)
        self._script(monkeypatch, [
            httpx.ConnectError("x"), _Resp(200, []), httpx.ConnectError("y"),
            _Resp(200, []), _Resp(200, ANSWER),
        ], health=None)
        assert _run(router._wait_answer(_inst(), "sid", "a1", 30)) == "答案"

    def test_consecutive_failures_end_the_wait(self, monkeypatch):
        monkeypatch.setattr(router, "POLL_FAIL_MAX_CONSECUTIVE", 3)
        seen = self._script(monkeypatch, [httpx.ConnectError("down")], health=None)
        with pytest.raises(HTTPException) as ei:
            _run(router._wait_answer(_inst(), "sid", "a1", 30))
        assert ei.value.status_code == 502
        assert "unreachable" in ei.value.detail
        assert seen["polls"] == 3

    def test_failure_window_in_seconds_ends_the_wait(self, monkeypatch):
        monkeypatch.setattr(router, "POLL_FAIL_MAX_CONSECUTIVE", 10_000)
        monkeypatch.setattr(router, "POLL_FAIL_MAX_S", 0.05)

        async def slow_fail(inst, method, path, **kw):
            await asyncio.sleep(0.02)
            raise httpx.ReadTimeout("slow")

        async def health(inst):
            return {"engine": "starting"}

        monkeypatch.setattr(router, "_vibe", slow_fail)
        monkeypatch.setattr(router, "_launcher_health", health)
        t0 = time.monotonic()
        with pytest.raises(HTTPException) as ei:
            _run(router._wait_answer(_inst(), "sid", "a1", 30))
        assert ei.value.status_code == 502
        assert time.monotonic() - t0 < 2.0

    def test_stopped_engine_fails_at_the_first_bad_poll(self, monkeypatch):
        seen = self._script(monkeypatch, [httpx.ConnectError("refused")],
                            health={"launcher": "ok", "engine": "stopped"})
        with pytest.raises(HTTPException) as ei:
            _run(router._wait_answer(_inst(), "sid", "a1", 30))
        assert ei.value.status_code == 502
        assert "stopped" in ei.value.detail
        assert seen["polls"] == 1

    def test_rejected_key_means_the_attempt_is_gone(self, monkeypatch):
        seen = self._script(monkeypatch, [_Resp(401, {"detail": "Invalid or missing API key"})],
                            health={"engine": "running"})
        with pytest.raises(HTTPException) as ei:
            _run(router._wait_answer(_inst(), "sid", "a1", 30))
        assert ei.value.status_code == 502
        assert seen["polls"] == 1

    def test_timeout_is_still_a_504(self, monkeypatch):
        self._script(monkeypatch, [_Resp(200, [])])
        with pytest.raises(HTTPException) as ei:
            _run(router._wait_answer(_inst(), "sid", "a1", 0.05))
        assert ei.value.status_code == 504


# ── event pump: reconnect with Last-Event-ID, no duplicates ─────────────────


def _sse(ev_id, name, data) -> list[str]:
    import json as _json

    lines = []
    if ev_id:
        lines.append(f"id: {ev_id}")
    lines += [f"event: {name}", f"data: {_json.dumps(data)}", ""]
    return lines


class _FakeStream:
    """``http.stream(...)`` stand-in: each call plays the next script."""

    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.calls: list[dict] = []

    def stream(self, method, url, **kw):
        self.calls.append({"method": method, "url": url, **kw})
        script = self.scripts.pop(0) if self.scripts else {"hang": True}
        return _FakeCtx(script)


class _FakeCtx:
    def __init__(self, script):
        self.script = script
        self.status_code = script.get("status", 200)

    async def __aenter__(self):
        if isinstance(self.script.get("connect_error"), Exception):
            raise self.script["connect_error"]
        return self

    async def __aexit__(self, *exc):
        return False

    async def aiter_lines(self):
        for line in self.script.get("lines", []):
            yield line
        if self.script.get("hang"):
            await asyncio.sleep(3600)
        if isinstance(self.script.get("then_raise"), Exception):
            raise self.script["then_raise"]


class TestPumpReconnect:
    @pytest.fixture(autouse=True)
    def _fast(self, monkeypatch):
        monkeypatch.setattr(router, "PUMP_RECONNECT_MIN_DELAY_S", 0.01)
        monkeypatch.setattr(router, "PUMP_RECONNECT_MAX_DELAY_S", 0.02)

    def _pump(self, monkeypatch, scripts, want: int):
        fake = _FakeStream(scripts)
        monkeypatch.setattr(router, "http", fake)
        q: asyncio.Queue = asyncio.Queue()
        stats: dict = {}

        async def go():
            task = asyncio.create_task(router._pump_events(_inst(), "sid", q, stats=stats))
            got = []
            try:
                while len(got) < want:
                    got.append(await asyncio.wait_for(q.get(), timeout=2.0))
                await asyncio.sleep(0.05)  # anything extra would show up here
                while not q.empty():
                    got.append(q.get_nowait())
            finally:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            return got

        return _run(go()), fake, stats

    def test_dropped_stream_resumes_from_the_last_event_id(self, monkeypatch):
        first = (_sse("e1", "attempt.started", {"attempt_id": "a1"})
                 + _sse("e2", "llm_usage", {"attempt_id": "a1", "input_tokens": 5}))
        # The engine replays e2 again after the reconnect (overlap): not forwarded twice.
        second = (_sse("e2", "llm_usage", {"attempt_id": "a1", "input_tokens": 5})
                  + _sse("e3", "attempt_stats", {"attempt_id": "a1"}))
        got, fake, stats = self._pump(monkeypatch, [
            {"lines": first, "then_raise": httpx.RemoteProtocolError("peer closed")},
            {"lines": second, "hang": True},
        ], want=3)

        assert [g["ev"] for g in got] == ["attempt.started", "llm_usage", "attempt_stats"]
        assert "Last-Event-ID" not in fake.calls[0]["headers"]
        assert fake.calls[1]["headers"]["Last-Event-ID"] == "e2"
        assert fake.calls[1]["params"] == {"replay": "active"}
        assert fake.calls[1]["headers"]["Authorization"] == "Bearer key"
        assert stats["pump_reconnects"] == 1

    def test_failed_connects_back_off_and_retry(self, monkeypatch):
        got, fake, stats = self._pump(monkeypatch, [
            {"connect_error": httpx.ConnectError("proxy down")},
            {"status": 502},
            {"lines": _sse("e1", "text_delta", {"attempt_id": "a1"}), "hang": True},
        ], want=1)
        assert [g["ev"] for g in got] == ["text_delta"]
        assert len(fake.calls) == 3
        assert stats["pump_reconnects"] == 2

    def test_heartbeats_without_ids_do_not_move_the_resume_point(self, monkeypatch):
        got, fake, _ = self._pump(monkeypatch, [
            {"lines": _sse(None, "heartbeat", {"ts": 1}), "then_raise": httpx.ReadTimeout("x")},
            {"lines": _sse("e9", "text_delta", {"attempt_id": "a1"}), "hang": True},
        ], want=2)
        assert [g["ev"] for g in got] == ["heartbeat", "text_delta"]
        assert "Last-Event-ID" not in fake.calls[1]["headers"]


# ── /ask stream harness (every upstream call stubbed) ────────────────────────


class _AskHarness:
    """Stubs the upstream calls of ``_ask_stream``; tests tweak the hooks."""

    def __init__(self, monkeypatch):
        self.inst = _inst(router.tenant_key("u-ask"))
        self.events: list[dict] = []
        self.answer_delay = 0.05
        self.answer: object = "答案"
        self.recorded: list[dict] = []
        self.cancelled: list[str] = []
        self.waiter_kw: dict = {}
        h = self

        async def get_or_create(tk, model, llm, meta=None):
            return h.inst

        async def ensure_session(inst, sid):
            return sid or "sid-1"

        async def post_turn(inst, sid, query, **kw):
            h.turn_kw = kw
            return "a1"

        async def pump(inst, sid, q, **kw):
            for ev in h.events:
                q.put_nowait(ev)
            await asyncio.sleep(3600)

        async def wait_answer(inst, sid, attempt_id, timeout_s, failed=None, **kw):
            h.waiter_kw = {"timeout_s": timeout_s, **kw}
            await asyncio.sleep(h.answer_delay)
            if isinstance(h.answer, BaseException):
                raise h.answer
            return h.answer

        async def cancel_bg(inst, sid, tk, stats, finalize=None):
            h.cancelled.append(sid)
            if finalize is not None:
                finalize()
            router._record_ask(stats)

        monkeypatch.setattr(router, "get_or_create", get_or_create)
        monkeypatch.setattr(router, "_ensure_session", ensure_session)
        monkeypatch.setattr(router, "_post_turn", post_turn)
        monkeypatch.setattr(router, "_pump_events", pump)
        monkeypatch.setattr(router, "_wait_answer", wait_answer)
        monkeypatch.setattr(router, "_cancel_attempt_bg", cancel_bg)
        monkeypatch.setattr(router, "_record_ask", self.recorded.append)

    def run(self, body=None, timeout_s: int = 30) -> list[dict]:
        import json as _json

        body = body or router.AskBody(uid="u-ask", query="q", vibeSessionId="sid-1")

        async def collect():
            return [_json.loads(line) async for line in router._ask_stream(body, timeout_s)]

        return _run(collect())


# ── forwarded events are attributed to the current attempt ──────────────────


class TestEventAttribution:
    def test_pure_rule(self):
        own = {"ev": "llm_usage", "data": {"attempt_id": "a1", "input_tokens": 1}}
        stale = {"ev": "llm_usage", "data": {"attempt_id": "a0", "input_tokens": 1}}
        orphan_usage = {"ev": "llm_usage", "data": {"input_tokens": 1}}
        session_level = {"ev": "message.received", "data": {"role": "user", "content": "q"}}
        heartbeat = {"ev": "heartbeat", "data": {"ts": 1}}
        stale_delta = {"ev": "text_delta", "data": {"attempt_id": "a0", "delta": "x"}}
        assert router._event_belongs_to_ask(own, "a1")
        assert not router._event_belongs_to_ask(stale, "a1")
        assert not router._event_belongs_to_ask(orphan_usage, "a1")
        assert router._event_belongs_to_ask(session_level, "a1")
        assert router._event_belongs_to_ask(heartbeat, "a1")
        assert not router._event_belongs_to_ask(stale_delta, "a1")
        # No attempt id from the engine: nothing to filter on.
        assert router._event_belongs_to_ask(stale, None)

    def test_replayed_previous_attempt_is_neither_forwarded_nor_counted(self, monkeypatch):
        h = _AskHarness(monkeypatch)
        prev_stats = {"attempt_id": "a0", "status": "success", "iterations": 9}
        cur_stats = {"attempt_id": "a1", "status": "success", "iterations": 2}
        h.events = [
            # What a continued session's buffer replays first …
            {"ev": "message.received", "data": {"role": "user", "content": "上一问"}},
            {"ev": "attempt.created", "data": {"attempt_id": "a0"}},
            {"ev": "llm_usage", "data": {"attempt_id": "a0", "input_tokens": 400000,
                                         "output_tokens": 90000, "source": "swarm"}},
            {"ev": "attempt_stats", "data": prev_stats},
            {"ev": "attempt.completed", "data": {"attempt_id": "a0", "status": "completed"}},
            {"ev": "llm_usage", "data": {"input_tokens": 7}},
            # … then this attempt's own events.
            {"ev": "attempt.started", "data": {"attempt_id": "a1"}},
            {"ev": "heartbeat", "data": {"ts": 1}},
            {"ev": "llm_usage", "data": {"attempt_id": "a1", "input_tokens": 9000,
                                         "output_tokens": 3000}},
            {"ev": "attempt_stats", "data": cur_stats},
        ]
        frames = h.run()

        progress = [f for f in frames if f["t"] == "progress"]
        usage = [f["data"] for f in progress if f["ev"] == "llm_usage"]
        assert usage == [{"attempt_id": "a1", "input_tokens": 9000, "output_tokens": 3000}]
        assert [f["data"] for f in progress if f["ev"] == "attempt_stats"] == [cur_stats]
        assert not any(isinstance(f.get("data"), dict) and f["data"].get("attempt_id") == "a0"
                       for f in progress)
        assert any(f["ev"] == "message.received" for f in progress)
        answer = frames[-1]
        assert answer["t"] == "answer"
        assert answer["stats"]["engine"] == cur_stats
        assert answer["stats"]["router"]["stale_events_dropped"] == 5
        assert h.recorded and h.recorded[0]["stale_events_dropped"] == 5

    def test_stale_attempt_failed_does_not_end_this_ask(self, monkeypatch):
        h = _AskHarness(monkeypatch)
        h.events = [{"ev": "attempt.failed", "data": {"attempt_id": "a0", "error": "old"}}]
        frames = h.run()
        assert frames[-1]["t"] == "answer"
