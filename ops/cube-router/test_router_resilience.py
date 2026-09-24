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
  · The engine fingerprint covers every forwarded router.env name and value
    (hashed), so an env edit + router restart reboots existing engines.
  · An interrupted or timed-out /boot leaves the router holding the NEW key
    under a boot-pending fingerprint (adopted if the engine came up with it,
    rebooted otherwise); an engine 401 marks the instance stale, and /ask
    reboots once and retries.
  · Waiting for a processing slot is bounded (VIBE_ACTIVE_QUEUE_WAIT_S);
    every 503 busy frame carries code=busy + busy_reason.
  · A pause CubeAPI refuses leaves the sandbox counted as RUNNING; eviction
    moves on to the next victim and the reaper retries next sweep.
  · The answer window is anchored at the ask's arrival (queue / cold start /
    lock wait come out of it) and attempt_meta reports the remaining window.
  · /forget writes a tombstone first and takes the tenant lock (bounded):
    a racing cold start aborts and removes its sandbox and data dir, and
    later asks for the tenant get 410 until the tombstone expires.
  · uvicorn access-log lines carry tk8 instead of the raw userId.
  · /boot carries the per-sandbox launcher token as a Bearer header, and in
    the env only with VIBE_LAUNCHER_AUTH=1 (a fingerprint-changing switch).
  · Error frames a caller can act on carry a machine-readable ``code``.
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

    def test_swarm_tail_usage_of_an_earlier_attempt_is_billed_here(self):
        tail = {"ev": "llm_usage", "data": {"attempt_id": "a0", "input_tokens": 5,
                                            "source": "swarm_tail", "run_id": "r1"}}
        swarm = {"ev": "llm_usage", "data": {"attempt_id": "a0", "input_tokens": 5, "source": "swarm"}}
        tail_stats = {"ev": "attempt_stats", "data": {"attempt_id": "a0", "source": "swarm_tail"}}
        assert router._event_belongs_to_ask(tail, "a1")
        assert not router._event_belongs_to_ask(swarm, "a1")
        assert not router._event_belongs_to_ask(tail_stats, "a1")

    def test_swarm_tail_reaches_the_caller(self, monkeypatch):
        h = _AskHarness(monkeypatch)
        tail = {"attempt_id": "a0", "input_tokens": 70000, "output_tokens": 9000,
                "source": "swarm_tail", "run_id": "r1"}
        h.events = [
            {"ev": "attempt.started", "data": {"attempt_id": "a1"}},
            {"ev": "llm_usage", "data": tail},
            {"ev": "llm_usage", "data": {"attempt_id": "a1", "input_tokens": 10, "output_tokens": 2}},
        ]
        frames = h.run()
        usage = [f["data"] for f in frames if f["t"] == "progress" and f["ev"] == "llm_usage"]
        assert tail in usage and len(usage) == 2
        assert frames[-1]["stats"]["router"].get("stale_events_dropped", 0) == 0

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


# ── router.env is part of the engine fingerprint ─────────────────────────────


class TestEnvFingerprint:
    def test_router_env_change_changes_the_fingerprint(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "old-credential-value")
        env1, key1 = router.engine_env(None, None)
        env1b, key1b = router.engine_env(None, None)
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "rotated-credential-value")
        env2, _ = router.engine_env(None, None)

        fp1 = router.llm_fingerprint(None, None, env1)
        assert key1 != key1b                                 # fresh key per boot …
        assert fp1 == router.llm_fingerprint(None, None, env1b)  # … not part of identity
        assert fp1 != router.llm_fingerprint(None, None, env2)
        assert fp1.startswith("default|env:")
        assert "credential" not in fp1

    def test_default_model_and_tier_knobs_count_too(self, monkeypatch):
        base = router.llm_fingerprint(None, None, router.engine_env(None, None)[0])
        monkeypatch.setenv("LANGCHAIN_MODEL_NAME", "some-other-model")
        assert router.llm_fingerprint(None, None, router.engine_env(None, None)[0]) != base
        monkeypatch.delenv("LANGCHAIN_MODEL_NAME")
        monkeypatch.setenv("VIBE_MAX_ITERATIONS", "30")
        assert router.llm_fingerprint(None, None, router.engine_env(None, None)[0]) != base

    def test_request_choice_is_still_named(self):
        llm = router.LlmOverride(provider="deepseek", model="deepseek-chat",
                                 apiKey="k" * 12, baseUrl="https://api.deepseek.com")
        env = {"X": "1"}
        assert router.llm_fingerprint("m1", None, env).startswith("builtin:m1|env:")
        assert router.llm_fingerprint(None, llm, env).startswith("byok:")
        # Without env the old shape is kept (callers that only name the choice).
        assert router.llm_fingerprint(None, None) is None
        assert router.llm_fingerprint("m1", None) == "builtin:m1"

    def test_instance_from_an_older_fingerprint_is_rebooted(self, monkeypatch):
        """A state row written before the env digest existed never matches."""
        booted: list[str] = []

        async def health(inst):
            return {"launcher": "ok", "engine": "running"}

        async def boot(inst, fp, env, api_key):
            booted.append(fp)
            inst.llm_fp, inst.api_key = fp, api_key

        monkeypatch.setattr(router, "_launcher_health", health)
        monkeypatch.setattr(router, "_boot_engine", boot)
        env, key = router.engine_env(None, None)
        fp = router.llm_fingerprint(None, None, env)

        for legacy in (None, "builtin:claude-opus-5"):
            inst = router.Instance("tk", "sbx", legacy, "old-key")
            _run(router._ensure_ready(inst, fp, env, key))
        assert booted == [fp, fp]

        booted.clear()
        inst = router.Instance("tk", "sbx", fp, "old-key")
        _run(router._ensure_ready(inst, fp, env, key))
        assert booted == []


# ── interrupted /boot never leaves the router with a dead key ────────────────


@pytest.fixture()
def clean_state(monkeypatch, tmp_path):
    router.pool.clear()
    router.state.clear()
    router.uid_locks.clear()
    monkeypatch.setattr(router, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(router, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(router, "pool_mutex", asyncio.Lock())
    monkeypatch.setattr(router, "capacity_lock", asyncio.Lock())
    yield tmp_path
    router.pool.clear()
    router.state.clear()
    router.uid_locks.clear()


class _FakeHttp:
    """Launcher /boot (post) + engine calls (request) through one object."""

    def __init__(self, boot, engine=None):
        self.boot = boot
        self.engine = engine or (lambda method, url, headers: _Resp(404, {}))
        self.boots: list[dict] = []
        self.engine_calls: list[tuple[str, str, dict]] = []

    async def post(self, url, json=None, **kw):
        self.boots.append({"url": url, "env": dict((json or {}).get("env") or {}), **kw})
        return await self.boot()

    async def request(self, method, url, headers=None, **kw):
        self.engine_calls.append((method, url, dict(headers or {})))
        return self.engine(method, url, headers or {})


class TestBootInterruption:
    def _setup(self, monkeypatch, boot, engine=None, health="stopped"):
        fake = _FakeHttp(boot, engine)
        monkeypatch.setattr(router, "http", fake)
        state = {"engine": health}

        async def launcher_health(inst):
            return {"launcher": "ok", "engine": state["engine"]}

        monkeypatch.setattr(router, "_launcher_health", launcher_health)
        return fake, state

    def _pooled(self, tk="t" * 64):
        inst = router.Instance(tk, "sbx-1", "old-fp", "OLD-KEY")
        router.pool[tk] = inst
        return inst

    def test_transport_error_mid_boot_keeps_the_new_key(self, monkeypatch, clean_state):
        async def boot():
            raise httpx.ReadError("connection reset during boot")

        self._setup(monkeypatch, boot)
        inst = self._pooled()
        env, key = router.engine_env(None, None)
        fp = router.llm_fingerprint(None, None, env)
        with pytest.raises(httpx.ReadError):
            _run(router._ensure_ready(inst, fp, env, key))
        assert inst.api_key == key
        assert inst.llm_fp == router._pending_fp(fp)
        row = router.state[inst.tk]
        assert row["api_key"] == key and row["llm_fp"] == router._pending_fp(fp)

    def test_launcher_boot_timeout_500_keeps_the_new_key(self, monkeypatch, clean_state):
        async def boot():
            return _Resp(500, {"ok": False}, "engine not healthy after 120s")

        self._setup(monkeypatch, boot)
        inst = self._pooled()
        env, key = router.engine_env(None, None)
        fp = router.llm_fingerprint(None, None, env)
        with pytest.raises(HTTPException) as ei:
            _run(router._ensure_ready(inst, fp, env, key))
        assert ei.value.status_code == 502
        assert inst.api_key == key and inst.llm_fp == router._pending_fp(fp)

    def test_cancelled_boot_keeps_the_new_key(self, monkeypatch, clean_state):
        async def boot():
            await asyncio.sleep(3600)

        self._setup(monkeypatch, boot)
        inst = self._pooled()
        env, key = router.engine_env(None, None)
        fp = router.llm_fingerprint(None, None, env)

        async def go():
            t = asyncio.create_task(router._ensure_ready(inst, fp, env, key))
            await asyncio.sleep(0.02)
            t.cancel()
            with pytest.raises(asyncio.CancelledError):
                await t

        _run(go())
        assert inst.api_key == key and inst.llm_fp == router._pending_fp(fp)

    def test_next_ask_adopts_an_engine_that_came_up_with_that_key(self, monkeypatch, clean_state):
        async def boot():
            raise AssertionError("must not reboot")

        def engine(method, url, headers):
            assert url.endswith("/sessions/keyprobe")
            return _Resp(404 if headers.get("Authorization") == "Bearer NEW" else 401, {})

        fake, _ = self._setup(monkeypatch, boot, engine, health="running")
        env, _ = router.engine_env(None, None)
        fp = router.llm_fingerprint(None, None, env)
        inst = self._pooled()
        inst.api_key, inst.llm_fp = "NEW", router._pending_fp(fp)
        meta: dict = {}
        _run(router._ensure_ready(inst, fp, env, "UNUSED", meta=meta))
        assert inst.llm_fp == fp and inst.api_key == "NEW"
        assert meta.get("boot_adopted") is True
        assert router.state[inst.tk]["llm_fp"] == fp

    def test_next_ask_reboots_when_the_engine_rejects_the_pending_key(self, monkeypatch, clean_state):
        async def boot():
            return _Resp(200, {"ok": True})

        fake, _ = self._setup(monkeypatch, boot, lambda m, u, h: _Resp(401, {}), health="running")
        env, key = router.engine_env(None, None)
        fp = router.llm_fingerprint(None, None, env)
        inst = self._pooled()
        inst.api_key, inst.llm_fp = "NEW", router._pending_fp(fp)
        _run(router._ensure_ready(inst, fp, env, key))
        assert len(fake.boots) == 1
        assert inst.llm_fp == fp and inst.api_key == key

    def test_pending_boot_of_another_configuration_is_rebooted(self, monkeypatch, clean_state):
        async def boot():
            return _Resp(200, {"ok": True})

        fake, _ = self._setup(monkeypatch, boot, health="running")
        env, key = router.engine_env(None, None)
        fp = router.llm_fingerprint(None, None, env)
        inst = self._pooled()
        inst.llm_fp = router._pending_fp("byok:someotherconfig")
        _run(router._ensure_ready(inst, fp, env, key))
        assert len(fake.boots) == 1 and inst.llm_fp == fp

    def test_401_from_the_engine_marks_the_instance_for_reboot(self, monkeypatch, clean_state):
        fake, _ = self._setup(monkeypatch, None, lambda m, u, h: _Resp(401, {}), health="running")
        inst = self._pooled()
        inst.llm_fp = "good-fp"
        r = _run(router._vibe(inst, "GET", "/sessions/x/messages"))
        assert r.status_code == 401
        assert inst.llm_fp == router._FP_STALE
        assert router.state[inst.tk]["llm_fp"] == router._FP_STALE

    def test_failed_fresh_cold_start_drops_the_prewritten_state_row(self, monkeypatch, clean_state):
        async def boot():
            return _Resp(500, {"ok": False}, "engine process exited during boot")

        self._setup(monkeypatch, boot)
        deleted: list[str] = []

        async def sbx_create(tk):
            return "sbx-new"

        async def sbx_delete(sandbox_id):
            deleted.append(sandbox_id)
            return True

        monkeypatch.setattr(router, "sbx_create", sbx_create)
        monkeypatch.setattr(router, "sbx_delete", sbx_delete)
        tk = router.tenant_key("fresh-user")
        with pytest.raises(HTTPException):
            _run(router.get_or_create(tk))
        assert deleted == ["sbx-new"]
        assert tk not in router.pool and tk not in router.state


class TestAskRebootsOnRejectedKey:
    def test_post_turn_401_reboots_once_and_retries(self, monkeypatch):
        h = _AskHarness(monkeypatch)
        posts = {"n": 0}
        reboots: list[str] = []

        async def post_turn(inst, sid, query, **kw):
            posts["n"] += 1
            if posts["n"] == 1:
                raise router._EngineUnauthorized()
            return "a1"

        async def reboot(inst, model, llm):
            reboots.append(inst.tk)

        monkeypatch.setattr(router, "_post_turn", post_turn)
        monkeypatch.setattr(router, "_reboot_engine", reboot)
        frames = h.run()
        assert frames[-1]["t"] == "answer"
        assert reboots == [h.inst.tk] and posts["n"] == 2
        assert frames[-1]["stats"]["router"]["auth_reboot"] is True

    def test_a_second_rejection_is_an_error_frame(self, monkeypatch):
        h = _AskHarness(monkeypatch)

        async def post_turn(inst, sid, query, **kw):
            raise router._EngineUnauthorized()

        async def reboot(inst, model, llm):
            return None

        monkeypatch.setattr(router, "_post_turn", post_turn)
        monkeypatch.setattr(router, "_reboot_engine", reboot)
        frames = h.run()
        assert frames[-1]["t"] == "error" and frames[-1]["status"] == 502


# ── the queue for a processing slot is bounded ───────────────────────────────


class TestActiveSlotQueue:
    def _saturate(self, monkeypatch, wait_s):
        monkeypatch.setattr(router, "ACTIVE_QUEUE_WAIT_S", wait_s)

    def test_saturated_slots_answer_503_busy_after_the_bound(self, monkeypatch):
        h = _AskHarness(monkeypatch)
        self._saturate(monkeypatch, 0.05)

        async def go():
            sem = asyncio.Semaphore(1)
            monkeypatch.setattr(router, "active_sem", sem)
            await sem.acquire()  # a long deep_team ask holds the only slot
            import json as _json

            body = router.AskBody(uid="u-ask", query="q")
            return [_json.loads(line) async for line in router._ask_stream(body, 900)], sem

        frames, sem = _run(go())
        assert len(frames) == 1
        err = frames[0]
        assert err["t"] == "error" and err["status"] == 503
        assert err["code"] == "busy" and err["busy_reason"] == "active_queue_full"
        assert err["stats"]["router"]["outcome"] == "busy"
        assert err["stats"]["router"]["queue_wait_ms"] >= 40
        assert h.recorded and h.recorded[0]["busy_reason"] == "active_queue_full"
        assert sem._value == 0  # the timed-out waiter took nothing

    def test_slot_freed_within_the_bound_is_taken(self, monkeypatch):
        h = _AskHarness(monkeypatch)
        self._saturate(monkeypatch, 5.0)

        async def go():
            sem = asyncio.Semaphore(1)
            monkeypatch.setattr(router, "active_sem", sem)
            await sem.acquire()

            async def release_soon():
                await asyncio.sleep(0.05)
                sem.release()

            asyncio.create_task(release_soon())
            import json as _json

            body = router.AskBody(uid="u-ask", query="q")
            frames = [_json.loads(line) async for line in router._ask_stream(body, 900)]
            return frames, sem

        frames, sem = _run(go())
        assert frames[-1]["t"] == "answer"
        assert frames[-1]["stats"]["router"]["queue_wait_ms"] >= 40
        assert sem._value == 1  # released after the ask

    def test_capacity_503_carries_the_same_busy_code(self, monkeypatch):
        h = _AskHarness(monkeypatch)

        async def full(tk, model, llm, meta=None):
            raise router._Busy("instances_full", "all instances busy; retry shortly")

        monkeypatch.setattr(router, "get_or_create", full)
        frames = h.run()
        assert frames[-1]["code"] == "busy" and frames[-1]["busy_reason"] == "instances_full"
        assert frames[-1]["status"] == 503


# ── a refused pause is not counted as paused ─────────────────────────────────


class TestPauseRefused:
    def test_sbx_pause_reads_the_status(self, monkeypatch):
        answers = iter([_Resp(204), _Resp(409), _Resp(404), _Resp(500, None, "not in normal state")])

        class _Api:
            async def post(self, path):
                return next(answers)

        monkeypatch.setattr(router, "api", _Api())
        assert [_run(router.sbx_pause("sbx")) for _ in range(4)] == [True, True, True, False]

        class _DeadApi:
            async def post(self, path):
                raise httpx.ConnectError("cube api down")

        monkeypatch.setattr(router, "api", _DeadApi())
        assert _run(router.sbx_pause("sbx")) is False

    def _pool(self, monkeypatch, clean_state, refuse: set[str]):
        monkeypatch.setattr(router, "MAX_RUNNING", 2)
        pauses: list[str] = []

        async def sbx_pause(sandbox_id):
            pauses.append(sandbox_id)
            return sandbox_id not in refuse

        monkeypatch.setattr(router, "sbx_pause", sbx_pause)
        now = time.monotonic()
        out = []
        for name, idle in (("a", 300), ("b", 100)):
            inst = router.Instance(name, f"sbx-{name}", None, "key")
            inst.last_activity = now - idle
            router.pool[name] = inst
            out.append(inst)
        return out, pauses

    def test_eviction_moves_on_to_the_next_victim(self, monkeypatch, clean_state):
        (a, b), pauses = self._pool(monkeypatch, clean_state, refuse={"sbx-a"})
        _run(router._evict_for_capacity())
        assert pauses == ["sbx-a", "sbx-b"]
        assert a.paused is False and b.paused is True

    def test_all_refused_is_busy_not_a_spin(self, monkeypatch, clean_state):
        (a, b), pauses = self._pool(monkeypatch, clean_state, refuse={"sbx-a", "sbx-b"})
        with pytest.raises(HTTPException) as ei:
            _run(router._evict_for_capacity())
        assert ei.value.status_code == 503
        assert pauses == ["sbx-a", "sbx-b"]
        assert not a.paused and not b.paused

    def test_reaper_keeps_counting_a_refused_victim(self, monkeypatch, clean_state):
        monkeypatch.setattr(router, "IDLE_TTL_S", 10)
        (a, b), _ = self._pool(monkeypatch, clean_state, refuse={"sbx-a"})
        paused = _run(router._reap_idle_once())
        assert paused == [b]
        assert a.paused is False and b.paused is True


# ── the answer window starts when the ask arrives ────────────────────────────


class TestAnswerWindow:
    def test_queue_and_boot_time_come_out_of_the_answer_window(self, monkeypatch):
        h = _AskHarness(monkeypatch)

        async def slow_cold_start(tk, model, llm, meta=None):
            await asyncio.sleep(0.3)
            return h.inst

        monkeypatch.setattr(router, "get_or_create", slow_cold_start)
        t_before = time.monotonic()
        frames = h.run(timeout_s=100)
        t_after = time.monotonic()

        deadline = h.waiter_kw["deadline"]
        assert t_before + 100 <= deadline <= t_after + 100
        meta = next(f for f in frames if f.get("ev") == "attempt_meta")["data"]
        assert meta["attempt_id"] == "a1" and meta["vibe_session_id"] == "sid-1"
        assert 99.0 <= meta["answer_deadline_s"] <= 99.8
        assert meta["engine_deadline_s"] == pytest.approx(89.7, abs=0.3)


# ── /forget: tenant lock + tombstone ─────────────────────────────────────────

AUTH = f"Bearer {os.environ['VIBE_ROUTER_TOKEN']}"


class TestForgetTombstone:
    def _cube(self, monkeypatch, boot_delay=0.0):
        calls = {"create": [], "delete": [], "boots": 0}

        async def sbx_create(tk):
            router.tenant_data_dir(tk)
            calls["create"].append(tk)
            return f"sbx-{len(calls['create'])}"

        async def sbx_delete(sandbox_id):
            calls["delete"].append(sandbox_id)
            return True

        async def health(inst):
            return {"launcher": "ok", "engine": "stopped"}

        async def boot():
            calls["boots"] += 1
            await asyncio.sleep(boot_delay)
            return _Resp(200, {"ok": True})

        monkeypatch.setattr(router, "sbx_create", sbx_create)
        monkeypatch.setattr(router, "sbx_delete", sbx_delete)
        monkeypatch.setattr(router, "_launcher_health", health)
        monkeypatch.setattr(router, "http", _FakeHttp(boot))
        return calls

    def test_forgotten_tenant_cannot_be_recreated(self, monkeypatch, clean_state):
        calls = self._cube(monkeypatch)
        uid = "gone-user"
        tk = router.tenant_key(uid)
        _run(router.get_or_create(tk))
        assert (clean_state / tk).is_dir() and tk in router.pool

        assert _run(router.forget({"uid": uid}, authorization=AUTH)) == {"ok": True}
        assert not (clean_state / tk).exists()
        assert set(router.state[tk]) == {"forgotten_at"}

        with pytest.raises(HTTPException) as ei:
            _run(router.get_or_create(tk))
        assert ei.value.status_code == 410
        assert not (clean_state / tk).exists()
        assert calls["create"] == [tk]
        # Survives a router restart (it is in state.json).
        import json as _json

        assert "forgotten_at" in _json.loads((clean_state / "state.json").read_text())[tk]

    def test_ask_for_a_forgotten_tenant_is_a_410_frame(self, monkeypatch, clean_state):
        h = _AskHarness(monkeypatch)
        router.state[router.tenant_key("u-ask")] = {"forgotten_at": time.time()}
        frames = h.run()
        assert len(frames) == 1
        assert frames[0]["t"] == "error" and frames[0]["status"] == 410
        assert frames[0]["code"] == "tenant_forgotten"
        assert frames[0]["stats"]["router"]["outcome"] == "forgotten"

    def test_forget_waits_for_an_in_flight_cold_start(self, monkeypatch, clean_state):
        calls = self._cube(monkeypatch, boot_delay=0.2)
        uid = "racer"
        tk = router.tenant_key(uid)

        async def go():
            cold = asyncio.create_task(router.get_or_create(tk))
            await asyncio.sleep(0.05)  # inside the boot, tenant lock held
            forget_res = await router.forget({"uid": uid}, authorization=AUTH)
            with pytest.raises(HTTPException) as ei:
                await cold
            return forget_res, ei.value

        res, exc = _run(go())
        assert res == {"ok": True}
        assert exc.status_code == 410       # the cold start aborted on the tombstone
        assert calls["delete"] == ["sbx-1"]  # … and removed its own sandbox
        assert tk not in router.pool
        assert set(router.state[tk]) == {"forgotten_at"}
        assert not (clean_state / tk).exists()

    def test_lock_wait_is_bounded_and_the_racer_cleans_up(self, monkeypatch, clean_state):
        monkeypatch.setattr(router, "FORGET_LOCK_WAIT_S", 0.05)
        calls = self._cube(monkeypatch, boot_delay=0.4)
        uid = "slow-racer"
        tk = router.tenant_key(uid)

        async def go():
            cold = asyncio.create_task(router.get_or_create(tk))
            await asyncio.sleep(0.05)
            t0 = time.monotonic()
            forget_res = await router.forget({"uid": uid}, authorization=AUTH)
            waited = time.monotonic() - t0
            with pytest.raises(HTTPException):
                await cold
            return forget_res, waited

        res, waited = _run(go())
        assert res == {"ok": True}
        assert waited < 0.3
        assert "sbx-1" in calls["delete"]
        assert tk not in router.pool
        assert set(router.state[tk]) == {"forgotten_at"}
        assert not (clean_state / tk).exists()

    def test_failed_sandbox_delete_keeps_mapping_and_tombstone(self, monkeypatch, clean_state):
        tk = router.tenant_key("stuck")
        router.state[tk] = {"sandbox_id": "sbx-stuck", "template_id": router.TEMPLATE_ID}

        async def refuse(sandbox_id):
            return False

        monkeypatch.setattr(router, "sbx_delete", refuse)
        resp = _run(router.forget({"uid": "stuck"}, authorization=AUTH))
        assert resp.status_code == 500
        assert router.state[tk]["sandbox_id"] == "sbx-stuck"
        assert "forgotten_at" in router.state[tk]

    def test_expired_tombstones_are_pruned_and_allow_asks_again(self, monkeypatch, clean_state):
        monkeypatch.setattr(router, "FORGET_TOMBSTONE_S", 100)
        router.state["old"] = {"forgotten_at": time.time() - 1000}
        router.state["fresh"] = {"forgotten_at": time.time()}
        router.state["old-with-sandbox"] = {"forgotten_at": time.time() - 1000, "sandbox_id": "sbx"}
        assert not router._tombstoned("old") and router._tombstoned("fresh")
        assert router._prune_tombstones() == 1
        assert "old" not in router.state
        assert "fresh" in router.state and "old-with-sandbox" in router.state

    def test_drop_state_row_keeps_the_tombstone(self, clean_state):
        router.state["t"] = {"sandbox_id": "s", "api_key": "k", "forgotten_at": 1.0}
        router._drop_state_row("t", "s")
        assert router.state["t"] == {"forgotten_at": 1.0}
        router.state["u"] = {"sandbox_id": "s"}
        router._drop_state_row("u", "other")
        assert "u" in router.state
        router._drop_state_row("u")
        assert "u" not in router.state


# ── no raw userId / memory title in router logs ──────────────────────────────


class TestLogPrivacy:
    def test_access_log_uid_becomes_tk8(self):
        import logging as _logging

        f = router._AccessLogUidRedactor()
        uid = "user_2xYz+abc@example"
        from urllib.parse import quote_plus

        path = f"/memory?uid={quote_plus(uid)}&limit=5"
        rec = _logging.LogRecord("uvicorn.access", _logging.INFO, __file__, 1,
                                 '%s - "%s %s HTTP/%s" %d',
                                 ("127.0.0.1:5555", "GET", path, "1.1", 200), None)
        assert f.filter(rec) is True
        line = rec.getMessage()
        assert uid not in line and quote_plus(uid) not in line
        assert f"uid=tk8:{router.tenant_key(uid)[:8]}&limit=5" in line

    def test_filter_is_installed_on_the_uvicorn_access_logger(self):
        import logging as _logging

        assert any(isinstance(f, router._AccessLogUidRedactor)
                   for f in _logging.getLogger("uvicorn.access").filters)

    def test_lines_without_uid_are_untouched(self):
        import logging as _logging

        rec = _logging.LogRecord("uvicorn.access", _logging.INFO, __file__, 1,
                                 '%s - "%s %s HTTP/%s" %d',
                                 ("1.2.3.4:1", "POST", "/ask", "1.1", 200), None)
        router._AccessLogUidRedactor().filter(rec)
        assert rec.args[2] == "/ask"


# ── router → launcher token ──────────────────────────────────────────────────


class TestLauncherToken:
    def _boot(self, monkeypatch, clean_state, flag: bool):
        monkeypatch.setattr(router, "LAUNCHER_AUTH", flag)

        async def ok():
            return _Resp(200, {"ok": True})

        fake = _FakeHttp(ok)
        monkeypatch.setattr(router, "http", fake)
        inst = router.Instance("t" * 64, "sbx-abc", None, None)
        router.pool[inst.tk] = inst
        env, key = router.engine_env(None, None)
        _run(router._boot_engine(inst, router.llm_fingerprint(None, None, env), env, key))
        return fake.boots[0], inst, env

    def test_off_by_default_header_only(self, monkeypatch, clean_state):
        call, inst, env = self._boot(monkeypatch, clean_state, flag=False)
        assert "VIBE_LAUNCHER_AUTH" not in env
        assert "VIBE_LAUNCHER_TOKEN" not in call["env"]
        assert call["headers"]["Authorization"] == f"Bearer {router.launcher_token('sbx-abc')}"

    def test_on_hands_the_per_sandbox_token_to_the_launcher(self, monkeypatch, clean_state):
        call, inst, env = self._boot(monkeypatch, clean_state, flag=True)
        token = router.launcher_token("sbx-abc")
        assert call["env"]["VIBE_LAUNCHER_TOKEN"] == token
        assert call["headers"]["Authorization"] == f"Bearer {token}"
        assert token != router.launcher_token("sbx-other")
        assert token == router.launcher_token("sbx-abc")  # derivable after a restart
        assert token not in (clean_state / "state.json").read_text()

    def test_switching_the_flag_changes_every_fingerprint(self, monkeypatch):
        monkeypatch.setattr(router, "LAUNCHER_AUTH", False)
        off = router.llm_fingerprint(None, None, router.engine_env(None, None)[0])
        monkeypatch.setattr(router, "LAUNCHER_AUTH", True)
        on = router.llm_fingerprint(None, None, router.engine_env(None, None)[0])
        assert off != on


# ── actionable error frames carry a code ─────────────────────────────────────


class TestErrorFrameCodes:
    def _ask_with_turn_error(self, monkeypatch, exc):
        h = _AskHarness(monkeypatch)

        async def post_turn(inst, sid, query, **kw):
            raise exc

        monkeypatch.setattr(router, "_post_turn", post_turn)
        return h.run()[-1]

    def test_length_cap_is_query_too_long(self, monkeypatch):
        r = _Resp(422, None, '{"detail":[{"type":"string_too_long","loc":["body","content"]}]}')
        frame = self._ask_with_turn_error(monkeypatch, router._QueryRejected(r))
        assert frame["status"] == 400 and frame["code"] == "query_too_long"
        assert "问题过长" in frame["detail"]

    def test_other_422_is_query_rejected(self, monkeypatch):
        r = _Resp(422, None, '{"detail":[{"type":"missing","loc":["body","x"]}]}')
        frame = self._ask_with_turn_error(monkeypatch, router._QueryRejected(r))
        assert frame["status"] == 400 and frame["code"] == "query_rejected"

    def test_plain_errors_carry_no_code(self, monkeypatch):
        frame = self._ask_with_turn_error(monkeypatch, HTTPException(502, "boom"))
        assert frame["status"] == 502 and "code" not in frame
