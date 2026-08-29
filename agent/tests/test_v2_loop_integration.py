"""V2 end-to-end wiring inside ``AgentLoop.run`` (stubbed LLM, no network).

Covers the four V2 behaviors that only exist once the loop is actually
running: the empty-response retry, compaction degrading instead of failing the
attempt, the handoff summary being persisted the moment it is produced (and
resumed on the next attempt), and oversized tool results reaching the
trajectory as an explicit preview rather than a silent cut.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import pytest

from src.agent.context_policy import TRUNCATED_TAG
from src.agent.loop import AgentLoop
from src.agent.tool_result_store import TOOL_RESULT_LIMIT
from src.session import handoff


class _Response:
    def __init__(self, content: str = "", tool_calls: list | None = None) -> None:
        self.content = content
        self.tool_calls = tool_calls or []
        self.reasoning_content = None
        self.has_tool_calls = bool(tool_calls)


class _ScriptedLLM:
    """Returns the scripted responses in order, repeating the last one."""

    model_name = "stub"

    def __init__(self, script: list[_Response]) -> None:
        self.script = script
        self.stream_calls = 0
        self.chat_calls = 0

    def stream_chat(
        self,
        messages: list,
        tools: Any = None,
        on_text_chunk: Callable[[str], None] | None = None,
        on_reasoning_chunk: Callable[[str], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> _Response:
        idx = min(self.stream_calls, len(self.script) - 1)
        self.stream_calls += 1
        return self.script[idx]

    def chat(self, messages: list, **_: Any) -> _Response:
        self.chat_calls += 1
        return _Response(content="## Goal\nsummarized")


def _agent(llm: Any, tmp_path: Path, max_iter: int = 4) -> AgentLoop:
    from src.memory.persistent import PersistentMemory
    from src.tools import build_registry

    pm = PersistentMemory(memory_dir=tmp_path / "memory")
    agent = AgentLoop(
        registry=build_registry(persistent_memory=pm, include_shell_tools=False),
        llm=llm,
        max_iterations=max_iter,
        persistent_memory=pm,
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    agent.memory.run_dir = str(run_dir)
    return agent


@pytest.fixture(autouse=True)
def _tenant_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIBE_DATA_DIR", str(tmp_path / "tenant"))


class TestEmptyResponseRetry:
    def test_an_empty_turn_is_retried_once_with_a_nudge(self, tmp_path: Path) -> None:
        """P2-3: a degraded provider turn used to burn a whole attempt."""
        llm = _ScriptedLLM([_Response(""), _Response("recovered answer")])
        agent = _agent(llm, tmp_path)

        result = agent.run(user_message="hi")

        assert llm.stream_calls == 2, "the empty turn must buy exactly one retry"
        assert result["status"] == "success"
        assert result["content"] == "recovered answer"

    def test_a_persistently_empty_provider_still_fails(self, tmp_path: Path) -> None:
        llm = _ScriptedLLM([_Response("")])
        agent = _agent(llm, tmp_path)

        result = agent.run(user_message="hi")

        assert result["status"] == "failed"
        assert result["reason"].startswith("empty_model_response")


class TestCompactionDegrades:
    def test_a_failing_summary_call_does_not_fail_the_run(
        self, tmp_path: Path
    ) -> None:
        """P2-4: compaction is a CORRECTION mechanism — its failure must not
        be the thing that kills an otherwise healthy attempt."""

        class _LLM(_ScriptedLLM):
            def chat(self, messages: list, **_: Any) -> _Response:
                raise RuntimeError("upstream 502 on the summary call")

        llm = _LLM([_Response("final answer")])
        agent = _agent(llm, tmp_path)
        trace = _CollectingTrace(tmp_path)
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "q" * 5000},
            {"role": "assistant", "content": "a" * 5000},
            {"role": "user", "content": "b" * 5000},
        ]
        before = [dict(m) for m in messages]

        agent._auto_compact(messages, tmp_path, trace, iteration=1)

        # Trajectory untouched, failure recorded, no exception escaped.
        assert messages == before
        assert any(e["type"] == "compact_failed" for e in trace.entries)
        assert agent._stats["compact_failures"] == 1

    def test_an_empty_summary_does_not_erase_the_head(self, tmp_path: Path) -> None:
        class _LLM(_ScriptedLLM):
            def chat(self, messages: list, **_: Any) -> _Response:
                return _Response(content="   ")

        agent = _agent(_LLM([_Response("x")]), tmp_path)
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "q" * 5000},
            {"role": "assistant", "content": "a" * 5000},
            {"role": "user", "content": "b" * 5000},
        ]
        before = [dict(m) for m in messages]

        agent._auto_compact(messages, tmp_path, _CollectingTrace(tmp_path), iteration=1)

        assert messages == before


class TestHandoffPersistence:
    def test_a_summary_is_saved_the_moment_it_is_produced(
        self, tmp_path: Path
    ) -> None:
        """Not at run end: the attempt that crashes is the one whose summary
        the NEXT attempt needs."""
        agent = _agent(_ScriptedLLM([_Response("x")]), tmp_path)
        agent._session_id = "sess-1"
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "q" * 5000},
            {"role": "assistant", "content": "a" * 5000},
            {"role": "user", "content": "b" * 5000},
        ]

        agent._auto_compact(messages, tmp_path, _CollectingTrace(tmp_path), iteration=3)

        assert handoff.load("sess-1") == "## Goal\nsummarized"

    def test_the_next_attempt_resumes_from_the_stored_summary(
        self, tmp_path: Path
    ) -> None:
        """Layer 5 continues iteratively instead of restarting from zero."""
        handoff.save("sess-2", "## Goal\ncarried over")
        agent = _agent(_ScriptedLLM([_Response("done")]), tmp_path)

        agent.run(user_message="follow-up", session_id="sess-2")

        assert agent._previous_summary == "## Goal\ncarried over"

    def test_an_unknown_session_starts_empty(self, tmp_path: Path) -> None:
        agent = _agent(_ScriptedLLM([_Response("done")]), tmp_path)
        agent.run(user_message="q", session_id="sess-never-seen")
        assert agent._previous_summary == ""

    def test_the_injected_summary_uses_the_shared_handoff_marker(
        self, tmp_path: Path
    ) -> None:
        """So Layer 2 skips it inside the run — the P1-1 half of this fix."""
        from src.agent.context_policy import HANDOFF_PREFIX

        agent = _agent(_ScriptedLLM([_Response("x")]), tmp_path)
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "q" * 5000},
            {"role": "assistant", "content": "a" * 5000},
            {"role": "user", "content": "b" * 5000},
        ]

        agent._auto_compact(messages, tmp_path, _CollectingTrace(tmp_path), iteration=1)

        assert messages[1]["content"].startswith(HANDOFF_PREFIX)


class TestOversizedToolResultWiring:
    def test_a_large_result_reaches_the_trajectory_as_a_marked_preview(
        self, tmp_path: Path
    ) -> None:
        from src.agent.context import ContextBuilder

        agent = _agent(_ScriptedLLM([_Response("x")]), tmp_path)
        context = ContextBuilder(agent.registry, agent.memory)
        messages: list = []
        raw = json.dumps({"rows": [{"i": i, "v": "x" * 40} for i in range(2000)]})
        assert len(raw) > TOOL_RESULT_LIMIT

        agent._finalize_tool_result(
            _Call("get_market_data", {"symbol": "AAPL"}, "call_abcdef12"),
            raw,
            elapsed_ms=5,
            context=context,
            messages=messages,
            trace=_CollectingTrace(tmp_path),
            react_trace=[],
            iteration=2,
        )

        payload = messages[0]["content"]
        assert payload.startswith(TRUNCATED_TAG)
        assert f'total_chars="{len(raw)}"' in payload
        # The file it points at exists and holds the WHOLE result.
        offloaded = list((Path(agent.memory.run_dir) / "tool-results").iterdir())
        assert len(offloaded) == 1
        assert offloaded[0].read_text(encoding="utf-8") == raw
        assert str(offloaded[0]) in payload

    def test_the_grounding_verifier_still_sees_the_raw_result(
        self, tmp_path: Path
    ) -> None:
        """F1 cross-checks the final answer against what was ACTUALLY fetched —
        it must keep consuming the full text, not the preview."""
        from src.agent.context import ContextBuilder

        agent = _agent(_ScriptedLLM([_Response("x")]), tmp_path)
        context = ContextBuilder(agent.registry, agent.memory)
        raw = json.dumps({"rows": [{"i": i, "v": "x" * 40} for i in range(2000)]})

        agent._finalize_tool_result(
            _Call("get_market_data", {"symbol": "AAPL"}, "c1"),
            raw,
            elapsed_ms=5,
            context=context,
            messages=[],
            trace=_CollectingTrace(tmp_path),
            react_trace=[],
            iteration=1,
        )

        assert agent._grounding_results == [("get_market_data", raw)]

    def test_a_small_result_is_not_offloaded(self, tmp_path: Path) -> None:
        from src.agent.context import ContextBuilder

        agent = _agent(_ScriptedLLM([_Response("x")]), tmp_path)
        context = ContextBuilder(agent.registry, agent.memory)
        messages: list = []

        agent._finalize_tool_result(
            _Call("read_file", {"path": "a"}, "c1"),
            '{"ok": true}',
            elapsed_ms=1,
            context=context,
            messages=messages,
            trace=_CollectingTrace(tmp_path),
            react_trace=[],
            iteration=1,
        )

        assert messages[0]["content"] == '{"ok": true}'
        assert not (Path(agent.memory.run_dir) / "tool-results").exists()


class _Call:
    def __init__(self, name: str, arguments: dict, call_id: str) -> None:
        self.name = name
        self.arguments = arguments
        self.id = call_id


class _CollectingTrace:
    """Trace double. ``dir_path`` must be a temp dir — ``_auto_compact`` writes
    the pre-compression transcript there before it summarizes."""

    def __init__(self, dir_path: Path) -> None:
        self.entries: list[dict] = []
        self.dir_path = dir_path

    def write(self, entry: dict) -> None:
        self.entries.append(entry)

    def write_tool_result(self, **kwargs: Any) -> None:
        self.entries.append({"type": "tool_result", **kwargs})

    def write_text_entry(self, entry: dict, **_: Any) -> None:
        self.entries.append(entry)
