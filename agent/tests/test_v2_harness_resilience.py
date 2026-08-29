"""V2 harness resilience: hysteresis, circuit breaker, and compaction guards.

Each test here pins one of the "Correct" gaps the harness review found:
Layer 1 rewrote the trajectory every turn past its trigger (cache churn), an
identical failing tool call could repeat until the iteration cap, a degraded
empty provider turn killed an hour-long attempt without one retry, and the
compaction LLM call — a correction mechanism — could itself fail the run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.agent import loop as loop_mod
from src.agent.loop import (
    MICROCOMPACT_ARMED_KEEP_RATIO,
    MICROCOMPACT_KEEP_BUDGET_RATIO,
    MICROCOMPACT_RELEASE_RATIO,
    MICROCOMPACT_TRIGGER_RATIO,
    SUMMARY_INPUT_TOKEN_BUDGET,
    TOOL_CIRCUIT_FAILURE_LIMIT,
    _CLEARED_PLACEHOLDER,
    _microcompact,
    _select_summary_input,
)


def _tool_msgs(count: int, size: int = 4000) -> list:
    return [
        {
            "role": "tool",
            "tool_call_id": f"c{i}",
            "name": "read_file",
            "content": f"result {i} " + "x" * size,
        }
        for i in range(count)
    ]


class TestMicrocompactHysteresis:
    def test_below_the_trigger_nothing_changes(self) -> None:
        messages = _tool_msgs(6, size=10)
        before = [m["content"] for m in messages]
        state: dict[str, Any] = {}

        _microcompact(messages, token_threshold=1_000_000, state=state)

        assert [m["content"] for m in messages] == before
        assert state.get("armed") is not True

    def test_crossing_the_trigger_arms_and_cuts_deeper(self) -> None:
        """The armed cut uses the deep water mark, not the shallow one."""
        assert MICROCOMPACT_ARMED_KEEP_RATIO < MICROCOMPACT_KEEP_BUDGET_RATIO
        # Sized so the keep BUDGET decides the cut, not the KEEP_RECENT floor:
        # 60 results x ~1000 tokens against a 100k threshold means the shallow
        # ratio keeps ~25 and the armed ratio ~15.
        messages = _tool_msgs(60)
        state: dict[str, Any] = {}

        _microcompact(messages, token_threshold=100_000, state=state)

        assert state["armed"] is True
        cleared = sum(1 for m in messages if m["content"] == _CLEARED_PLACEHOLDER)
        assert cleared > 0

        stateless = _tool_msgs(60)
        _microcompact(stateless, token_threshold=100_000)
        cleared_shallow = sum(
            1 for m in stateless if m["content"] == _CLEARED_PLACEHOLDER
        )
        assert cleared > cleared_shallow, "armed pass must prune more, not less"

    def test_staying_armed_between_trigger_and_release_keeps_cutting(self) -> None:
        messages = _tool_msgs(20)
        state = {"armed": True}

        _microcompact(messages, token_threshold=10_000, state=state)

        assert state["armed"] is True

    def test_falling_below_release_disarms_and_leaves_bytes_alone(self) -> None:
        """The cache-friendly half: once quiet, the trajectory stops changing."""
        assert MICROCOMPACT_RELEASE_RATIO < MICROCOMPACT_TRIGGER_RATIO
        messages = _tool_msgs(6, size=10)
        before = [m["content"] for m in messages]
        state = {"armed": True}

        _microcompact(messages, token_threshold=1_000_000, state=state)

        assert state["armed"] is False
        assert [m["content"] for m in messages] == before

    def test_stateless_call_preserves_pre_v2_behavior(self) -> None:
        """Callers that pass no state keep the single-threshold semantics."""
        messages = _tool_msgs(6, size=10)
        before = [m["content"] for m in messages]
        _microcompact(messages, token_threshold=1_000_000)
        assert [m["content"] for m in messages] == before


class TestSummaryInputSelection:
    def test_drops_the_OLDEST_messages_not_the_newest(self) -> None:
        """P2-5: the old ``[:80000]`` tail cut discarded the densest turns."""
        head = [
            {"role": "user", "content": f"MARK{i} " + "w " * 8000}
            for i in range(20)
        ]

        text, dropped = _select_summary_input(head)

        assert dropped > 0
        assert "MARK19" in text, "the newest turn must always survive"
        assert "MARK0" not in text, "the oldest turns are the ones sacrificed"

    def test_dropped_messages_are_announced(self) -> None:
        head = [{"role": "user", "content": "w " * 20000} for _ in range(20)]
        text, dropped = _select_summary_input(head)
        assert dropped > 0
        assert f"[{dropped} older messages omitted" in text

    def test_small_input_is_passed_through_whole(self) -> None:
        head = [{"role": "user", "content": "short"}]
        text, dropped = _select_summary_input(head)
        assert dropped == 0
        assert "short" in text
        assert "older messages omitted" not in text

    def test_budget_is_a_token_budget(self) -> None:
        from src.core.token_estimate import estimate_text_tokens

        head = [{"role": "user", "content": "中文" * 3000} for _ in range(40)]
        text, _ = _select_summary_input(head)
        assert estimate_text_tokens(text) <= SUMMARY_INPUT_TOKEN_BUDGET * 1.1


# ---------------------------------------------------------------------------
# Circuit breaker / empty-response retry / compaction failure — via AgentLoop
# ---------------------------------------------------------------------------


class _ToolCall:
    def __init__(self, name: str, arguments: dict, call_id: str = "c1") -> None:
        self.name = name
        self.arguments = arguments
        self.id = call_id


class _AlwaysFailsRegistry:
    """Registry whose single tool always returns a structured error."""

    def __init__(self) -> None:
        self.calls = 0

    def get(self, name: str) -> Any:
        return type(
            "T", (), {"repeatable": False, "is_readonly": True, "timeout_seconds": None}
        )()

    def get_definitions(self) -> list:
        return []

    def execute(self, name: str, args: dict) -> str:
        self.calls += 1
        return json.dumps({"status": "error", "error": "upstream is down"})


class _StubLLM:
    model_name = "stub"

    def chat(self, messages: list, **kwargs: Any) -> Any:
        raise AssertionError("not used")


def _loop_with(registry: Any, tmp_path: Path) -> Any:
    from src.agent.memory import WorkspaceMemory

    memory = WorkspaceMemory()
    memory.run_dir = str(tmp_path)
    return loop_mod.AgentLoop(registry=registry, llm=_StubLLM(), memory=memory)


class _Trace:
    def __init__(self) -> None:
        self.entries: list[dict] = []

    def write(self, entry: dict) -> None:
        self.entries.append(entry)

    def write_tool_result(self, **kwargs: Any) -> None:
        self.entries.append({"type": "tool_result", **kwargs})


class TestToolCircuitBreaker:
    def test_identical_failures_open_the_circuit(self, tmp_path: Path) -> None:
        """P2-1: the duplicate guard only registered SUCCESSES, so a dead
        upstream could be re-called until max_iterations."""
        from src.agent.context import ContextBuilder

        registry = _AlwaysFailsRegistry()
        agent = _loop_with(registry, tmp_path)
        context = ContextBuilder(registry, agent.memory)
        messages: list = []
        trace = _Trace()

        for i in range(TOOL_CIRCUIT_FAILURE_LIMIT + 2):
            agent._process_tool_calls(
                [_ToolCall("flaky", {"symbol": "AAPL"}, f"c{i}")],
                context,
                messages,
                trace,
                [],
                iteration=i + 1,
            )

        # The tool ran exactly up to the limit; every later attempt is refused.
        assert registry.calls == TOOL_CIRCUIT_FAILURE_LIMIT
        opened = [e for e in trace.entries if e.get("type") == "tool_circuit_open"]
        assert len(opened) == 2

    def test_open_circuit_returns_an_actionable_error(self, tmp_path: Path) -> None:
        from src.agent.context import ContextBuilder

        registry = _AlwaysFailsRegistry()
        agent = _loop_with(registry, tmp_path)
        context = ContextBuilder(registry, agent.memory)
        messages: list = []

        for i in range(TOOL_CIRCUIT_FAILURE_LIMIT + 1):
            agent._process_tool_calls(
                [_ToolCall("flaky", {"symbol": "AAPL"}, f"c{i}")],
                context,
                messages,
                _Trace(),
                [],
                iteration=i + 1,
            )

        payload = json.loads(messages[-1]["content"])
        assert payload["error_code"] == "circuit_open"
        assert payload["consecutive_failures"] >= TOOL_CIRCUIT_FAILURE_LIMIT
        # It must tell the model what to do instead of just saying "no".
        assert "change the arguments" in payload["message"]

    def test_different_arguments_are_a_different_circuit(self, tmp_path: Path) -> None:
        """Keyed on (tool, args) — the dea1222743ef lesson, not tool name."""
        from src.agent.context import ContextBuilder

        registry = _AlwaysFailsRegistry()
        agent = _loop_with(registry, tmp_path)
        context = ContextBuilder(registry, agent.memory)
        messages: list = []

        for i in range(TOOL_CIRCUIT_FAILURE_LIMIT + 1):
            agent._process_tool_calls(
                [_ToolCall("flaky", {"symbol": "AAPL"}, f"a{i}")],
                context,
                messages,
                _Trace(),
                [],
                iteration=i + 1,
            )
        calls_after_first_symbol = registry.calls

        agent._process_tool_calls(
            [_ToolCall("flaky", {"symbol": "MSFT"}, "b0")],
            context,
            messages,
            _Trace(),
            [],
            iteration=99,
        )

        assert registry.calls == calls_after_first_symbol + 1

    def test_a_success_resets_the_counter(self, tmp_path: Path) -> None:
        from src.agent.context import ContextBuilder

        class _FlipRegistry(_AlwaysFailsRegistry):
            def __init__(self) -> None:
                super().__init__()
                self.fail = True

            def execute(self, name: str, args: dict) -> str:
                self.calls += 1
                if self.fail:
                    return json.dumps({"status": "error", "error": "down"})
                return json.dumps({"status": "ok"})

        registry = _FlipRegistry()
        agent = _loop_with(registry, tmp_path)
        context = ContextBuilder(registry, agent.memory)
        messages: list = []

        for i in range(TOOL_CIRCUIT_FAILURE_LIMIT - 1):
            agent._process_tool_calls(
                [_ToolCall("flaky", {"s": "A"}, f"c{i}")],
                context, messages, _Trace(), [], iteration=i + 1,
            )
        registry.fail = False
        agent._process_tool_calls(
            [_ToolCall("flaky", {"s": "A"}, "ok")],
            context, messages, _Trace(), [], iteration=50,
        )

        assert agent._consecutive_failures == {}
