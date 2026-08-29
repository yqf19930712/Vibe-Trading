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
