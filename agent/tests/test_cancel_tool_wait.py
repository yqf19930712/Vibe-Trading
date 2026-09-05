"""A3 (P1, review 2026-09-04): a cancel must cut through an in-flight tool wait.

``AgentLoop.cancel()`` used to only flip a flag checked between iterations;
while ``invoke_tool_guarded`` sat in ``queue.get(timeout=1800)`` the cancel
did nothing until the tool came back on its own.
"""

from __future__ import annotations

import json
import threading
import time

from src.agent.loop import invoke_tool_guarded
from src.core import cancel as _cancel


class _SlowRegistry:
    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self.started = threading.Event()

    def execute(self, name: str, args: dict) -> str:
        self.started.set()
        time.sleep(self.seconds)
        return "done"


def _guard(registry, cancel_event, timeout=60.0):
    events: list[tuple[str, dict]] = []
    result, elapsed = invoke_tool_guarded(
        registry,
        "slow_tool",
        {},
        readonly=True,
        timeout=timeout,
        emit=lambda t, d: events.append((t, d)),
        cancel_event=cancel_event,
    )
    return result, elapsed, events


def test_cancel_returns_within_two_seconds():
    reg = _SlowRegistry(30.0)
    ev = threading.Event()
    threading.Timer(0.5, ev.set).start()

    t0 = time.monotonic()
    result, _elapsed, events = _guard(reg, ev)
    took = time.monotonic() - t0

    assert took < 2.0, f"cancel took {took:.1f}s to surface"
    payload = json.loads(result)
    assert payload["status"] == "error"
    assert payload["error_code"] == "cancelled"
    assert any(d.get("stage") == "cancelled" for _t, d in events)


def test_cancel_during_write_tool_grace_period_also_returns():
    """Write tools get a grace window past the soft timeout — cancel must
    cut that short as well, not only the first wait."""
    reg = _SlowRegistry(30.0)
    ev = threading.Event()
    # Soft timeout 1s → grace window [1s, 2s) (WRITE_TOOL_TIMEOUT_FACTOR=2);
    # the cancel lands at 1.4s, inside the grace wait.
    threading.Timer(1.4, ev.set).start()

    t0 = time.monotonic()
    result, _elapsed = invoke_tool_guarded(
        reg, "slow_write", {}, readonly=False, timeout=1.0,
        emit=lambda t, d: None, cancel_event=ev,
    )
    took = time.monotonic() - t0

    assert 1.3 < took < 3.0, f"expected the cancel (1.4s) to end the wait, took {took:.2f}s"
    assert json.loads(result)["error_code"] == "cancelled"


def test_uncancelled_fast_tool_still_returns_its_result():
    reg = _SlowRegistry(0.05)
    result, _elapsed, _events = _guard(reg, threading.Event())
    assert result == "done"


def test_uncancelled_timeout_still_times_out():
    reg = _SlowRegistry(5.0)
    t0 = time.monotonic()
    result, _elapsed, _events = _guard(reg, threading.Event(), timeout=0.4)
    assert time.monotonic() - t0 < 2.0
    assert "timed out" in result.lower() or "timeout" in result.lower()


def test_bound_cancel_event_is_picked_up_by_default():
    """The loop binds its event via src.core.cancel; the guard must find it
    without being handed one explicitly (swarm worker path)."""
    ev = threading.Event()
    _cancel.bind_cancel_event(ev)
    try:
        threading.Timer(0.3, ev.set).start()
        t0 = time.monotonic()
        result, _elapsed = invoke_tool_guarded(
            _SlowRegistry(30.0), "slow", {}, readonly=True, timeout=60.0,
            emit=lambda t, d: None,
        )
        assert time.monotonic() - t0 < 2.0
        assert json.loads(result)["error_code"] == "cancelled"
    finally:
        _cancel.bind_cancel_event(None)


def test_sleep_unless_cancelled_wakes_early():
    ev = threading.Event()
    threading.Timer(0.2, ev.set).start()
    t0 = time.monotonic()
    assert _cancel.sleep_unless_cancelled(10.0, ev) is True
    assert time.monotonic() - t0 < 1.5
    assert _cancel.sleep_unless_cancelled(0.05, threading.Event()) is False
