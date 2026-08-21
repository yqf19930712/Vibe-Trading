"""Structured JSONL logging for the engine (fork addition, see README_CUSTOM.md).

Upstream configures no logging handlers at all, so ``logger.info(...)`` calls
throughout the engine are dropped (root logger falls back to the WARNING-level
``lastResort`` stderr handler). This module gives the engine a real log sink:

- ``setup_logging()``      — idempotent; JSON-lines to ``<data_root>/logs/engine.jsonl``
                             (rotating) plus WARNING+ to stderr for journald.
- ``bind_log_context()``   — attaches ``session_id`` / ``attempt_id`` to every
                             record via contextvars, so multi-tenant operators can
                             correlate engine logs with the router's ask log and
                             laicai's ``deep_engine_runs`` rows by ``attempt_id``.

In multi-tenant production ``data_root()`` is the tenant dir bind-mounted from
the host (``/data/shared/vibe/<tk>/logs/engine.jsonl``), so logs are readable on
the host without entering the sandbox and survive pause/resume/rebuild.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Any, Optional

from src.core.paths import data_root

_LOG_SESSION_ID: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "vibe_log_session_id", default=None
)
_LOG_ATTEMPT_ID: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "vibe_log_attempt_id", default=None
)

# Attributes present on every LogRecord; anything else was passed via ``extra=``
# and is worth serializing as structured payload.
_STD_RECORD_ATTRS = frozenset(
    {
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "taskName", "message", "asctime",
    }
)

_MAX_LOG_BYTES = 20 * 1024 * 1024
_BACKUP_COUNT = 3


def bind_log_context(session_id: Optional[str] = None, attempt_id: Optional[str] = None) -> None:
    """Bind correlation ids into the current context (and threads spawned with
    ``contextvars.copy_context()``). ``None`` leaves the existing value alone."""
    if session_id is not None:
        _LOG_SESSION_ID.set(session_id)
    if attempt_id is not None:
        _LOG_ATTEMPT_ID.set(attempt_id)


class _ContextFilter(logging.Filter):
    """Attach the bound session/attempt ids to every record."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        record.session_id = _LOG_SESSION_ID.get()
        record.attempt_id = _LOG_ATTEMPT_ID.get()
        return True


class _JsonlFormatter(logging.Formatter):
    """One JSON object per line; ``extra=`` kwargs become structured fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        session_id = getattr(record, "session_id", None)
        attempt_id = getattr(record, "attempt_id", None)
        if session_id:
            payload["session_id"] = session_id
        if attempt_id:
            payload["attempt_id"] = attempt_id
        for key, value in record.__dict__.items():
            if key in _STD_RECORD_ATTRS or key in ("session_id", "attempt_id"):
                continue
            if key.startswith("_"):
                continue
            payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)[-2000:]
        return json.dumps(payload, ensure_ascii=False, default=str)


_configured = False


def setup_logging() -> None:
    """Configure root logging once. Safe to call from every entrypoint.

    ``VIBE_LOG_LEVEL`` (default INFO) sets the root level. File sink failures
    (read-only fs, missing dir perms) degrade to stderr-only rather than
    crashing the server.
    """
    global _configured
    if _configured:
        return
    _configured = True

    level_name = (os.getenv("VIBE_LOG_LEVEL") or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)
    context_filter = _ContextFilter()

    try:
        log_dir = data_root() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_dir / "engine.jsonl",
            maxBytes=_MAX_LOG_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
            delay=True,
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(_JsonlFormatter())
        file_handler.addFilter(context_filter)
        root.addHandler(file_handler)
    except OSError as exc:  # pragma: no cover - depends on fs perms
        sys.stderr.write(f"[logging_setup] file sink unavailable: {exc}\n")

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(logging.WARNING)
    stderr_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    stderr_handler.addFilter(context_filter)
    root.addHandler(stderr_handler)
