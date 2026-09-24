"""Tombstones for deleted sessions.

Deleting a session only signals its running attempt to stop; the attempt
keeps writing until it reaches its next cancel check, and every one of those
writes (attempt.json with the full prompt, the assistant receipt, the FTS
row, handoff.json, trace, tool-result offloads) used to recreate the session
directory with ``mkdir(parents=True)`` — undoing the delete the user asked
for. A tombstone is registered *before* anything is removed, and every
session-scoped writer checks it and refuses to write.

Two layers:

* an in-process set keyed by sessions root — the engine that deleted the
  session is the one running its attempt, so this covers the common race;
* a marker file ``sessions/.deleted/<session_id>`` — survives an engine
  restart, and lets a host-side deleter (the router's offline path) mark a
  session whose engine is paused mid-attempt. Any process may create it;
  readers only test for existence.

Session ids are never reused, so a tombstone is never lifted; marker files
older than :data:`TOMBSTONE_TTL_DAYS` are pruned at engine start (by then no
attempt of the session can still be alive).
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

TOMBSTONE_DIRNAME = ".deleted"
TOMBSTONE_TTL_DAYS = 30.0
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

_MARKED: set[tuple[str, str]] = set()
_LOCK = threading.Lock()


def _default_sessions_dir() -> Path:
    from src.core.paths import data_root

    return data_root() / "sessions"


def _key(sessions_dir: Path, session_id: str) -> tuple[str, str]:
    return (os.path.abspath(sessions_dir), session_id)


def _marker(sessions_dir: Path, session_id: str) -> Optional[Path]:
    if not _SESSION_ID_RE.match(session_id or ""):
        return None
    return sessions_dir / TOMBSTONE_DIRNAME / session_id


def mark(session_id: str, sessions_dir: Optional[Path] = None) -> None:
    """Register ``session_id`` as deleted (memory + marker file, best effort)."""
    if not session_id:
        return
    base = sessions_dir if sessions_dir is not None else _default_sessions_dir()
    with _LOCK:
        _MARKED.add(_key(base, session_id))
    marker = _marker(base, session_id)
    if marker is None:
        return
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch(exist_ok=True)
    except OSError as exc:  # the in-process mark still holds
        logger.warning("session tombstone file not written for %s: %s", session_id, exc)


def is_deleted(session_id: str, sessions_dir: Optional[Path] = None) -> bool:
    """Whether ``session_id`` was deleted (by this process or per marker file)."""
    if not session_id:
        return False
    base = sessions_dir if sessions_dir is not None else _default_sessions_dir()
    with _LOCK:
        if _key(base, session_id) in _MARKED:
            return True
    marker = _marker(base, session_id)
    return marker is not None and marker.exists()


def prune(sessions_dir: Path, max_age_days: float = TOMBSTONE_TTL_DAYS) -> int:
    """Remove marker files older than ``max_age_days``; returns the count."""
    root = sessions_dir / TOMBSTONE_DIRNAME
    if not root.is_dir():
        return 0
    cutoff = time.time() - max_age_days * 86400.0
    removed = 0
    for marker in root.iterdir():
        try:
            if marker.is_file() and not marker.is_symlink() and marker.stat().st_mtime < cutoff:
                marker.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def _reset_for_tests() -> None:
    with _LOCK:
        _MARKED.clear()
