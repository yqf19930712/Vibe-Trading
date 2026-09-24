"""Layer 3 never loses the current request.

Shape under test: a War-Room-sized request (≈13.5k CJK characters, the
output contract at its END) followed by several protected ``get_market_data``
results — Layer 1 cannot prune those, so this is the shape that reaches
Layer 3. Before: the request was the oldest head message, the newest-first
summary input skipped it, the rebuilt trajectory was ``system + summary +
tail`` and the Goal of a continued thread stayed on the previous question.

Now: the request is re-inserted right after the summary (verbatim up to
``REQUEST_PIN_MAX_TOKENS``, else beginning + end + transcript pointer), the
summary input reserves budget for it, and the prompt pins ``## Goal`` to it —
also on the iterative path seeded by the previous attempt's handoff summary.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.agent.context_policy import HANDOFF_PREFIX, is_request_message, mark_request_message
from src.agent.loop import (
    REQUEST_PIN_MAX_TOKENS,
    SUMMARY_INPUT_TOKEN_BUDGET,
    AgentLoop,
    _select_summary_input,
)
from src.agent.tools import ToolRegistry
from src.core.token_estimate import estimate_text_tokens
from src.providers.chat import LLMResponse

HEAD_MARK = "【作战计划请求开头】"
CONTRACT = '请按如下 ```json 条目契约输出：action ∈ {buy, sell, hold}，priceLow/priceHigh 为价格区间，分档必须拆多条。【契约结尾】'


def _big_request() -> dict:
    body = HEAD_MARK + ("持仓与市场背景说明，" * 1340) + CONTRACT
    return mark_request_message({"role": "user", "content": body})


def _market_turn(n: int) -> list[dict]:
    rows = ",".join(f'["2026-09-{d:02d}",{100 + d}.5,{101 + d}.2,{99 + d}.1,{100 + d}.9,{1000 + d}]'
                    for d in range(1, 29))
    payload = '{"summary":{"rows":120},"columns":["date","o","h","l","c","v"],"rows":[' + rows + "]}"
    call = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": f"c{n}", "type": "function",
                        "function": {"name": "get_market_data",
                                     "arguments": json.dumps({"codes": f"60051{n}.SH"})}}],
    }
    result = {"role": "tool", "tool_call_id": f"c{n}", "name": "get_market_data",
              "content": payload * 6}
    return [call, result]


def _trajectory(request: dict, turns: int = 6) -> list[dict]:
    msgs: list[dict] = [{"role": "system", "content": "SYSTEM"}, request]
    for n in range(turns):
        msgs.extend(_market_turn(n))
    return msgs


class _SummaryLLM:
    model_name = "stub"
    sends_reasoning_content = False

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def stream_chat(self, *a: Any, **k: Any) -> LLMResponse:  # pragma: no cover - unused
        raise AssertionError("not used")

    def chat(self, messages, **_: Any) -> LLMResponse:
        self.prompts.append(messages[0]["content"])
        return LLMResponse(content="## Goal\n执行作战计划\n## Pending User Asks\n- 输出 JSON 条目")


class _Trace:
    def __init__(self, tmp_path: Path) -> None:
        self.dir_path = tmp_path
        self.events: list[dict] = []

    def write(self, event: dict) -> None:
        self.events.append(event)

    def write_text_entry(self, event: dict, **_: Any) -> None:
        self.events.append(event)


def _agent(llm: Any) -> AgentLoop:
    return AgentLoop(registry=ToolRegistry(), llm=llm, max_iterations=4)


def test_large_request_survives_compaction_with_its_contract(tmp_path: Path) -> None:
    request = _big_request()
    assert estimate_text_tokens(request["content"]) > REQUEST_PIN_MAX_TOKENS
    messages = _trajectory(request)
    llm = _SummaryLLM()

    _agent(llm)._auto_compact(messages, tmp_path, _Trace(tmp_path), iteration=7)

    assert messages[1]["content"].startswith(HANDOFF_PREFIX)
    pinned = messages[2]
    assert is_request_message(pinned)
    assert pinned["content"].startswith(HEAD_MARK)
    assert pinned["content"].endswith(CONTRACT)
    assert "pre-compaction transcript" in pinned["content"]
    assert estimate_text_tokens(pinned["content"]) <= REQUEST_PIN_MAX_TOKENS + 100
    assert sum(is_request_message(m) for m in messages) == 1

    # The summariser saw the request (reserved budget) and the Goal rule.
    [prompt] = llm.prompts
    assert HEAD_MARK in prompt and "【契约结尾】" in prompt
    assert '"vibe_class": "request"' in prompt
    assert "the ## Goal section" in prompt


def test_small_request_is_reinserted_verbatim(tmp_path: Path) -> None:
    request = mark_request_message({"role": "user", "content": "分析贵州茅台的估值" + CONTRACT})
    messages = _trajectory(request, turns=10)

    _agent(_SummaryLLM())._auto_compact(messages, tmp_path, _Trace(tmp_path), iteration=7)

    assert messages[2] is request


def test_request_in_the_tail_is_quoted_not_duplicated(tmp_path: Path) -> None:
    """A continued thread: old replayed turns in the head, the request recent."""
    request = mark_request_message({"role": "user", "content": "那第一笔怎么操作？"})
    old_turns = []
    for n in range(8):
        old_turns.append({"role": "user", "content": f"旧问题 {n}"})
        old_turns.append({"role": "assistant", "content": "旧回答" * 3000})
    messages = [{"role": "system", "content": "S"}, *old_turns, request, *_market_turn(0)]
    llm = _SummaryLLM()

    _agent(llm)._auto_compact(messages, tmp_path, _Trace(tmp_path), iteration=7)

    assert sum(is_request_message(m) for m in messages) == 1
    [prompt] = llm.prompts
    assert "<current-request>\n那第一笔怎么操作？\n</current-request>" in prompt


def test_handoff_seeded_first_compaction_pins_goal_to_this_request(tmp_path: Path) -> None:
    llm = _SummaryLLM()
    agent = _agent(llm)
    agent._previous_summary = "## Goal\n上一问：评估宁德时代"
    messages = _trajectory(_big_request())

    agent._auto_compact(messages, tmp_path, _Trace(tmp_path), iteration=7)

    [prompt] = llm.prompts
    assert "PREVIOUS SUMMARY" in prompt and "上一问：评估宁德时代" in prompt
    assert "CURRENT REQUEST" in prompt
    assert "earlier topic" in prompt


def test_summary_input_reserves_the_request_before_newer_messages() -> None:
    request = _big_request()
    others = []
    for n in range(12):
        others.extend(_market_turn(n))
    text, dropped = _select_summary_input([request, *others])

    assert HEAD_MARK in text and "【契约结尾】" in text
    assert dropped > 0  # older market turns made room, the request did not
    # Newest turn is in, and the request stays first (chronological order).
    assert '"c11"' in text
    assert text.index(HEAD_MARK) < text.index('"c11"')
    assert estimate_text_tokens(text) <= SUMMARY_INPUT_TOKEN_BUDGET + 200


@pytest.mark.parametrize("size", [10, 100])
def test_summary_input_without_request_is_unchanged(size: int) -> None:
    msgs = [{"role": "user", "content": f"m{i}"} for i in range(size)]
    text, dropped = _select_summary_input(msgs)
    assert dropped == 0
    assert json.loads(text) == msgs
