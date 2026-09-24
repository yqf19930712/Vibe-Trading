"""Swarm tail usage parked until the session's next attempt.

A swarm run the attempt stopped waiting for keeps working, and when it ends
the rest of its tokens are reported once as ``llm_usage`` with
``source="swarm_tail"`` (``src.tools.swarm_tool``). The caller bills what
arrives on the stream of a request, so that event only reaches it while an
attempt of the session is running. When none is, the report is parked here,
next to the session, and ``SessionService`` sends it at the start of the
session's next attempt — once: taking the reports removes the file.

Reports are keyed by ``tail_key`` (run id plus the run's billed totals after
the report), so parking the same report twice keeps one copy. The file lives
in the session directory and goes with the session when it is deleted.
Callers serialize access (``SessionService`` holds its tail lock).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List

from src.core.atomic_write import atomic_write_text
from src.session import tombstone

logger = logging.getLogger(__name__)

PENDING_FILE = "swarm_tail_pending.json"


def _path(sessions_root: Path, session_id: str) -> Path:
    return sessions_root / session_id / PENDING_FILE


def _read(path: Path) -> List[Dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        logger.warning("unreadable swarm tail file %s; its reports are dropped", path)
        return []
    tails = data.get("tails") if isinstance(data, dict) else None
    return [t for t in tails if isinstance(t, dict)] if isinstance(tails, list) else []


def park(sessions_root: Path, session_id: str, report: Dict[str, Any]) -> bool:
    """Keep ``report`` for the session's next attempt; False when it cannot be kept.

    Never recreates a deleted (or deleting) session's directory.
    """
    path = _path(sessions_root, session_id)
    if tombstone.is_deleted(session_id, sessions_root) or not path.parent.is_dir():
        return False
    tails = _read(path)
    key = report.get("tail_key")
    if key and any(t.get("tail_key") == key for t in tails):
        return True
    tails.append(dict(report))
    try:
        atomic_write_text(path, json.dumps({"tails": tails}, ensure_ascii=False))
    except OSError as exc:
        logger.warning("swarm tail for session %s not parked: %s", session_id, exc)
        return False
    return True


def take(sessions_root: Path, session_id: str) -> List[Dict[str, Any]]:
    """Remove and return the parked reports of a session.

    The file is removed before the reports are handed out: a report is sent
    at most once even if the process dies right after. When it cannot be
    removed nothing is returned, so the next attempt tries again instead of
    sending the same report twice.
    """
    path = _path(sessions_root, session_id)
    tails = _read(path)
    if not tails:
        return []
    try:
        path.unlink()
    except FileNotFoundError:
        return []
    except OSError as exc:
        logger.warning("swarm tail file of session %s not removed: %s", session_id, exc)
        return []
    return tails
