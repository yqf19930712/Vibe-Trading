"""A5 + A9 (P1, review 2026-09-04) on ``AgentLoop``.

A5: after Layer-3 compaction the duplicate-call guard (``_called_ok``) must
forget results that were summarised away — otherwise the model is told "use
the result above" about data it can no longer see.

A9: ``attempt_stats`` carries ``compact_failures`` / ``offload_failures``
when (and only when) they are non-zero.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from tests.test_v2_loop_integration import (
    _CollectingTrace,
    _Response,
    _ScriptedLLM,
    _agent,
)


def _tool_msg(call_id: str, content: str) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": call_id, "name": "get_market_data", "content": content}


class TestCalledOkSurvivesOnlyInTail:
    def test_compressed_result_is_forgotten_tail_result_is_kept(self, tmp_path: Path) -> None:
        agent = _agent(_ScriptedLLM([_Response("x")]), tmp_path)
        head_result = _tool_msg("c1", "A" * 5000)
        tail_result = _tool_msg("c2", "B" * 5000)
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function"}]},
            head_result,
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c2", "type": "function"}]},
            tail_result,
        ]
        agent._called_ok = {
            "get_market_data:{\"symbol\":\"A\"}": head_result,
            "get_market_data:{\"symbol\":\"B\"}": tail_result,
        }

        agent._auto_compact(messages, tmp_path, _CollectingTrace(tmp_path), iteration=1)

        # The head was summarised away; the tail survived verbatim.
        assert head_result not in messages
        assert any(m is tail_result for m in messages)
        assert set(agent._called_ok) == {"get_market_data:{\"symbol\":\"B\"}"}
        assert agent._called_ok["get_market_data:{\"symbol\":\"B\"}"] is tail_result

    def test_failed_compaction_leaves_the_guard_untouched(self, tmp_path: Path) -> None:
        class _LLM(_ScriptedLLM):
            def chat(self, messages: list, **_: Any) -> _Response:
                raise RuntimeError("summary call failed")

        agent = _agent(_LLM([_Response("x")]), tmp_path)
        head_result = _tool_msg("c1", "A" * 5000)
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function"}]},
            head_result,
            {"role": "user", "content": "b" * 5000},
        ]
        agent._called_ok = {"k": head_result}

        agent._auto_compact(messages, tmp_path, _CollectingTrace(tmp_path), iteration=1)

        assert agent._called_ok == {"k": head_result}


class TestAttemptStatsDegradationCounters:
    def _stats(self, agent, tmp_path: Path) -> dict[str, Any]:
        emitted: list[tuple[str, dict]] = []
        agent._event_callback = lambda t, d: emitted.append((t, d))
        trace = _CollectingTrace(tmp_path)
        agent._emit_attempt_stats("success", 3, time.perf_counter(), None, trace)
        frames = [d for t, d in emitted if t == "attempt_stats"]
        assert len(frames) == 1
        traced = [e for e in trace.entries if e.get("type") == "attempt_stats"]
        assert len(traced) == 1
        return frames[0]

    def test_counters_present_when_non_zero(self, tmp_path: Path) -> None:
        agent = _agent(_ScriptedLLM([_Response("x")]), tmp_path)
        agent._stats["compact_failures"] = 2
        agent._stats["offload_failures"] = 1

        stats = self._stats(agent, tmp_path)

        assert stats["compact_failures"] == 2
        assert stats["offload_failures"] == 1

    def test_counters_absent_when_zero(self, tmp_path: Path) -> None:
        agent = _agent(_ScriptedLLM([_Response("x")]), tmp_path)

        stats = self._stats(agent, tmp_path)

        assert "compact_failures" not in stats
        assert "offload_failures" not in stats
