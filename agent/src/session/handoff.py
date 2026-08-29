"""Session-level handoff summary sidecar (V2).

``AgentLoop._previous_summary`` — the structured Layer 3 summary that Layer 5
iteratively updates — is instance state reset on every ``run()``. Anything the
loop compressed away in attempt N was therefore invisible to attempt N+1: a
laicai thread bound to the same ``vibe_session_id`` only replayed a sliding
window of raw user/assistant text.

This module persists that summary next to the session it belongs to. It lives
in ``src.session`` rather than ``src.agent`` on purpose: the lifetime is the
session's, so ``/forget`` and any future retention sweep delete it along with
``messages.jsonl`` without needing to know it exists.

Not a ``Message`` in ``messages.jsonl`` because that store is append-only and
user-visible: a rewritable derived artifact does not belong there (it would
show up in the session's message list as a turn the user never said).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from src.core.paths import data_root
from src.core.token_estimate import estimate_text_tokens

logger = logging.getLogger(__name__)

HANDOFF_FILE = "handoff.json"
# The structured template is naturally bounded; anything past this means the
# iterative update ran away, so it is clipped with a visible marker.
HANDOFF_MAX_TOKENS = 4_000
# A summary older than this is not carried into a new attempt: the user has
# almost certainly moved on, and a stale handoff is worse than none.
HANDOFF_TTL_DAYS = 14.0
_CLIP_MARKER = "\n\n...[handoff summary clipped at the size cap]"


def _path(session_id: str) -> Path:
    """Return the sidecar path for a session."""
    return data_root() / "sessions" / session_id / HANDOFF_FILE


def _clip(summary: str) -> str:
    """Clip a summary to :data:`HANDOFF_MAX_TOKENS` with a visible marker."""
    if estimate_text_tokens(summary) <= HANDOFF_MAX_TOKENS:
        return summary
    # Character budget derived from the same weighted estimator, then a short
    # binary walk-down so the clip lands close to the token cap for any script.
    limit = len(summary)
    while limit > 0 and estimate_text_tokens(summary[:limit]) > HANDOFF_MAX_TOKENS:
        limit = int(limit * 0.9)
    return summary[:limit] + _CLIP_MARKER


def save(session_id: str, summary: str, *, attempt_iter: int = 0) -> bool:
    """Persist the latest handoff summary for a session.

    Called the moment Layer 3 produces the summary, not at run end: an attempt
    that later times out or crashes is exactly the one whose summary matters.

    Args:
        session_id: Session identifier.
        summary: Structured summary text.
        attempt_iter: Trace iteration the summary was produced at (diagnostics).

    Returns:
        True when written. Never raises — persistence is an enhancement.
    """
    if not session_id or not summary or not summary.strip():
        return False
    path = _path(session_id)
    payload = {
        "summary": _clip(summary),
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "attempt_iter": int(attempt_iter or 0),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic replace: a concurrent reader never sees a half-written file.
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
            os.replace(tmp_name, path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
    except OSError as exc:  # noqa: BLE001 - full disk must not kill the attempt
        logger.warning("handoff save failed for session %s: %s", session_id, exc)
        return False
    return True


def load(session_id: str) -> str:
    """Return the stored handoff summary for a session.

    Args:
        session_id: Session identifier.

    Returns:
        The summary text, or ``""`` when absent, unreadable, malformed, or
        older than :data:`HANDOFF_TTL_DAYS`.
    """
    if not session_id:
        return ""
    try:
        raw = _path(session_id).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return ""
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    summary = payload.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return ""
    updated_at = payload.get("updated_at")
    if isinstance(updated_at, str) and updated_at:
        try:
            stamp = datetime.fromisoformat(updated_at)
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            age_days = (datetime.now(timezone.utc) - stamp).total_seconds() / 86400.0
            if age_days > HANDOFF_TTL_DAYS:
                return ""
        except ValueError:
            pass
    return summary


def clear(session_id: str) -> None:
    """Delete the sidecar for a session (best effort)."""
    if not session_id:
        return
    try:
        _path(session_id).unlink(missing_ok=True)
    except OSError:  # noqa: BLE001
        logger.debug("handoff clear failed for session %s", session_id, exc_info=True)
