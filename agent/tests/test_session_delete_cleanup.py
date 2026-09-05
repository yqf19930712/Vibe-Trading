"""A10 engine half (P1, review 2026-09-04): deleting a session also drops its
``sessions.db`` FTS rows and cancels a live loop, so ``session_search`` stops
returning dead links and no orphan attempt keeps burning tokens.
"""

from __future__ import annotations

from pathlib import Path

from src.session.events import EventBus
from src.session.search import SessionSearchIndex
from src.session.service import SessionService
from src.session.store import SessionStore


class _FakeLoop:
    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


def _service(tmp_path: Path) -> tuple[SessionService, SessionSearchIndex]:
    svc = SessionService(
        store=SessionStore(base_dir=tmp_path / "sessions"),
        event_bus=EventBus(),
        runs_dir=tmp_path / "runs",
    )
    idx = SessionSearchIndex(db_path=tmp_path / "sessions.db")
    svc._search_index = idx
    return svc, idx


def test_delete_session_drops_fts_rows_and_files(tmp_path: Path) -> None:
    svc, idx = _service(tmp_path)
    sess = svc.create_session(title="bitcoin research")
    idx.index_session(sess.session_id, sess.title)
    idx.index_message(sess.session_id, "user", "analyze Bitcoin momentum please")
    assert idx.search("Bitcoin")

    assert svc.delete_session(sess.session_id) is True

    assert idx.search("Bitcoin") == []
    assert not (tmp_path / "sessions" / sess.session_id).exists()
    idx.close()


def test_delete_session_cancels_the_live_loop(tmp_path: Path) -> None:
    svc, idx = _service(tmp_path)
    sess = svc.create_session(title="t")
    loop = _FakeLoop()
    svc._active_loops[sess.session_id] = loop

    svc.delete_session(sess.session_id)

    assert loop.cancelled is True
    idx.close()


def test_delete_missing_session_is_false_and_index_cleanup_is_best_effort(tmp_path: Path) -> None:
    svc, idx = _service(tmp_path)

    class _Broken:
        def delete_session(self, session_id: str) -> int:
            raise RuntimeError("db locked")

    svc._search_index = _Broken()
    assert svc.delete_session("nope") is False
    idx.close()


def test_search_index_delete_session_returns_row_count(tmp_path: Path) -> None:
    idx = SessionSearchIndex(db_path=tmp_path / "s.db")
    idx.index_session("s1", "title")
    idx.index_message("s1", "user", "one")
    idx.index_message("s1", "assistant", "two")
    idx.index_session("s2", "other")
    idx.index_message("s2", "user", "keep me")

    assert idx.delete_session("s1") == 2
    assert idx.search("keep") and idx.search("keep")[0].session_id == "s2"
    assert idx.delete_session("s1") == 0
    idx.close()
