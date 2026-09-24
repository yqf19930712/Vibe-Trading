"""Filesystem-backed persistence for Session, Message, and Attempt records."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.session import tombstone
from src.session.models import Attempt, Message, Session

logger = logging.getLogger(__name__)


class SessionStore:
    """Filesystem-backed persistent storage.

    Directory structure::

        sessions/
        ├── {session_id}/
        │   ├── session.json
        │   ├── messages.jsonl
        │   └── attempts/
        │       └── {attempt_id}/
        │           └── attempt.json

    Attributes:
        base_dir: Root directory for session storage.
    """

    def __init__(self, base_dir: Path) -> None:
        """Initialize session storage.

        Args:
            base_dir: Root directory for session storage.
        """
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _session_dir(self, session_id: str) -> Path:
        return self.base_dir / session_id

    def _session_file(self, session_id: str) -> Path:
        return self._session_dir(session_id) / "session.json"

    def _messages_file(self, session_id: str) -> Path:
        return self._session_dir(session_id) / "messages.jsonl"

    def _attempt_dir(self, session_id: str, attempt_id: str) -> Path:
        return self._session_dir(session_id) / "attempts" / attempt_id

    def _attempt_file(self, session_id: str, attempt_id: str) -> Path:
        return self._attempt_dir(session_id, attempt_id) / "attempt.json"

    def is_deleted(self, session_id: str) -> bool:
        """Whether ``session_id`` carries a deletion tombstone."""
        return tombstone.is_deleted(session_id, self.base_dir)

    def _writable_dir(self, session_id: str, target_dir: Path) -> bool:
        """Create ``target_dir`` inside an existing, live session; False to skip the write.

        Writes into a session never recreate its root directory: a missing
        root means the session was deleted (or never existed), and a
        tombstoned one is being deleted right now. Intermediate directories
        below the root are created one level at a time, so a delete racing
        this call cannot be undone by a ``parents=True`` walk.
        """
        session_dir = self._session_dir(session_id)
        if self.is_deleted(session_id):
            logger.info("session %s is deleted; write to %s skipped", session_id, target_dir.name)
            return False
        if not session_dir.is_dir():
            logger.info("session %s has no directory; write to %s skipped", session_id, target_dir.name)
            return False
        try:
            relative = target_dir.relative_to(session_dir)
        except ValueError:
            return False
        current = session_dir
        for part in relative.parts:
            current = current / part
            try:
                current.mkdir(exist_ok=True)
            except FileNotFoundError:
                return False
        return True

    # ---- Session CRUD ----

    def create_session(self, session: Session) -> Session:
        """Create and persist a session.

        Args:
            session: Session instance to create.

        Returns:
            The persisted Session.

        Raises:
            ValueError: Raised when the session already exists.
        """
        session_dir = self._session_dir(session.session_id)
        if session_dir.exists():
            raise ValueError(f"Session {session.session_id} already exists")
        session_dir.mkdir(parents=True)
        (session_dir / "attempts").mkdir()
        self._write_json(self._session_file(session.session_id), session.to_dict())
        return session

    def get_session(self, session_id: str) -> Optional[Session]:
        """Read a session.

        Args:
            session_id: Session ID.

        Returns:
            The Session instance, or None when it does not exist.
        """
        path = self._session_file(session_id)
        data = self._read_json(path)
        if data is None:
            return None
        return Session.from_dict(data)

    def update_session(self, session: Session) -> None:
        """Update a session (skipped once the session is deleted).

        Args:
            session: Modified Session instance.
        """
        path = self._session_file(session.session_id)
        if not self._writable_dir(session.session_id, path.parent):
            return
        self._write_json(path, session.to_dict())

    def delete_session(self, session_id: str) -> bool:
        """Delete a session and all of its data.

        Args:
            session_id: Session ID.

        Returns:
            Whether the delete succeeded.
        """
        session_dir = self._session_dir(session_id)
        if session_dir.is_symlink():
            session_dir.unlink(missing_ok=True)
            return True
        if not session_dir.exists():
            return False
        import shutil
        shutil.rmtree(session_dir, ignore_errors=True)
        return True

    def list_sessions(self, limit: int = 50) -> List[Session]:
        """List all sessions in descending update-time order.

        Args:
            limit: Maximum number of sessions to return.

        Returns:
            List of Session objects.
        """
        sessions: List[Session] = []
        if not self.base_dir.exists():
            return sessions
        for session_dir in self.base_dir.iterdir():
            if not session_dir.is_dir():
                continue
            session_file = session_dir / "session.json"
            data = self._read_json(session_file)
            if data:
                sessions.append(Session.from_dict(data))
        sessions.sort(key=lambda s: s.updated_at, reverse=True)
        return sessions[:limit]

    # ---- Message Append-Only Log ----

    def append_message(self, message: Message) -> bool:
        """Append a message to the session JSONL log.

        Args:
            message: Message to append.

        Returns:
            False when the session is deleted / missing and nothing was written.
        """
        path = self._messages_file(message.session_id)
        if not self._writable_dir(message.session_id, path.parent):
            return False
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(message.to_dict(), ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        return True

    def get_messages(self, session_id: str, limit: int = 100) -> List[Message]:
        """Read all messages for a session.

        Args:
            session_id: Session ID.
            limit: Maximum number of messages to return.

        Returns:
            List of Message objects in chronological order.
        """
        path = self._messages_file(session_id)
        if not path.exists():
            return []
        messages: List[Message] = []
        for line in path.read_text(encoding="utf-8").strip().splitlines():
            if line.strip():
                try:
                    messages.append(Message.from_dict(json.loads(line)))
                except json.JSONDecodeError:
                    logger.warning(
                        "Skipping corrupted message line in session %s: %s",
                        session_id,
                        line[:200],
                    )
        return messages[-limit:]

    # ---- Attempt CRUD ----

    def create_attempt(self, attempt: Attempt) -> Attempt:
        """Create an execution attempt.

        Args:
            attempt: Attempt to create.

        Returns:
            The persisted Attempt.
        """
        attempt_dir = self._attempt_dir(attempt.session_id, attempt.attempt_id)
        if not self._writable_dir(attempt.session_id, attempt_dir):
            return attempt
        self._write_json(
            self._attempt_file(attempt.session_id, attempt.attempt_id),
            attempt.to_dict(),
        )
        return attempt

    def get_attempt(self, session_id: str, attempt_id: str) -> Optional[Attempt]:
        """Read an execution attempt.

        Args:
            session_id: Session ID.
            attempt_id: Attempt ID.

        Returns:
            The Attempt instance, or None when it does not exist.
        """
        path = self._attempt_file(session_id, attempt_id)
        data = self._read_json(path)
        if data is None:
            return None
        return Attempt.from_dict(data)

    def update_attempt(self, attempt: Attempt) -> None:
        """Update an execution attempt (skipped once the session is deleted).

        Args:
            attempt: Modified Attempt.
        """
        path = self._attempt_file(attempt.session_id, attempt.attempt_id)
        if not self._writable_dir(attempt.session_id, path.parent):
            return
        self._write_json(path, attempt.to_dict())

    # ---- IO Helpers ----

    @staticmethod
    def _write_json(path: Path, data: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def _read_json(path: Path) -> Optional[Dict[str, Any]]:
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
