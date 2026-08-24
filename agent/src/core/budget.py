"""Per-attempt wall-clock budget shared across the loop and its tool threads.

The caller (laicai → router → ``POST /sessions/<sid>/messages``) supplies a
``deadline_s`` budget; the AgentLoop binds the absolute monotonic deadline here
so any long-running tool (swarm, market-data fallback chains, web research) can
cap its own internal timeout to the time actually left instead of a fixed
constant that may outlive the caller (the pre-batch-3 inverted-budget bug).

Propagation into worker threads relies on ``contextvars.copy_context()`` at
thread creation — already done by the loop/service for tool execution.
"""

from __future__ import annotations

import contextvars
import time
from typing import Optional

_DEADLINE: contextvars.ContextVar[Optional[float]] = contextvars.ContextVar(
    "vibe_attempt_deadline", default=None
)


def bind_deadline(deadline_monotonic: Optional[float]) -> None:
    """Bind the attempt's absolute ``time.monotonic()`` deadline (None = unbounded)."""
    _DEADLINE.set(deadline_monotonic)


def get_deadline() -> Optional[float]:
    """Return the bound absolute monotonic deadline, if any."""
    return _DEADLINE.get()


def remaining_s(default: Optional[float] = None) -> Optional[float]:
    """Seconds left until the attempt deadline; ``default`` when unbounded.

    Never negative — an expired budget returns 0.0.
    """
    deadline = _DEADLINE.get()
    if deadline is None:
        return default
    return max(0.0, deadline - time.monotonic())


def cap_timeout(requested_s: float, *, reserve_s: float = 0.0, floor_s: float = 5.0) -> float:
    """Clamp a timeout to the remaining attempt budget.

    Args:
        requested_s: The timeout the caller would use standalone.
        reserve_s: Seconds to keep back for post-processing after the wait.
        floor_s: Minimum returned value so a nearly-spent budget still allows
            one quick attempt instead of an instant failure.
    """
    left = remaining_s()
    if left is None:
        return requested_s
    return max(floor_s, min(requested_s, left - reserve_s))
