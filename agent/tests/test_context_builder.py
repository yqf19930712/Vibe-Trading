"""Tests for ContextBuilder system-prompt assembly (context-engineering batch E).

E2 — the system prompt must be byte-stable across iterations: no timestamp,
no workspace-state block (both moved to the loop's <agent_status> tail
message). E5 — the ## Tools block is a one-line-per-tool index; full schemas
already ride the API `tools` payload.
"""

from __future__ import annotations

from typing import Any

from src.agent.context import ContextBuilder, _tool_summary
from src.agent.memory import WorkspaceMemory
from src.agent.tools import BaseTool, ToolRegistry


class _DummyTool(BaseTool):
    name = "dummy_tool"
    description = (
        "Run a dummy computation over the workspace. "
        "Second sentence with lots of parameter detail that must not appear."
    )
    parameters = {
        "type": "object",
        "properties": {
            "secret_param_name": {"type": "string", "description": "UNIQUE_PARAM_DESC"},
        },
        "required": ["secret_param_name"],
    }

    def execute(self, **kwargs: Any) -> str:
        return "{}"


def _builder() -> ContextBuilder:
    registry = ToolRegistry()
    registry.register(_DummyTool())
    return ContextBuilder(registry, WorkspaceMemory())


class TestSystemPromptStability:
    def test_no_dynamic_blocks(self) -> None:
        prompt = _builder().build_system_prompt()
        assert "Current Date" not in prompt
        assert "## State" not in prompt
        # The prompt tells the model where the dynamic info now lives
        assert "<agent_status>" in prompt

    def test_byte_identical_across_calls(self) -> None:
        builder = _builder()
        assert builder.build_system_prompt() == builder.build_system_prompt()

    def test_state_updates_do_not_change_prompt(self) -> None:
        builder = _builder()
        before = builder.build_system_prompt()
        builder.memory.increment("dummy_tool")
        builder.memory.run_dir = "/tmp/some_run"
        assert builder.build_system_prompt() == before


class TestToolDescriptions:
    def test_one_line_summary_without_params(self) -> None:
        prompt = _builder().build_system_prompt()
        assert "- dummy_tool — Run a dummy computation over the workspace." in prompt
        # Parameter schemas are no longer duplicated into the prompt (E5)
        assert "secret_param_name" not in prompt
        assert "UNIQUE_PARAM_DESC" not in prompt
        assert "Second sentence" not in prompt


class TestToolSummary:
    def test_first_sentence(self) -> None:
        assert _tool_summary("Do a thing. And more detail.") == "Do a thing."

    def test_chinese_sentence(self) -> None:
        assert _tool_summary("执行回测。参数很多。") == "执行回测。"

    def test_skips_eg_abbreviation(self) -> None:
        text = "Reads a document (e.g. PDF or DOCX) into text. Second sentence."
        assert _tool_summary(text) == "Reads a document (e.g. PDF or DOCX) into text."

    def test_caps_at_100_chars(self) -> None:
        summary = _tool_summary("x" * 500)
        assert len(summary) <= 100
        assert summary.endswith("…")

    def test_collapses_whitespace(self) -> None:
        assert _tool_summary("multi\n  line\tdescription here") == "multi line description here"

    def test_empty(self) -> None:
        assert _tool_summary("") == ""
