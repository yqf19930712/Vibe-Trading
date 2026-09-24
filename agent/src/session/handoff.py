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
import re
from datetime import datetime, timezone
from pathlib import Path

from src.core.atomic_write import atomic_write_text
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
# When a structured summary has to shrink, whole ``## `` sections are kept in
# this order: the template lists the current goal first but the open asks and
# the concrete numbers near the END, so cutting from the tail dropped exactly
# what the next attempt needs to carry on. Unknown sections rank after these.
SECTION_PRIORITY = (
    "goal",
    "pending user asks",
    "critical context",
    "constraints & preferences",
    "key decisions",
    "progress",
    "remaining work",
    "relevant files",
    "resolved questions",
    "tools & patterns",
)
# A section that does not fit whole is still included, clipped, when at
# least this many tokens are left for it.
_MIN_PARTIAL_TOKENS = 120
_SECTION_SPLIT_RE = re.compile(r"(?m)^(?=## )")


def _path(session_id: str) -> Path:
    """Return the sidecar path for a session."""
    return data_root() / "sessions" / session_id / HANDOFF_FILE


def _chars_within(text: str, max_tokens: int) -> int:
    """Longest prefix length of ``text`` within ``max_tokens`` (estimator walk-down)."""
    limit = len(text)
    while limit > 0 and estimate_text_tokens(text[:limit]) > max_tokens:
        limit = int(limit * 0.9)
    return limit


def _section_rank(section: str) -> int:
    heading = section.split("\n", 1)[0][3:].strip().lower()
    try:
        return SECTION_PRIORITY.index(heading)
    except ValueError:
        return len(SECTION_PRIORITY)


def fit_summary(summary: str, max_tokens: int, marker: str) -> str:
    """Shrink ``summary`` to ``max_tokens``, keeping the sections that matter.

    A structured summary (two or more ``## `` sections) keeps whole sections
    in :data:`SECTION_PRIORITY` order — goal, open asks, concrete numbers
    first — and names what was left out; the kept sections stay in their
    original order. Anything else is cut in the middle, not at the end, so
    both the opening and the latest state survive.

    Args:
        summary: Summary text.
        max_tokens: Token budget (CJK-weighted estimator).
        marker: Clip notice; ``{omitted}`` is filled with the dropped section
            names when present.

    Returns:
        The summary, unchanged when it already fits.
    """
    if estimate_text_tokens(summary) <= max_tokens:
        return summary
    parts = _SECTION_SPLIT_RE.split(summary)
    preamble = "" if parts[0].startswith("## ") else parts[0]
    sections = [p for p in parts if p.startswith("## ")]
    notice_tokens = estimate_text_tokens(marker) + 40
    if len(sections) < 2:
        note = marker.replace("{omitted}", "middle of the summary")
        room = max(0, max_tokens - estimate_text_tokens(note))
        head = _chars_within(summary, int(room * 0.6))
        tail_budget = room - estimate_text_tokens(summary[:head])
        tail = summary[head:]
        cut = len(tail) - _chars_within(tail[::-1], tail_budget)
        return summary[:head] + note + tail[cut:]

    budget = max_tokens - notice_tokens - estimate_text_tokens(preamble)
    kept: dict[int, str] = {}
    for index in sorted(range(len(sections)), key=lambda i: (_section_rank(sections[i]), i)):
        section = sections[index]
        cost = estimate_text_tokens(section)
        if cost <= budget:
            kept[index] = section
            budget -= cost
        elif budget >= _MIN_PARTIAL_TOKENS:
            kept[index] = section[: _chars_within(section, budget)].rstrip() + "\n...\n"
            budget = 0
    omitted = [
        sections[i].split("\n", 1)[0][3:].strip()
        for i in range(len(sections)) if i not in kept
    ]
    body = preamble + "".join(kept[i] for i in sorted(kept))
    return body.rstrip() + marker.replace("{omitted}", ", ".join(omitted) or "none")


def _clip(summary: str) -> str:
    """Clip a summary to :data:`HANDOFF_MAX_TOKENS` with a visible marker."""
    if estimate_text_tokens(summary) <= HANDOFF_MAX_TOKENS:
        return summary
    if len(_SECTION_SPLIT_RE.split(summary)) > 2:
        return fit_summary(
            summary, HANDOFF_MAX_TOKENS,
            "\n\n...[handoff summary clipped at the size cap; omitted: {omitted}]",
        )
    return summary[: _chars_within(summary, HANDOFF_MAX_TOKENS)] + _CLIP_MARKER


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
    from src.session import tombstone

    path = _path(session_id)
    # The sidecar belongs to an existing session: never recreate a deleted
    # (or deleting) session's directory just to hold its summary.
    if tombstone.is_deleted(session_id, path.parent.parent) or not path.parent.is_dir():
        logger.info("handoff for session %s not saved: session is gone", session_id)
        return False
    payload = {
        "summary": _clip(summary),
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "attempt_iter": int(attempt_iter or 0),
    }
    try:
        # Atomic replace: a concurrent reader never sees a half-written file.
        atomic_write_text(path, json.dumps(payload, ensure_ascii=False))
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
