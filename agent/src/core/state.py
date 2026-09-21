"""Run state persistence: creates run directories and records status."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# ``req.json`` keeps only a preview of the user prompt. The full text already
# lives in the session's ``trace.jsonl`` (``start`` event) and is deleted with
# the session; a second full copy under ``runs/`` would outlive that deletion
# and carry the portfolio block laicai attaches to every question.
REQUEST_PREVIEW_CHARS = 200


class RunStateStore:
    """Run state store: manages run directories and their lifecycle status."""

    def create_run_dir(self, workspace: Path) -> Path:
        """Create a unique run directory.

        Args:
            workspace: Parent directory (typically runs/).

        Returns:
            Newly created run directory path.
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:18]
        suffix = uuid.uuid4().hex[:6]
        run_dir = workspace / f"{timestamp}_{suffix}"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "code").mkdir(exist_ok=True)
        (run_dir / "logs").mkdir(exist_ok=True)
        (run_dir / "artifacts").mkdir(exist_ok=True)
        return run_dir

    def save_request(self, run_dir: Path, prompt: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """Save the user request as a preview (not the full prompt text).

        ``prompt`` holds the first :data:`REQUEST_PREVIEW_CHARS` characters
        (what the run list / run detail pages display); ``prompt_chars`` and
        ``prompt_sha256`` identify the full text without storing it.

        Args:
            run_dir: Run directory.
            prompt: User prompt.
            context: Context metadata (``session_id`` links the run to its
                session so deleting the session can remove the run too).

        Returns:
            Saved payload.
        """
        text = prompt or ""
        preview = text[:REQUEST_PREVIEW_CHARS]
        payload = {
            "prompt": preview,
            "prompt_chars": len(text),
            "prompt_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "prompt_truncated": len(text) > REQUEST_PREVIEW_CHARS,
            "context": context,
        }
        self._write_json(run_dir / "req.json", payload)
        return payload

    def mark_success(self, run_dir: Path) -> None:
        """Mark the run as successful.

        Args:
            run_dir: Run directory.
        """
        self._write_json(run_dir / "state.json", {"status": "success"})

    def mark_failure(self, run_dir: Path, reason: str) -> None:
        """Mark the run as failed.

        Args:
            run_dir: Run directory.
            reason: Failure reason.
        """
        self._write_json(run_dir / "state.json", {"status": "failed", "reason": reason})

    @staticmethod
    def _write_json(path: Path, data: Any) -> None:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def runs_for_session(runs_dir: Path, session_id: str) -> List[Path]:
    """Return the run directories whose ``req.json`` names ``session_id``.

    A session's runs are only linked to it through ``req.json``
    (``context.session_id``); the attempt record's ``run_dir`` is set after
    the loop returns, so an attempt that died mid-run has no other pointer
    to its directory. Unreadable or malformed ``req.json`` files are skipped.

    Args:
        runs_dir: The ``runs/`` root.
        session_id: Session to match.

    Returns:
        Matching run directories (no particular order); empty when
        ``session_id`` is blank or ``runs_dir`` does not exist.
    """
    if not session_id or not runs_dir.is_dir():
        return []
    found: List[Path] = []
    for run_dir in runs_dir.iterdir():
        req = run_dir / "req.json"
        if not run_dir.is_dir() or run_dir.is_symlink() or not req.is_file():
            continue
        try:
            data = json.loads(req.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        context = data.get("context") if isinstance(data, dict) else None
        if isinstance(context, dict) and context.get("session_id") == session_id:
            found.append(run_dir)
    return found
