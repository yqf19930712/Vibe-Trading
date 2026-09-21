"""Memory & session data lifecycle guards.

- Deleting a session takes its ``runs/<id>`` directories with it, and a
  session removed from the host while the engine was down (router offline
  delete) is dropped from ``sessions.db`` (FTS rows + goal ledger) at the
  next engine start and on sight by ``search()``.
- ``req.json`` keeps a prompt preview, never the full text.
- The memory index keeps ``user`` entries first, evicts the oldest non-user
  entry at the cap, honours ``VIBE_MEMORY_TTL_DAYS`` as a soft expiry, and
  survives concurrent adds without losing lines.
- ``recall`` ties break on ``created``; ``forget`` emits an audit event.
- The BackgroundManager singleton is reset between tests.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from pathlib import Path

import pytest

from src.agent.progress import ProgressEvent, _set_emitter
from src.core.state import REQUEST_PREVIEW_CHARS, RunStateStore, runs_for_session
from src.goal import GoalStore
from src.memory.persistent import MAX_INDEX_LINES, PersistentMemory
from src.session.events import EventBus
from src.session.search import SessionSearchIndex
from src.session.service import SessionService
from src.session.store import SessionStore
from src.tools.background_tools import get_background_manager
from src.tools.remember_tool import RememberTool


# ---------------------------------------------------------------- helpers


def _service(tmp_path: Path) -> tuple[SessionService, SessionSearchIndex]:
    svc = SessionService(
        store=SessionStore(base_dir=tmp_path / "sessions"),
        event_bus=EventBus(),
        runs_dir=tmp_path / "runs",
    )
    idx = SessionSearchIndex(db_path=tmp_path / "sessions.db")
    svc._search_index = idx
    return svc, idx


def _write_run(runs_dir: Path, session_id: str, prompt: str = "hello") -> Path:
    runs_dir.mkdir(parents=True, exist_ok=True)
    run_dir = RunStateStore().create_run_dir(runs_dir)
    RunStateStore().save_request(run_dir, prompt, {"session_id": session_id})
    return run_dir


def _entry(directory: Path, name: str, memory_type: str, age_days: float, created: str = "") -> Path:
    path = directory / f"{memory_type}_{name}.md"
    created_line = f"created: {created}\n" if created else ""
    path.write_text(
        f"---\nname: {name}\ndescription: {name} note\ntype: {memory_type}\n{created_line}---\n\nbody of {name}",
        encoding="utf-8",
    )
    ts = time.time() - age_days * 86400
    os.utime(path, (ts, ts))
    return path


# ---------------------------------------------------------------- V-M1: session deletion


class TestSessionDeleteTakesRunsAlong:
    def test_engine_delete_removes_the_session_runs_only(self, tmp_path: Path) -> None:
        svc, idx = _service(tmp_path)
        sess = svc.create_session(title="t")
        other = svc.create_session(title="o")
        mine = _write_run(svc.runs_dir, sess.session_id)
        theirs = _write_run(svc.runs_dir, other.session_id)
        orphan = svc.runs_dir / "no-req"
        orphan.mkdir()

        assert svc.delete_session(sess.session_id) is True

        assert not mine.exists()
        assert theirs.exists()
        assert orphan.exists()
        idx.close()

    def test_runs_for_session_skips_symlinks_and_bad_json(self, tmp_path: Path) -> None:
        runs = tmp_path / "runs"
        good = _write_run(runs, "s1")
        bad = runs / "bad"
        bad.mkdir()
        (bad / "req.json").write_text("{not json", encoding="utf-8")
        (runs / "link").symlink_to(good)

        assert runs_for_session(runs, "s1") == [good]
        assert runs_for_session(runs, "") == []
        assert runs_for_session(tmp_path / "missing", "s1") == []


class TestRequestSnapshotIsAPreview:
    def test_req_json_holds_preview_length_and_hash_not_the_text(self, tmp_path: Path) -> None:
        prompt = "结合我的持仓分析：" + "600519 贵州茅台 100 股 1680.00\n" * 40
        run_dir = _write_run(tmp_path / "runs", "s1", prompt)

        data = json.loads((run_dir / "req.json").read_text(encoding="utf-8"))

        assert data["prompt"] == prompt[:REQUEST_PREVIEW_CHARS]
        assert data["prompt_chars"] == len(prompt)
        assert data["prompt_truncated"] is True
        assert len(data["prompt_sha256"]) == 64
        assert data["context"] == {"session_id": "s1"}
        assert prompt not in (run_dir / "req.json").read_text(encoding="utf-8")

    def test_short_prompt_is_kept_whole(self, tmp_path: Path) -> None:
        run_dir = _write_run(tmp_path / "runs", "s1", "short")
        data = json.loads((run_dir / "req.json").read_text(encoding="utf-8"))
        assert data["prompt"] == "short"
        assert data["prompt_truncated"] is False


class TestOfflineDeleteIsReconciled:
    """The host removed ``sessions/<sid>`` while the engine was down."""

    def _offline_delete(self, svc: SessionService, idx: SessionSearchIndex, title: str) -> str:
        sess = svc.create_session(title=title)
        idx.index_session(sess.session_id, title)
        idx.index_message(sess.session_id, "user", f"analyze {title} momentum with my holdings")
        shutil.rmtree(svc.store.base_dir / sess.session_id)
        return sess.session_id

    def test_search_drops_a_hit_whose_directory_is_gone(self, tmp_path: Path) -> None:
        svc, idx = _service(tmp_path)
        gone = self._offline_delete(svc, idx, "Bitcoin")
        kept = svc.create_session(title="Ethereum")
        idx.index_message(kept.session_id, "user", "analyze Ethereum momentum")
        # Unbound index: the dead row is still a hit.
        assert gone in {m.session_id for m in idx.search("momentum", max_sessions=5)}

        idx.bind_store(svc.store.base_dir)
        hits = idx.search("momentum", max_sessions=5)

        assert [m.session_id for m in hits] == [kept.session_id]
        # The orphan rows were deleted on sight, not merely hidden.
        assert gone not in idx.list_session_ids()
        idx.close()

    def test_reconcile_at_startup_sweeps_fts_and_goal_rows(self, tmp_path: Path) -> None:
        svc, idx = _service(tmp_path)
        goals = GoalStore(tmp_path / "goals.db")
        gone = self._offline_delete(svc, idx, "Bitcoin")
        goals.replace_goal(session_id=gone, objective="Evaluate BTC.", criteria=["thesis"])
        kept = svc.create_session(title="Ethereum")
        idx.index_message(kept.session_id, "user", "keep me")
        goals.replace_goal(session_id=kept.session_id, objective="Evaluate ETH.", criteria=["thesis"])

        removed = svc.reconcile_orphans(goal_store=goals)

        assert removed == [gone]
        assert idx.search("Bitcoin") == []
        assert idx.search("keep") and idx.search("keep")[0].session_id == kept.session_id
        assert goals.list_session_ids() == [kept.session_id]
        idx.close()

    def test_reconcile_is_a_noop_when_nothing_is_orphaned(self, tmp_path: Path) -> None:
        svc, idx = _service(tmp_path)
        kept = svc.create_session(title="t")
        idx.index_message(kept.session_id, "user", "keep me")
        assert svc.reconcile_orphans() == []
        assert idx.search("keep")
        idx.close()


# ---------------------------------------------------------------- memory P2 #1: index order + TTL


class TestIndexPrioritisesUserEntries:
    def test_user_entries_lead_and_the_oldest_non_user_is_evicted(self, tmp_path: Path) -> None:
        pm = PersistentMemory(memory_dir=tmp_path)
        for i in range(MAX_INDEX_LINES - 1):
            _entry(tmp_path, f"note{i}", "project", age_days=i + 1)
        _entry(tmp_path, "risk", "user", age_days=400)  # oldest file of all
        _entry(tmp_path, "fresh_ref", "reference", age_days=0)

        pm._rebuild_index()
        lines = (tmp_path / "MEMORY.md").read_text(encoding="utf-8").split("\n")

        assert len(lines) == MAX_INDEX_LINES
        assert lines[0].startswith("- [risk](user_risk.md)")
        assert lines[1].startswith("- [fresh_ref](reference_fresh_ref.md)")
        assert f"[note{MAX_INDEX_LINES - 2}]" not in "\n".join(lines)  # oldest project note
        assert (tmp_path / f"project_note{MAX_INDEX_LINES - 2}.md").exists()

    def test_add_remove_and_consolidate_agree_on_the_order(self, tmp_path: Path) -> None:
        """One writer (`_rebuild_index`) — the snapshot no longer depends on the last op."""
        pm = PersistentMemory(memory_dir=tmp_path)
        pm.add("proj", "p", "project")
        pm.add("pref", "u", "user")
        after_add = (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
        pm.consolidate()
        after_consolidate = (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
        assert after_add == after_consolidate
        assert after_add.split("\n")[0].startswith("- [pref](user_pref.md)")


class TestSoftExpiry:
    def test_no_ttl_by_default(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VIBE_MEMORY_TTL_DAYS", raising=False)
        pm = PersistentMemory(memory_dir=tmp_path)
        _entry(tmp_path, "ancient", "project", age_days=3650)
        pm._rebuild_index()
        assert "[ancient]" in (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
        assert pm.find_relevant("ancient note")

    def test_expired_non_user_entries_leave_index_and_recall_but_stay_on_disk(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("VIBE_MEMORY_TTL_DAYS", "90")
        pm = PersistentMemory(memory_dir=tmp_path)
        old = _entry(tmp_path, "stale_view", "project", age_days=120)
        _entry(tmp_path, "recent_view", "project", age_days=10)
        _entry(tmp_path, "risk", "user", age_days=500)

        pm._rebuild_index()
        index = (tmp_path / "MEMORY.md").read_text(encoding="utf-8")

        assert "[stale_view]" not in index
        assert "[recent_view]" in index
        assert "[risk]" in index  # user entries never expire
        assert old.exists()
        recalled = [e.title for e in pm.find_relevant("stale_view note")]
        assert "stale_view" not in recalled and "recent_view" in recalled
        assert pm.find("stale_view") is not None  # still reachable by title
        assert "risk" in [e.title for e in pm.find_relevant("risk note")]

    def test_garbage_ttl_disables_expiry(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("VIBE_MEMORY_TTL_DAYS", "soon")
        pm = PersistentMemory(memory_dir=tmp_path)
        _entry(tmp_path, "ancient", "project", age_days=3650)
        pm._rebuild_index()
        assert "[ancient]" in (tmp_path / "MEMORY.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------- memory P2 #2: concurrent adds


class TestConcurrentAddsKeepEveryIndexLine:
    def test_two_instances_adding_in_parallel_lose_nothing(self, tmp_path: Path) -> None:
        """Two attempts of one tenant each hold their own PersistentMemory."""
        writers = [PersistentMemory(memory_dir=tmp_path) for _ in range(4)]
        per_writer = 15
        errors: list[BaseException] = []

        def _work(n: int) -> None:
            try:
                for i in range(per_writer):
                    writers[n].add(f"w{n}_entry{i}", f"body {n}-{i}", "project")
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=_work, args=(n,)) for n in range(len(writers))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        index = (tmp_path / "MEMORY.md").read_text(encoding="utf-8")
        for n in range(len(writers)):
            for i in range(per_writer):
                assert f"[w{n}_entry{i}]" in index
        assert (tmp_path / ".MEMORY.lock").exists()
        assert not list(tmp_path.glob("MEMORY.md*.tmp"))

    def test_lock_is_reentrant_across_consolidate(self, tmp_path: Path) -> None:
        pm = PersistentMemory(memory_dir=tmp_path)
        pm.add("dup", "a", "project")
        pm.add("dup", "b", "user")
        with pm._lock.held():
            stats = pm.consolidate()  # nested acquisition must not deadlock
        assert stats["duplicates_merged"] == 1


# ---------------------------------------------------------------- memory P2 #7: recall order + audit


class TestRecallTiesBreakOnCreated:
    def test_equal_scores_prefer_the_later_created_entry(self, tmp_path: Path) -> None:
        pm = PersistentMemory(memory_dir=tmp_path)
        _entry(tmp_path, "gold_a", "project", age_days=5, created="2026-01-01T00:00:00+00:00")
        _entry(tmp_path, "gold_b", "project", age_days=5, created="2026-06-01T00:00:00+00:00")
        ts = time.time() - 5 * 86400  # identical mtime → only `created` differs
        for p in (tmp_path / "project_gold_a.md", tmp_path / "project_gold_b.md"):
            os.utime(p, (ts, ts))

        titles = [e.title for e in pm.find_relevant("gold note")]

        assert titles[:2] == ["gold_b", "gold_a"]

    def test_legacy_entries_without_created_sort_last_among_ties(self, tmp_path: Path) -> None:
        pm = PersistentMemory(memory_dir=tmp_path)
        _entry(tmp_path, "gold_legacy", "project", age_days=5)
        _entry(tmp_path, "gold_new", "project", age_days=5, created="2026-06-01T00:00:00+00:00")
        ts = time.time() - 5 * 86400
        for p in (tmp_path / "project_gold_legacy.md", tmp_path / "project_gold_new.md"):
            os.utime(p, (ts, ts))
        titles = [e.title for e in pm.find_relevant("gold note")]
        assert titles[:2] == ["gold_new", "gold_legacy"]


class TestForgetIsAudited:
    def test_forget_emits_a_memory_forgotten_event(self, tmp_path: Path) -> None:
        tool = RememberTool(memory=PersistentMemory(memory_dir=tmp_path))
        tool.execute(action="save", title="tmp-note", content="x")
        events: list[ProgressEvent] = []
        _set_emitter(events.append)
        try:
            result = json.loads(tool.execute(action="forget", title="tmp-note"))
            missing = json.loads(tool.execute(action="forget", title="tmp-note"))
        finally:
            _set_emitter(None)

        assert result["status"] == "ok"
        assert missing["status"] == "not_found"
        stages = [(e.stage, e.message) for e in events]
        assert stages == [("memory_forgotten", "memory entry removed: tmp-note")]


# ---------------------------------------------------------------- background manager isolation


class TestBackgroundManagerIsolation:
    def test_singleton_starts_empty_here(self) -> None:
        mgr = get_background_manager()
        assert mgr.tasks == {}
        assert mgr.drain_notifications() == []

    def test_a_task_started_here_does_not_leak(self) -> None:
        mgr = get_background_manager()
        tid = json.loads(mgr.run("echo leak"))["task_id"]
        deadline = time.monotonic() + 10
        while mgr.tasks.get(tid, {}).get("status") == "running" and time.monotonic() < deadline:
            time.sleep(0.02)
        assert mgr.tasks[tid]["result"].strip() == "leak"
        # the autouse fixture resets after this test; the next test asserts emptiness

    def test_reset_makes_a_finishing_task_a_no_op(self) -> None:
        mgr = get_background_manager()
        tid = json.loads(mgr.run("sleep 0.3; echo late"))["task_id"]
        mgr.reset()
        time.sleep(0.6)
        assert tid not in mgr.tasks
        assert mgr.drain_notifications() == []
