"""Deleting a session whose attempt is still running must stay deleted.

The attempt only stops at its next cancel check; its terminal bookkeeping
(attempt.json with the full prompt, the assistant receipt, the FTS row, the
handoff sidecar, trace / offloads into its run dir, swarm artifacts) used to
recreate ``sessions/<sid>/`` after the delete had returned. Also pins the
per-attempt in-flight bookkeeping and the empty-answer / full-disk outcomes
of the same terminal path.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from src.session import service as svc_mod
from src.session import tombstone
from src.session.events import EventBus
from src.session.models import AttemptStatus
from src.session.search import SessionSearchIndex
from src.session.store import SessionStore

SECRET = "持仓：贵州茅台 500 股，成本 1680；手机号 13800138000"


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data = tmp_path / "vt"
    monkeypatch.setenv("VIBE_DATA_DIR", str(data))
    idx = SessionSearchIndex(db_path=data / "sessions.db")
    monkeypatch.setattr(svc_mod, "get_shared_index", lambda: idx)
    store = SessionStore(data / "sessions")
    svc = svc_mod.SessionService(store=store, event_bus=EventBus(), runs_dir=data / "runs")
    yield svc, idx, data
    idx.close()


def _fts_rows(idx: SessionSearchIndex, sid: str) -> int:
    return idx._get_conn().execute(
        "select count(*) from messages where session_id=?", (sid,)
    ).fetchone()[0]


def test_delete_while_attempt_runs_does_not_resurrect_the_session(env) -> None:
    svc, idx, data = env
    gate = asyncio.Event()
    run_dir = data / "runs" / "20260101_000000_00_abcdef"

    async def fake_run_with_agent(attempt, **kw):
        await gate.wait()
        # What the loop tail does after the delete: trace, offload, handoff.
        sess_dir = data / "sessions" / attempt.session_id
        sess_dir.mkdir(parents=True, exist_ok=True)
        (sess_dir / "trace.jsonl").write_text(json.dumps({"prompt": SECRET}), encoding="utf-8")
        (run_dir / "tool-results").mkdir(parents=True, exist_ok=True)
        (run_dir / "tool-results" / "x.txt").write_text(SECRET, encoding="utf-8")
        from src.session import handoff

        handoff.save(attempt.session_id, f"## Goal\n{SECRET}")
        return {"status": "success", "content": "answer", "run_dir": str(run_dir)}

    svc._run_with_agent = fake_run_with_agent

    async def main() -> str:
        sid = svc.create_session("t").session_id
        await svc.send_message(sid, SECRET)
        await asyncio.sleep(0.05)
        assert svc.delete_session(sid) is True
        assert not (data / "sessions" / sid).exists()
        gate.set()
        for _ in range(50):
            await asyncio.sleep(0.02)
            if not svc._inflight:
                break
        return sid

    sid = asyncio.run(main())

    assert not (data / "sessions" / sid).exists()
    assert not run_dir.exists()
    assert _fts_rows(idx, sid) == 0
    assert svc.get_messages(sid) == []


def test_store_refuses_writes_for_deleted_or_missing_sessions(env) -> None:
    svc, _idx, data = env
    from src.session.models import Attempt, Message

    sid = svc.create_session("t").session_id
    svc.store.delete_session(sid)
    assert svc.store.append_message(Message(session_id=sid, role="assistant", content=SECRET)) is False
    svc.store.create_attempt(Attempt(session_id=sid, prompt=SECRET))
    assert not (data / "sessions" / sid).exists()

    live = svc.create_session("live").session_id
    tombstone.mark(live, svc.store.base_dir)
    svc.store.append_message(Message(session_id=live, role="user", content=SECRET))
    assert not (data / "sessions" / live / "messages.jsonl").exists()


def test_marker_file_tombstone_is_honoured_by_a_fresh_process(env, monkeypatch) -> None:
    svc, idx, data = env
    sid = svc.create_session("t").session_id
    # A host-side deleter (router offline path) drops the marker; the
    # engine's in-memory set knows nothing about it.
    (data / "sessions" / ".deleted").mkdir(parents=True, exist_ok=True)
    (data / "sessions" / ".deleted" / sid).touch()
    from src.session.models import Message

    assert svc.store.append_message(Message(session_id=sid, role="user", content="x")) is False
    idx.index_message(sid, "user", "should not be indexed")
    assert _fts_rows(idx, sid) == 0
    # The marker directory is not mistaken for a session.
    assert all(s.session_id != ".deleted" for s in svc.list_sessions())


def test_old_tombstone_markers_are_pruned(tmp_path: Path) -> None:
    import os
    import time

    root = tmp_path / "sessions"
    tombstone.mark("old", root)
    tombstone.mark("new", root)
    old = root / ".deleted" / "old"
    stamp = time.time() - (tombstone.TOMBSTONE_TTL_DAYS + 1) * 86400
    os.utime(old, (stamp, stamp))
    assert tombstone.prune(root) == 1
    assert not old.exists() and (root / ".deleted" / "new").exists()


def test_delete_removes_the_sessions_swarm_runs(env) -> None:
    svc, _idx, data = env
    from src.swarm.store import SwarmStore, run_owner, swarm_runs_root

    from src.swarm.models import SwarmRun

    sid = svc.create_session("t").session_id
    store = SwarmStore(swarm_runs_root())
    with run_owner(sid):
        store.create_run(SwarmRun(id="run-mine", preset_name="p", created_at="2026-01-01T00:00:00",
                                  user_vars={"goal": SECRET}))
    store.create_run(SwarmRun(id="run-other", preset_name="p", created_at="2026-01-01T00:00:00",
                              session_id="someone-else"))
    store.create_run(SwarmRun(id="run-legacy", preset_name="p", created_at="2026-01-01T00:00:00"))

    svc.delete_session(sid)

    root = swarm_runs_root()
    assert not (root / "run-mine").exists()
    assert (root / "run-other").exists()
    assert (root / "run-legacy").exists()


# ── per-attempt in-flight bookkeeping ────────────────────────────────────────


class _Loop:
    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


def test_old_attempt_cleanup_keeps_the_new_attempts_pending_cancel(env) -> None:
    svc, _idx, _data = env
    sid = "sess-abc"
    svc._inflight = {sid: {"A", "B"}}
    # A cancel arrives while B is still building its registry.
    assert svc.cancel_current(sid) is True
    assert svc._pending_cancel == {"A", "B"}
    # A finishes first and cleans up only its own entries.
    svc._inflight[sid].discard("A")
    svc._pending_cancel.discard("A")
    assert "B" in svc._pending_cancel and svc._inflight[sid] == {"B"}


def test_cancel_reaches_a_registered_loop_and_a_registering_attempt(env) -> None:
    svc, _idx, _data = env
    sid = "sess-abc"
    old_loop = _Loop()
    svc._active_loops[sid] = old_loop
    svc._attempt_loops["A"] = old_loop
    svc._inflight = {sid: {"A", "B"}}

    svc.cancel_current(sid)

    assert old_loop.cancelled
    assert "B" in svc._pending_cancel and "A" not in svc._pending_cancel


def test_back_to_back_attempts_old_finally_does_not_swallow_new_cancel(env, monkeypatch) -> None:
    """End-to-end through _run_attempt: A exits while B is registering."""
    svc, _idx, _data = env
    b_registering = asyncio.Event()
    a_release = asyncio.Event()
    seen: dict[str, Any] = {}

    async def fake_inner(session, attempt, **kw):
        if attempt.prompt == "A":
            await a_release.wait()
            return
        b_registering.set()
        await asyncio.sleep(0.05)  # A's finally runs meanwhile
        seen["pending_for_B"] = attempt.attempt_id in svc._pending_cancel

    monkeypatch.setattr(svc, "_run_attempt_inner", fake_inner)

    async def main() -> None:
        sid = svc.create_session("t").session_id
        await svc.send_message(sid, "A")
        await svc.send_message(sid, "B")
        await b_registering.wait()
        svc.cancel_current(sid)
        a_release.set()
        await asyncio.sleep(0.2)

    asyncio.run(main())
    assert seen["pending_for_B"] is True


# ── terminal outcome ─────────────────────────────────────────────────────────


def _run_one(svc, result: dict) -> tuple[str, str]:
    async def fake_run_with_agent(attempt, **kw):
        return result

    svc._run_with_agent = fake_run_with_agent

    async def main() -> tuple[str, str]:
        sid = svc.create_session("t").session_id
        out = await svc.send_message(sid, "q")
        for _ in range(50):
            await asyncio.sleep(0.01)
            if not svc._inflight:
                break
        return sid, out["attempt_id"]

    return asyncio.run(main())


def test_success_without_text_is_a_failure_not_a_canned_receipt(env) -> None:
    svc, _idx, _data = env
    sid, aid = _run_one(svc, {"status": "success", "content": "", "iterations": 50, "max_iterations": 50})
    attempt = svc.store.get_attempt(sid, aid)
    assert attempt.status == AttemptStatus.FAILED
    assert "max iterations" in attempt.error
    receipt = [m for m in svc.get_messages(sid) if m.role == "assistant"][0]
    assert receipt.metadata["ok"] is False
    assert "Strategy execution completed" not in receipt.content


def test_success_with_text_is_delivered(env) -> None:
    svc, _idx, _data = env
    sid, _aid = _run_one(svc, {"status": "success", "content": "最终答案"})
    receipt = [m for m in svc.get_messages(sid) if m.role == "assistant"][0]
    assert receipt.metadata["ok"] is True and receipt.content == "最终答案"


def test_full_disk_on_the_way_out_keeps_the_answer(env, monkeypatch) -> None:
    svc, _idx, _data = env
    real_append = svc.store.append_message

    def _append(message):
        if message.role == "assistant":
            raise OSError(28, "No space left on device")
        return real_append(message)

    def _update(attempt):
        if attempt.status != AttemptStatus.RUNNING:
            raise OSError(28, "No space left on device")

    monkeypatch.setattr(svc.store, "append_message", _append)
    monkeypatch.setattr(svc.store, "update_attempt", _update)
    sid, aid = _run_one(svc, {"status": "success", "content": "答案仍在"})

    receipts = [m for m in svc.get_messages(sid) if m.role == "assistant"]
    assert len(receipts) == 1
    assert receipts[0].metadata["ok"] is True and receipts[0].content == "答案仍在"
    assert receipts[0].linked_attempt_id == aid


def test_fts_failure_after_the_receipt_does_not_fail_the_attempt(env, monkeypatch) -> None:
    svc, idx, _data = env
    events: list[str] = []
    real_emit = svc.event_bus.emit
    monkeypatch.setattr(
        svc.event_bus, "emit",
        lambda sid, et, data=None: (events.append(et), real_emit(sid, et, data))[1],
    )

    def _index(session_id, role, content, tool_name=None):
        if role == "assistant":
            raise RuntimeError("fts locked")

    monkeypatch.setattr(idx, "index_message", _index)
    sid, _aid = _run_one(svc, {"status": "success", "content": "ok"})
    assert "attempt.completed" in events and "attempt.failed" not in events


# ── opt-in retention sweep ───────────────────────────────────────────────────


def _age(path: Path, days: float) -> None:
    import os
    import time

    ts = time.time() - days * 86400
    for f in path.iterdir():
        if f.is_file():
            os.utime(f, (ts, ts))


def test_retention_is_off_by_default(env, monkeypatch) -> None:
    svc, _idx, data = env
    monkeypatch.delenv("VIBE_SESSION_RETENTION_DAYS", raising=False)
    sid = svc.create_session("old").session_id
    _age(data / "sessions" / sid, 400)
    assert svc.sweep_expired_sessions() == []
    assert (data / "sessions" / sid).exists()


def test_retention_deletes_idle_sessions_through_delete_session(env, monkeypatch) -> None:
    svc, idx, data = env
    monkeypatch.setenv("VIBE_SESSION_RETENTION_DAYS", "90")
    old = svc.create_session("old").session_id
    idx.index_message(old, "user", "ancient question")
    fresh = svc.create_session("fresh").session_id
    _age(data / "sessions" / old, 120)
    _age(data / "sessions" / fresh, 10)

    assert svc.sweep_expired_sessions() == [old]
    assert not (data / "sessions" / old).exists()
    assert (data / "sessions" / fresh).exists()
    assert _fts_rows(idx, old) == 0


def test_retention_dry_run_only_reports(env, monkeypatch) -> None:
    svc, _idx, data = env
    monkeypatch.setenv("VIBE_SESSION_RETENTION_DAYS", "90")
    monkeypatch.setenv("VIBE_SESSION_RETENTION_DRY_RUN", "1")
    old = svc.create_session("old").session_id
    _age(data / "sessions" / old, 120)
    assert svc.sweep_expired_sessions() == [old]
    assert (data / "sessions" / old).exists()


def test_retention_never_touches_a_session_being_messaged(env, monkeypatch) -> None:
    svc, _idx, data = env
    monkeypatch.setenv("VIBE_SESSION_RETENTION_DAYS", "90")
    sid = svc.create_session("old but active").session_id
    _age(data / "sessions" / sid, 120)

    async def fake_run_with_agent(attempt, **kw):
        return {"status": "success", "content": "ok"}

    svc._run_with_agent = fake_run_with_agent

    async def main() -> None:
        await svc.send_message(sid, "back after four months")
        await asyncio.sleep(0.05)

    asyncio.run(main())
    assert (data / "sessions" / sid).exists()
