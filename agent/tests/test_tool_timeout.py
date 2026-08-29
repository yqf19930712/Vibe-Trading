"""Tool timeout/liveness regression tests."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from typing import Any

from src.agent import loop as loop_mod
from src.agent.loop import AgentLoop


class _SlowRegistry:
    class _Tool:
        is_readonly = True

    def get(self, tool_name: str) -> object:
        return self._Tool()

    def execute(self, tool_name: str, args: dict[str, Any]) -> str:
        time.sleep(1.0)
        return '{"status":"ok"}'


class _SlowWriteRegistry:
    """Write tool that finishes inside the 2x grace window (warn, no kill)."""

    class _Tool:
        is_readonly = False

    def __init__(self, sleep_s: float = 0.03) -> None:
        self.completed = False
        self._sleep_s = sleep_s

    def get(self, tool_name: str) -> object:
        return self._Tool()

    def execute(self, tool_name: str, args: dict[str, Any]) -> str:
        time.sleep(self._sleep_s)
        self.completed = True
        return '{"status":"ok"}'


class _DeclaredTimeoutRegistry:
    """Write tool that declares its own ``timeout_seconds`` (V1)."""

    def __init__(self, sleep_s: float, declared: float) -> None:
        self.completed = False
        self._sleep_s = sleep_s
        self._declared = declared

    def get(self, tool_name: str) -> object:
        return SimpleNamespace(is_readonly=False, timeout_seconds=self._declared)

    def execute(self, tool_name: str, args: dict[str, Any]) -> str:
        time.sleep(self._sleep_s)
        self.completed = True
        return '{"status":"ok"}'


def test_tool_timeout_returns_error_and_stops_heartbeats(monkeypatch) -> None:
    """A hung tool should become a bounded diagnostic instead of heartbeating forever."""
    monkeypatch.setattr(loop_mod, "TOOL_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(loop_mod, "HEARTBEAT_INTERVAL_S", 0.01)
    events: list[tuple[str, dict[str, Any]]] = []
    agent = AgentLoop(
        registry=_SlowRegistry(),  # type: ignore[arg-type]
        llm=SimpleNamespace(),
        event_callback=lambda event_type, data: events.append((event_type, data)),
        max_iterations=1,
    )

    result, elapsed_ms = agent._invoke_tool("slow_tool", {})
    event_count_at_return = len(events)
    time.sleep(0.08)

    payload = json.loads(result)
    assert payload["status"] == "error"
    assert payload["error_code"] == "tool_timeout"
    assert payload["tool"] == "slow_tool"
    assert elapsed_ms >= 40
    assert len(events) == event_count_at_return


def test_write_tool_timeout_warns_but_completes_inside_grace(monkeypatch) -> None:
    """A write tool finishing between 1x and 2x the timeout warns but succeeds."""
    monkeypatch.setattr(loop_mod, "TOOL_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr(loop_mod, "HEARTBEAT_INTERVAL_S", 0.01)
    events: list[tuple[str, dict[str, Any]]] = []
    registry = _SlowWriteRegistry(sleep_s=0.03)  # 1.5x the timeout, inside 2x
    agent = AgentLoop(
        registry=registry,  # type: ignore[arg-type]
        llm=SimpleNamespace(),
        event_callback=lambda event_type, data: events.append((event_type, data)),
        max_iterations=1,
    )

    result, elapsed_ms = agent._invoke_tool("place_order", {})

    assert json.loads(result) == {"status": "ok"}
    assert registry.completed is True
    assert elapsed_ms >= 25
    assert any(
        event_type == "tool_progress" and data.get("stage") == "timeout_warning"
        for event_type, data in events
    )
    assert agent._stats.get("degraded") is not True


def test_write_tool_hard_timeout_abandons_and_marks_degraded(monkeypatch) -> None:
    """A write tool past 2x the timeout is abandoned with a structured error (F2)."""
    monkeypatch.setattr(loop_mod, "TOOL_TIMEOUT_SECONDS", 0.03)
    monkeypatch.setattr(loop_mod, "HEARTBEAT_INTERVAL_S", 0.01)
    events: list[tuple[str, dict[str, Any]]] = []
    registry = _SlowWriteRegistry(sleep_s=0.5)  # far past the 2x hard cap
    agent = AgentLoop(
        registry=registry,  # type: ignore[arg-type]
        llm=SimpleNamespace(),
        event_callback=lambda event_type, data: events.append((event_type, data)),
        max_iterations=1,
    )

    result, elapsed_ms = agent._invoke_tool("place_order", {})
    event_count_at_return = len(events)
    time.sleep(0.55)  # let the abandoned worker finish; its result is discarded

    payload = json.loads(result)
    assert payload["status"] == "error"
    assert payload["error_code"] == "write_tool_timeout"
    assert payload["degraded"] is True
    assert "background" in payload["message"]
    # Returned at ~2x the timeout, not after the tool's full 0.5s runtime.
    assert elapsed_ms < 400
    assert agent._stats.get("degraded") is True
    assert any(
        event_type == "tool_progress" and data.get("stage") == "timeout_warning"
        for event_type, data in events
    )
    assert any(
        event_type == "tool_progress" and data.get("stage") == "timeout"
        for event_type, data in events
    )
    # Late completion must not emit further events (emitters suppressed).
    assert len(events) == event_count_at_return


# --- V1: per-tool timeout_seconds declarations -----------------------------
# The three tests above are the F2 regression and must stay byte-identical:
# they use tools that declare NOTHING, so they pin that the 1x/2x semantics
# are unchanged for every ordinary write tool.


def test_declared_timeout_overrides_global(monkeypatch) -> None:
    """A declaration raises the base of the 1x/2x window: no warning, no kill."""
    monkeypatch.setattr(loop_mod, "TOOL_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr(loop_mod, "HEARTBEAT_INTERVAL_S", 0.01)
    events: list[tuple[str, dict[str, Any]]] = []
    registry = _DeclaredTimeoutRegistry(sleep_s=0.1, declared=5.0)
    agent = AgentLoop(
        registry=registry,  # type: ignore[arg-type]
        llm=SimpleNamespace(),
        event_callback=lambda event_type, data: events.append((event_type, data)),
        max_iterations=1,
    )

    result, _ = agent._invoke_tool("run_swarm", {})

    assert json.loads(result) == {"status": "ok"}
    assert registry.completed is True
    assert agent._stats.get("degraded") is not True
    # Not even the 1x warning should fire — the declaration moved the window.
    assert not any(
        data.get("stage") == "timeout_warning" for _, data in events
    )


def test_declared_timeout_still_clamped_by_attempt_budget(monkeypatch) -> None:
    """F2's original guarantee survives: a declaration cannot eat the attempt."""
    from src.core import budget

    monkeypatch.setattr(loop_mod, "TOOL_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr(loop_mod, "_TOOL_CAP_RESERVE_S", 0.05)
    # Scale the floors too, else the 10s "one quick shot" floor dominates a
    # sub-second test deadline and the assertion measures the floor, not the clamp.
    monkeypatch.setattr(loop_mod, "_TOOL_CAP_FLOOR_S", 0.05)
    monkeypatch.setattr(loop_mod, "_TOOL_GRACE_FLOOR_S", 0.02)
    monkeypatch.setattr(loop_mod, "HEARTBEAT_INTERVAL_S", 0.01)
    previous = budget.get_deadline()
    budget.bind_deadline(time.monotonic() + 0.3)
    try:
        registry = _DeclaredTimeoutRegistry(sleep_s=100.0, declared=100.0)
        agent = AgentLoop(
            registry=registry,  # type: ignore[arg-type]
            llm=SimpleNamespace(),
            event_callback=lambda event_type, data: None,
            max_iterations=1,
        )
        t0 = time.perf_counter()
        result, _ = agent._invoke_tool("run_swarm", {})
        elapsed = time.perf_counter() - t0
    finally:
        budget.bind_deadline(previous)

    assert elapsed < 1.5, "declared timeout escaped the attempt budget clamp"
    payload = json.loads(result)
    assert payload["error_code"] == "write_tool_timeout"
    assert agent._stats["degraded"] is True


def test_declared_timeout_cannot_lower_the_global(monkeypatch) -> None:
    """``max()`` semantics: a tool may widen its window, never narrow it."""
    monkeypatch.setattr(loop_mod, "TOOL_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(loop_mod, "HEARTBEAT_INTERVAL_S", 0.01)
    events: list[tuple[str, dict[str, Any]]] = []
    registry = _DeclaredTimeoutRegistry(sleep_s=0.1, declared=0.02)
    agent = AgentLoop(
        registry=registry,  # type: ignore[arg-type]
        llm=SimpleNamespace(),
        event_callback=lambda event_type, data: events.append((event_type, data)),
        max_iterations=1,
    )

    result, _ = agent._invoke_tool("place_order", {})

    assert json.loads(result) == {"status": "ok"}
    assert not any(data.get("stage") == "timeout_warning" for _, data in events)


def test_tool_cap_reserve_covers_the_finalize_reserve() -> None:
    """Abandoning a tool must leave enough time for the forced-finalize path."""
    assert loop_mod._TOOL_CAP_RESERVE_S >= loop_mod.FINALIZE_RESERVE_S


def test_undeclared_tool_keeps_the_global_timeout() -> None:
    """No declaration (and an unknown tool) must fall back to the global."""
    agent = AgentLoop(
        registry=_SlowWriteRegistry(),  # type: ignore[arg-type]
        llm=SimpleNamespace(),
        event_callback=lambda event_type, data: None,
        max_iterations=1,
    )
    assert agent._tool_timeout("place_order") == loop_mod.TOOL_TIMEOUT_SECONDS


def test_malformed_declaration_falls_back_to_the_global(monkeypatch) -> None:
    """A garbage ``timeout_seconds`` must not take the loop down."""

    class _BadRegistry:
        def get(self, tool_name: str) -> object:
            return SimpleNamespace(is_readonly=False, timeout_seconds="soon")

        def execute(self, tool_name: str, args: dict[str, Any]) -> str:
            return '{"status":"ok"}'

    monkeypatch.setattr(loop_mod, "TOOL_TIMEOUT_SECONDS", 12.0)
    agent = AgentLoop(
        registry=_BadRegistry(),  # type: ignore[arg-type]
        llm=SimpleNamespace(),
        event_callback=lambda event_type, data: None,
        max_iterations=1,
    )
    assert agent._tool_timeout("weird_tool") == 12.0
