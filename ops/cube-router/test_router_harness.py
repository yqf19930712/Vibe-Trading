"""Harness tests for cube-router: capacity cap, fast engine failure, cancel
bookkeeping, background-task references.

Run: VIBE_ROUTER_SECRET=x VIBE_ROUTER_TOKEN=y VIBE_CUBE_TEMPLATE_ID=tpl-test \
     python -m pytest test_router_harness.py

No CubeAPI, no sandbox: the sandbox / engine calls are replaced by async
fakes that yield to the event loop the way the real ones do.

  · RUNNING cap — N concurrent cold starts never exceed MAX_RUNNING (a
    booting instance already holds its slot); a re-attached (state.json)
    instance and a paused one being resumed also evict; a failed cold start
    gives its slot back and drops the sandbox; booting instances are never
    pause victims (LRU or reaper).
  · _wait_answer ends with _EngineFailed as soon as attempt.failed is seen
    on the event stream, without waiting for the next message poll.
  · engine_cancelled is stamped from the engine's answer to the cancel and
    the ask-log line is written by the cancel task, exactly once.
  · fire-and-forget tasks keep a strong reference until they finish.
"""
from __future__ import annotations

import asyncio
import os

os.environ.setdefault("VIBE_ROUTER_SECRET", "test-secret")
os.environ.setdefault("VIBE_ROUTER_TOKEN", "test-token")
os.environ.setdefault("VIBE_CUBE_TEMPLATE_ID", "tpl-test")

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
        return self._payload


@pytest.fixture()
def fake_cube(monkeypatch, tmp_path):
    """Empty pool + async sandbox fakes that record what they were asked."""
    calls: dict[str, list] = {"create": [], "pause": [], "delete": [], "ready": []}
    router.pool.clear()
    router.state.clear()
    router.uid_locks.clear()
    router._du_cache.clear()
    monkeypatch.setattr(router, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(router, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(router, "MAX_RUNNING", 2)
    monkeypatch.setattr(router, "capacity_lock", asyncio.Lock())
    monkeypatch.setattr(router, "pool_mutex", asyncio.Lock())

    async def sbx_create(tk):
        await asyncio.sleep(0.01)
        calls["create"].append(tk)
        return f"sbx-{tk[:6]}"

    async def sbx_pause(sandbox_id):
        await asyncio.sleep(0.005)
        calls["pause"].append(sandbox_id)

    async def sbx_delete(sandbox_id):
        calls["delete"].append(sandbox_id)
        return True

    async def sbx_info(sandbox_id):
        return {"sandboxID": sandbox_id}

    async def ensure_ready(inst, fp, env, api_key, meta=None):
        # Boot takes a while; the cap must already hold during it.
        calls["ready"].append(inst.tk)
        await asyncio.sleep(0.03)
        inst.paused = False
        inst.llm_fp = fp
        inst.api_key = api_key

    monkeypatch.setattr(router, "sbx_create", sbx_create)
    monkeypatch.setattr(router, "sbx_pause", sbx_pause)
    monkeypatch.setattr(router, "sbx_delete", sbx_delete)
    monkeypatch.setattr(router, "sbx_info", sbx_info)
    monkeypatch.setattr(router, "_ensure_ready", ensure_ready)
    monkeypatch.setattr(router, "engine_env", lambda model, llm: ({}, "key"))
    yield calls
    router.pool.clear()
    router.state.clear()
    router.uid_locks.clear()


def _idle_running(tk: str, idle_s: float = 100.0) -> router.Instance:
    inst = router.Instance(tk, f"sbx-{tk}", None, "key")
    inst.last_activity = time.monotonic() - idle_s
    router.pool[tk] = inst
    return inst


import time  # noqa: E402


# ── RUNNING cap ──────────────────────────────────────────────────────────────


class TestRunningCap:
    def test_concurrent_cold_starts_never_exceed_max_running(self, fake_cube):
        peak = {"n": 0}
        real_ready = router._ensure_ready

        async def ensure_ready(inst, *a, **k):
            running = sum(1 for i in router.pool.values() if not i.paused)
            peak["n"] = max(peak["n"], running)
            await real_ready(inst, *a, **k)

        router._ensure_ready = ensure_ready

        async def go():
            return await asyncio.gather(
                *(router.get_or_create(router.tenant_key(f"u{i}")) for i in range(4)),
                return_exceptions=True,
            )

        results = _run(go())

        ok = [r for r in results if isinstance(r, router.Instance)]
        busy = [r for r in results if isinstance(r, HTTPException)]
        assert len(ok) == 2 and len(busy) == 2, results
        assert all(e.status_code == 503 for e in busy)
        assert peak["n"] <= router.MAX_RUNNING
        assert len(fake_cube["create"]) == 2
        assert all(not i.booting for i in router.pool.values())
        assert len(router.pool) == 2

    def test_cold_start_evicts_the_lru_idle_instance(self, fake_cube):
        old = _idle_running("old", idle_s=300)
        _idle_running("newer", idle_s=10)

        inst = _run(router.get_or_create(router.tenant_key("fresh")))

        assert fake_cube["pause"] == [old.sandbox_id]
        assert old.paused is True
        assert inst.paused is False and inst.booting is False
        assert sum(1 for i in router.pool.values() if not i.paused) == 2

    def test_reattached_instance_takes_a_slot_first(self, fake_cube):
        tk = router.tenant_key("restarted")
        router.state[tk] = {
            "sandbox_id": "sbx-restarted", "template_id": router.TEMPLATE_ID,
            "llm_fp": None, "api_key": "key",
        }
        victim = _idle_running("a", idle_s=50)
        _idle_running("b", idle_s=5)

        inst = _run(router.get_or_create(tk))

        assert fake_cube["create"] == []            # re-attached, not rebuilt
        assert fake_cube["pause"] == [victim.sandbox_id]
        assert inst.sandbox_id == "sbx-restarted"
        assert router.pool[tk] is inst and not inst.paused and not inst.booting

    def test_resuming_a_paused_pool_instance_evicts_too(self, fake_cube):
        victim = _idle_running("a", idle_s=50)
        _idle_running("b", idle_s=5)
        paused = _idle_running("c", idle_s=1000)
        paused.paused = True

        inst = _run(router.get_or_create("c"))

        assert inst is paused
        assert fake_cube["pause"] == [victim.sandbox_id]
        assert not paused.paused

    def test_failed_cold_start_releases_slot_and_drops_sandbox(self, fake_cube):
        async def boom(inst, *a, **k):
            raise HTTPException(502, "engine boot failed")

        router._ensure_ready = boom
        tk = router.tenant_key("broken")
        with pytest.raises(HTTPException) as ei:
            _run(router.get_or_create(tk))

        assert ei.value.status_code == 502
        assert tk not in router.pool
        assert fake_cube["delete"] == [f"sbx-{tk[:6]}"]

    def test_booting_instance_is_never_a_pause_victim(self, fake_cube):
        booting = _idle_running("boot", idle_s=10_000)
        booting.booting = True
        _idle_running("busy", idle_s=10_000).refcount = 1

        with pytest.raises(HTTPException) as ei:
            _run(router._evict_for_capacity())
        assert ei.value.status_code == 503
        assert fake_cube["pause"] == []

        victims = _run(router._reap_idle_once())
        assert victims == []
        assert not booting.paused

    def test_evict_loops_until_under_the_cap(self, fake_cube, monkeypatch):
        monkeypatch.setattr(router, "MAX_RUNNING", 1)
        a = _idle_running("a", idle_s=30)
        b = _idle_running("b", idle_s=20)
        _idle_running("c", idle_s=10)

        _run(router._evict_for_capacity())

        # Room for one more under a cap of 1 means nothing may stay running;
        # victims go LRU-first.
        assert fake_cube["pause"] == [a.sandbox_id, b.sandbox_id, "sbx-c"]
        assert sum(1 for i in router.pool.values() if not i.paused) == 0


# ── attempt.failed short-circuits the answer wait ────────────────────────────


class TestWaitAnswerFailSignal:
    def test_fail_signal_ends_wait_before_next_poll(self, monkeypatch):
        monkeypatch.setattr(router, "POLL_INTERVAL_S", 5.0)
        polls = {"n": 0}

        async def vibe(inst, method, path, **kw):
            polls["n"] += 1
            return _Resp(200, [])

        monkeypatch.setattr(router, "_vibe", vibe)
        failed = router._FailSignal()

        async def go():
            async def fire():
                await asyncio.sleep(0.05)
                failed.fire("No space left on device")

            t = asyncio.create_task(fire())
            t0 = time.monotonic()
            with pytest.raises(router._EngineFailed) as ei:
                await router._wait_answer(None, "sid", "a1", 60, failed=failed)
            await t
            return ei.value, time.monotonic() - t0

        exc, elapsed = _run(go())
        assert elapsed < 2.0
        assert polls["n"] == 0
        assert exc.status_code == 502
        assert exc.engine_error == "No space left on device"
        assert router._classify_status(502, exc) == "engine_failed"

    def test_without_signal_a_failed_receipt_still_ends_the_wait(self, monkeypatch):
        monkeypatch.setattr(router, "POLL_INTERVAL_S", 0.01)

        async def vibe(inst, method, path, **kw):
            return _Resp(200, [{
                "role": "assistant", "content": "Execution failed: x",
                "linked_attempt_id": "a1", "metadata": {"ok": False, "error": "x"},
            }])

        monkeypatch.setattr(router, "_vibe", vibe)
        with pytest.raises(router._EngineFailed):
            _run(router._wait_answer(None, "sid", "a1", 5, failed=router._FailSignal()))


# ── engine_cancelled from the engine's answer, one ask-log line ──────────────


class TestCancelBookkeeping:
    @pytest.fixture()
    def recorded(self, monkeypatch):
        lines: list[dict] = []
        monkeypatch.setattr(router, "_record_ask", lambda stats: lines.append(dict(stats)))
        return lines

    def _inst(self):
        return router.Instance("tk", "sbx", None, "key")

    def test_confirmed_cancel(self, monkeypatch, recorded):
        async def vibe(inst, method, path, **kw):
            assert method == "POST" and path.endswith("/cancel")
            return _Resp(200, {"status": "cancelled"})

        monkeypatch.setattr(router, "_vibe", vibe)
        stats: dict = {"outcome": "timeout"}
        finalized = {"n": 0}

        def finalize():
            finalized["n"] += 1

        _run(router._cancel_attempt_bg(self._inst(), "sid", "tk", stats, finalize))

        assert stats["engine_cancelled"] is True
        assert stats["engine_cancel_status"] == "cancelled"
        assert finalized["n"] == 1
        assert recorded == [stats]

    def test_no_active_loop_is_not_a_confirmed_cancel(self, monkeypatch, recorded):
        async def vibe(inst, method, path, **kw):
            return _Resp(200, {"status": "no_active_loop"})

        monkeypatch.setattr(router, "_vibe", vibe)
        stats: dict = {}
        _run(router._cancel_attempt_bg(self._inst(), "sid", "tk", stats))
        assert stats["engine_cancelled"] is False
        assert stats["engine_cancel_status"] == "no_active_loop"
        assert len(recorded) == 1

    def test_unreachable_engine(self, monkeypatch, recorded):
        async def vibe(inst, method, path, **kw):
            raise ConnectionError("down")

        monkeypatch.setattr(router, "_vibe", vibe)
        stats: dict = {}
        _run(router._cancel_attempt_bg(self._inst(), "sid", "tk", stats))
        assert stats["engine_cancelled"] is False
        assert stats["engine_cancel_status"] == "unreachable"
        assert len(recorded) == 1


# ── background tasks keep a strong reference ─────────────────────────────────


def test_spawn_keeps_task_referenced_until_done():
    async def go():
        started = asyncio.Event()
        release = asyncio.Event()

        async def job():
            started.set()
            await release.wait()

        task = router._spawn(job())
        await started.wait()
        assert task in router._bg_tasks
        release.set()
        await task
        await asyncio.sleep(0)
        assert task not in router._bg_tasks

    _run(go())
