"""Per-attempt cancellation signal shared across the loop and its tool threads.

Companion of :mod:`src.core.budget`: the AgentLoop binds its cancel
``threading.Event`` here at the start of ``run()``; every tool thread it
spawns via ``contextvars.copy_context()`` inherits the binding, so a tool
that polls something for minutes (``run_swarm`` waiting on a committee, the
watchdog waiting on any tool) can notice a user cancel between ticks instead
of only at the next iteration boundary.

A flag checked only between iterations is not enough: a cancel issued while a
30-minute tool wait is in flight would do nothing until the tool returned, and
the router's "unanswered → cancel" safety net could not actually stop the burn.
"""

from __future__ import annotations

import contextvars
import threading
import time
from typing import Optional

_CANCEL: contextvars.ContextVar[Optional[threading.Event]] = contextvars.ContextVar(
    "vibe_attempt_cancel", default=None
)

#: Longest single sleep any cooperative wait may take before re-checking.
CANCEL_POLL_S = 1.0


def bind_cancel_event(event: Optional[threading.Event]) -> contextvars.Token:
    """Bind the attempt's cancel event (None = unbound).

    Returns:
        The token :func:`reset_cancel_event` takes to restore the previous
        binding, so a set event does not outlive its attempt on this thread.
    """
    return _CANCEL.set(event)


def reset_cancel_event(token: contextvars.Token) -> None:
    """Restore the binding :func:`bind_cancel_event` replaced."""
    _CANCEL.reset(token)


def get_cancel_event() -> Optional[threading.Event]:
    """Return the bound cancel event, if any."""
    return _CANCEL.get()


def is_cancelled() -> bool:
    """Return whether the bound attempt (if any) has been cancelled."""
    ev = _CANCEL.get()
    return bool(ev is not None and ev.is_set())


def sleep_unless_cancelled(seconds: float, event: Optional[threading.Event] = None) -> bool:
    """Sleep up to ``seconds``, returning early when the cancel event fires.

    Drop-in replacement for ``time.sleep`` inside polling loops.

    Args:
        seconds: Wall-clock seconds to wait.
        event: Explicit event; defaults to the bound one.

    Returns:
        ``True`` when the wait ended because of a cancel, ``False`` when the
        full duration elapsed.
    """
    ev = event if event is not None else _CANCEL.get()
    if ev is None:
        time.sleep(max(0.0, seconds))
        return False
    return ev.wait(max(0.0, seconds))
