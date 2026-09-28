"""A single model call is bounded by the attempt's deadline and cancel.

* ``ChatLLM.stream_chat(timeout=…)`` is a real wall-clock budget: checked per
  chunk, the stream closed and the partial reply returned with
  ``interrupted="deadline"``; a transport error after that instant (the
  tightened SDK read timeout) ends the same way. The SDK request timeout is
  tightened through bound kwargs — a ``RunnableConfig`` ``timeout`` key never
  reached the SDK.
* The main loop stops a turn at the deadline, keeps the streamed text as the
  answer (marked), never starts a turn past it, and never runs a goal
  continuation the budget cannot hold.
* Layer 3 is skipped with under two rounds of budget left; otherwise it goes
  through ``ChatLLM.summarize`` (streamed, cancellable, no SDK retries) with a
  budget that leaves one round for the answer.
* A swarm worker's per-call timeout takes effect: a call cut at the worker's
  deadline ends the worker as ``timeout`` without executing partial tool calls.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessageChunk, HumanMessage

import src.agent.loop as loop_mod
import src.providers.chat as chat_mod
import src.swarm.worker as worker_mod
from src.agent.loop import BUDGET_TRUNCATED_MARK, GOAL_UNFINISHED_MARK, AgentLoop
from src.agent.tools import BaseTool, ToolRegistry
from src.core import budget
from src.providers.chat import ChatLLM, LLMResponse, ProviderStreamError, ToolCallRequest
from src.swarm.models import SwarmAgentSpec, SwarmTask
from src.swarm.worker import run_worker


# ── ChatLLM ─────────────────────────────────────────────────────────────────


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


class _ScriptedStream:
    """``llm.stream`` stand-in: yields chunks at scripted clock instants."""

    def __init__(self, clock: _Clock, steps: list) -> None:
        self.clock = clock
        self.steps = steps
        self.closed = False
        self.stream_kwargs: dict | None = None

    def stream(self, messages, **kwargs):
        self.stream_kwargs = kwargs

        def _gen():
            try:
                for at, item in self.steps:
                    self.clock.now = at
                    if isinstance(item, Exception):
                        raise item
                    yield AIMessageChunk(content=item)
            except GeneratorExit:
                self.closed = True
                raise

        return _gen()

    def bind_tools(self, tools, tool_choice=None):
        return self


def _client(monkeypatch, runnable: Any) -> ChatLLM:
    monkeypatch.setenv("LANGCHAIN_PROVIDER", "openai")
    client = ChatLLM.__new__(ChatLLM)
    client.model_name = "m"
    client._llm = runnable
    return client


def test_stream_stops_at_the_wall_clock_budget(monkeypatch) -> None:
    clock = _Clock()
    monkeypatch.setattr(chat_mod, "time", clock)
    fake = _ScriptedStream(clock, [(1000.0, "A"), (1005.0, "B"), (1011.0, "C"), (1012.0, "D")])
    client = _client(monkeypatch, fake)

    resp = client.stream_chat([{"role": "user", "content": "q"}], timeout=10)

    assert resp.content == "AB"
    assert resp.interrupted == "deadline"
    assert fake.closed is True
    # No RunnableConfig timeout any more (it was filed under ``configurable``).
    assert fake.stream_kwargs == {}


def test_transport_error_after_the_budget_returns_the_partial(monkeypatch) -> None:
    import httpx

    clock = _Clock()
    monkeypatch.setattr(chat_mod, "time", clock)
    fake = _ScriptedStream(clock, [(1000.0, "partial"), (1020.0, httpx.ReadTimeout("slow"))])
    client = _client(monkeypatch, fake)

    resp = client.stream_chat([{"role": "user", "content": "q"}], timeout=10)

    assert resp.content == "partial"
    assert resp.interrupted == "deadline"


def test_transport_error_within_the_budget_still_raises(monkeypatch) -> None:
    import httpx

    clock = _Clock()
    monkeypatch.setattr(chat_mod, "time", clock)
    fake = _ScriptedStream(clock, [(1000.0, "x"), (1002.0, httpx.ReadTimeout("slow"))])
    client = _client(monkeypatch, fake)

    with pytest.raises(ProviderStreamError):
        client.stream_chat([{"role": "user", "content": "q"}], timeout=10)


def test_cancel_marks_the_response(monkeypatch) -> None:
    clock = _Clock()
    monkeypatch.setattr(chat_mod, "time", clock)
    fake = _ScriptedStream(clock, [(1000.0, "A"), (1001.0, "B")])
    client = _client(monkeypatch, fake)
    calls = {"n": 0}

    def _cancel() -> bool:
        calls["n"] += 1
        return calls["n"] > 1

    resp = client.stream_chat([{"role": "user", "content": "q"}], should_cancel=_cancel)
    assert resp.content == "A"
    assert resp.interrupted == "cancelled"


class TestRequestTimeoutReachesTheSdk:
    TOOLS = [{"type": "function", "function": {
        "name": "t", "description": "d",
        "parameters": {"type": "object", "properties": {}},
    }}]

    def test_native_anthropic_payload_carries_the_tightened_timeout(self, monkeypatch) -> None:
        pytest.importorskip("langchain_anthropic")
        from src.providers.llm import _build_native_anthropic

        monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
        monkeypatch.setenv("TIMEOUT_SECONDS", "300")
        monkeypatch.setenv("LANGCHAIN_PROVIDER", "anthropic")
        native = _build_native_anthropic("claude-opus-5")
        client = _client(monkeypatch, native)
        monkeypatch.setenv("LANGCHAIN_PROVIDER", "anthropic")

        bound = chat_mod._with_request_timeout(client._bind(self.TOOLS, None), 42)
        payload = native._get_request_payload([HumanMessage("q")], **bound.kwargs)

        assert payload["timeout"] == 42.0
        assert [t["name"] for t in payload["tools"]] == ["t"]

    def test_openai_compatible_payload_carries_the_tightened_timeout(self, monkeypatch) -> None:
        import src.providers.llm as llm_mod
        from src.providers.llm import build_llm

        monkeypatch.setattr(llm_mod, "_dotenv_loaded", True)
        monkeypatch.setenv("LANGCHAIN_PROVIDER", "openai")
        monkeypatch.setenv("LANGCHAIN_MODEL_NAME", "gpt-x")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("TIMEOUT_SECONDS", "300")
        llm = build_llm()

        bound = chat_mod._with_request_timeout(llm, 17)
        payload = llm._get_request_payload([HumanMessage("q")], **bound.kwargs)

        assert payload["timeout"] == 17.0

    def test_never_loosens_and_skips_runnables_without_bind(self, monkeypatch) -> None:
        monkeypatch.setenv("TIMEOUT_SECONDS", "300")
        marker = object()
        assert chat_mod._with_request_timeout(marker, 5) is marker

        class _Bindable:
            def bind(self, **kwargs):
                raise AssertionError("must not bind")

        runnable = _Bindable()
        assert chat_mod._with_request_timeout(runnable, 600) is runnable
        assert chat_mod._with_request_timeout(runnable, None) is runnable


def test_chat_passes_timeout_as_bound_kwarg_not_config(monkeypatch) -> None:
    from langchain_core.messages import AIMessage

    seen: dict = {}

    class _Runnable:
        def bind(self, **kwargs):
            seen["bind"] = kwargs
            return self

        def invoke(self, messages, **kwargs):
            seen["invoke"] = kwargs
            return AIMessage(content="ok")

    monkeypatch.setenv("TIMEOUT_SECONDS", "300")
    client = _client(monkeypatch, _Runnable())
    assert client.chat([{"role": "user", "content": "q"}], timeout=30).content == "ok"
    assert seen["bind"] == {"timeout": 30.0}
    assert seen["invoke"] == {}


def test_summarize_streams_through_a_no_retry_client(monkeypatch) -> None:
    clock = _Clock()
    monkeypatch.setattr(chat_mod, "time", clock)
    fake = _ScriptedStream(clock, [(1000.0, "## Goal\n"), (1001.0, "done")])
    built: dict = {}

    def _build(**kwargs):
        built.update(kwargs)
        return fake

    monkeypatch.setattr(chat_mod, "build_llm", _build)
    client = _client(monkeypatch, object())

    resp = client.summarize([{"role": "user", "content": "summarise"}], timeout=100)

    assert built["max_retries"] == 0
    assert resp.content == "## Goal\ndone"
    assert resp.interrupted is None


# ── main loop ───────────────────────────────────────────────────────────────


class _Write(BaseTool):
    name = "write_file"
    description = "w"
    is_readonly = False
    parameters = {"type": "object", "properties": {"content": {"type": "string"}}}

    def __init__(self) -> None:
        self.calls = 0

    def execute(self, **kwargs: Any) -> str:
        self.calls += 1
        return json.dumps({"status": "ok"})


class _BudgetLLM:
    model_name = "stub"
    sends_reasoning_content = False

    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = responses
        self.kwargs: list[dict] = []
        self.summaries: list[dict] = []

    def stream_chat(self, messages, tools=None, **kwargs: Any) -> LLMResponse:
        self.kwargs.append(kwargs)
        assert kwargs["should_cancel"]() is False
        return self.responses[min(len(self.kwargs), len(self.responses)) - 1]

    def summarize(self, messages, *, timeout=None, should_cancel=None) -> LLMResponse:
        self.summaries.append({"timeout": timeout, "should_cancel": should_cancel})
        return LLMResponse(content="## Goal\nsummary")


def _loop(llm: Any, tmp_path: Path, tool: BaseTool | None = None, max_iter: int = 4) -> tuple[AgentLoop, list]:
    registry = ToolRegistry()
    if tool is not None:
        registry.register(tool)
    events: list = []
    agent = AgentLoop(registry=registry, llm=llm, max_iterations=max_iter,
                      event_callback=lambda ev, data: events.append((ev, data)))
    run_dir = tmp_path / "run"
    run_dir.mkdir(exist_ok=True)
    agent.memory.run_dir = str(run_dir)
    return agent, events


@pytest.fixture(autouse=True)
def _tenant_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIBE_DATA_DIR", str(tmp_path / "tenant"))


def test_turn_cut_at_deadline_keeps_the_streamed_text(tmp_path: Path) -> None:
    llm = _BudgetLLM([LLMResponse(content="部分答案", interrupted="deadline")])
    agent, events = _loop(llm, tmp_path)

    result = agent.run("q", deadline=time.monotonic() + 600)

    assert result["status"] == "success"
    assert result["content"] == "部分答案" + BUDGET_TRUNCATED_MARK
    assert 0 < llm.kwargs[0]["timeout"] <= 600
    stats = [d for ev, d in events if ev == "attempt_stats"][-1]
    assert stats["budget_truncated"] is True
    trace = [json.loads(line) for line in (tmp_path / "run" / "trace.jsonl").read_text().splitlines()]
    assert any(e.get("type") == "llm_deadline_cut" for e in trace)
    assert any(e.get("type") == "answer" for e in trace)


def _usage_events(events: list) -> list[dict]:
    return [d for ev, d in events if ev == "llm_usage"]


def test_cut_turn_without_usage_bills_an_estimate(tmp_path: Path) -> None:
    """OpenAI-compatible channels send usage in the last chunk, which a cut
    stream never receives: the call is billed from the estimates."""
    from src.core.token_estimate import estimate_text_tokens

    partial = "这是被截断的长答案。" * 200
    llm = _BudgetLLM([LLMResponse(content=partial, interrupted="deadline")])
    agent, events = _loop(llm, tmp_path)

    agent.run("q", deadline=time.monotonic() + 600)

    usage = _usage_events(events)
    assert len(usage) == 1 and usage[0]["estimated"] is True
    assert usage[0]["input_tokens"] > 0
    assert usage[0]["output_tokens"] == estimate_text_tokens(partial)
    stats = [d for ev, d in events if ev == "attempt_stats"][-1]
    assert stats["usage_estimates"] == 1
    assert stats["tokens"]["output"] == usage[0]["output_tokens"]
    ledger = json.loads((tmp_path / "run" / "llm_usage.json").read_text())
    assert ledger["per_iteration"][0]["estimated"] is True
    assert ledger["totals"]["estimated_calls"] == 1


def test_cut_turn_with_only_the_input_count_estimates_the_output(tmp_path: Path) -> None:
    """The native Anthropic channel has message_start's input and nothing else."""
    llm = _BudgetLLM([LLMResponse(
        content="partial answer " * 100, interrupted="deadline",
        usage_metadata={"input_tokens": 30000, "output_tokens": 1, "total_tokens": 30001},
    )])
    agent, events = _loop(llm, tmp_path)

    agent.run("q", deadline=time.monotonic() + 600)

    usage = _usage_events(events)[0]
    assert usage["input_tokens"] == 30000
    assert usage["output_tokens"] > 1
    assert usage["estimated"] is True


def test_complete_usage_on_a_cut_turn_is_billed_as_reported(tmp_path: Path) -> None:
    llm = _BudgetLLM([LLMResponse(
        content="short", interrupted="deadline",
        usage_metadata={"input_tokens": 120, "output_tokens": 900, "total_tokens": 1020},
    )])
    agent, events = _loop(llm, tmp_path)

    agent.run("q", deadline=time.monotonic() + 600)

    usage = _usage_events(events)[0]
    assert (usage["input_tokens"], usage["output_tokens"]) == (120, 900)
    assert "estimated" not in usage
    assert "usage_estimates" not in [d for ev, d in events if ev == "attempt_stats"][-1]


def test_deadline_cut_estimate_counts_tool_arguments_and_the_ratio() -> None:
    from src.core.token_estimate import estimate_text_tokens

    response = LLMResponse(
        content="", interrupted="deadline",
        tool_calls=[ToolCallRequest(id="c", name="write_file", arguments={"content": "x" * 400})],
    )
    usage, estimated = loop_mod._deadline_cut_usage(None, response, 1000, 1.5)

    args = json.dumps({"content": "x" * 400}, ensure_ascii=False)
    assert estimated is True
    assert usage["input_tokens"] == 1500
    assert usage["output_tokens"] == int(estimate_text_tokens(args) * 1.5)
    assert usage["total_tokens"] == usage["input_tokens"] + usage["output_tokens"]


def test_partial_tool_calls_at_deadline_are_not_executed(tmp_path: Path) -> None:
    tool = _Write()
    llm = _BudgetLLM([
        LLMResponse(content="", interrupted="deadline",
                    tool_calls=[ToolCallRequest(id="c", name="write_file", arguments={"content": "hal"})]),
    ])
    agent, _ = _loop(llm, tmp_path, tool)

    result = agent.run("q", deadline=time.monotonic() + 600)

    assert tool.calls == 0
    assert result["status"] == "failed"
    assert result["reason"].startswith("deadline_exhausted")


def test_no_turn_starts_past_the_deadline(tmp_path: Path) -> None:
    llm = _BudgetLLM([LLMResponse(content="never")])
    agent, _ = _loop(llm, tmp_path)

    result = agent.run("q", deadline=time.monotonic() - 1)

    assert llm.kwargs == []
    assert result["status"] == "failed"
    assert result["reason"].startswith("deadline_exhausted")


def test_no_deadline_means_no_timeout_kwarg(tmp_path: Path) -> None:
    llm = _BudgetLLM([LLMResponse(content="ok")])
    agent, _ = _loop(llm, tmp_path)
    previous = budget.get_deadline()
    budget.bind_deadline(None)
    try:
        agent.run("q")
    finally:
        budget.bind_deadline(previous)
    assert "timeout" not in llm.kwargs[0]


def test_goal_continuation_is_skipped_when_the_budget_cannot_hold_it(tmp_path: Path, monkeypatch) -> None:
    from src.goal import GoalStore

    monkeypatch.setattr(loop_mod, "GOAL_MAX_CONTINUATIONS", 3)
    monkeypatch.setenv("VIBE_TRADING_GOAL_DB_PATH", str(tmp_path / "goals.db"))
    monkeypatch.setattr(loop_mod, "SESSIONS_DIR", tmp_path / "sessions")
    GoalStore().replace_goal(
        session_id="s1", objective="Evaluate NVDA.", criteria=["Define thesis", "Check price"],
    )
    llm = _BudgetLLM([LLMResponse(content="interim"), LLMResponse(content="never")])
    agent, _ = _loop(llm, tmp_path, max_iter=4)

    # 30s left < one round's reserve (VIBE_FINALIZE_RESERVE_S=60).
    result = agent.run("start", session_id="s1", deadline=time.monotonic() + 30)

    assert len(llm.kwargs) == 1
    assert result["content"] == "interim" + GOAL_UNFINISHED_MARK


# ── Layer 3 ─────────────────────────────────────────────────────────────────


def _long_trajectory() -> list[dict]:
    msgs: list[dict] = [{"role": "system", "content": "sys"}, {"role": "user", "content": "q"}]
    for i in range(6):
        msgs.append({"role": "assistant", "content": "x" * 30000})
        msgs.append({"role": "user", "content": f"u{i}"})
    return msgs


class _Trace:
    def __init__(self, tmp_path: Path) -> None:
        self.dir_path = tmp_path
        self.events: list[dict] = []

    def write(self, event: dict) -> None:
        self.events.append(event)

    def write_text_entry(self, event: dict, **_: Any) -> None:
        self.events.append(event)


def test_compaction_skipped_with_less_than_two_rounds_left(tmp_path: Path) -> None:
    llm = _BudgetLLM([LLMResponse(content="x")])
    agent, _ = _loop(llm, tmp_path)
    messages = _long_trajectory()
    before = [dict(m) for m in messages]
    trace = _Trace(tmp_path)
    previous = budget.get_deadline()
    budget.bind_deadline(time.monotonic() + 100)  # < 2 × 60s
    try:
        agent._auto_compact(messages, tmp_path, trace, iteration=3)
    finally:
        budget.bind_deadline(previous)

    assert llm.summaries == []
    assert messages == before
    assert agent._stats["compact_skips"] == 1
    assert trace.events[-1]["type"] == "compact_skipped"


def test_compaction_uses_the_streamed_summary_with_a_round_left(tmp_path: Path) -> None:
    llm = _BudgetLLM([LLMResponse(content="x")])
    agent, _ = _loop(llm, tmp_path)
    messages = _long_trajectory()
    previous = budget.get_deadline()
    budget.bind_deadline(time.monotonic() + 1000)
    try:
        agent._auto_compact(messages, tmp_path, _Trace(tmp_path), iteration=3)
    finally:
        budget.bind_deadline(previous)

    [call] = llm.summaries
    assert 900 < call["timeout"] <= 1000 - 60
    assert call["should_cancel"] == agent._cancel_event.is_set
    assert any("summary" in str(m.get("content")) for m in messages)


def test_interrupted_summary_leaves_the_trajectory_alone(tmp_path: Path) -> None:
    class _CutSummary(_BudgetLLM):
        def summarize(self, messages, *, timeout=None, should_cancel=None) -> LLMResponse:
            return LLMResponse(content="## Goal\nhalf", interrupted="deadline")

    llm = _CutSummary([LLMResponse(content="x")])
    agent, _ = _loop(llm, tmp_path)
    messages = _long_trajectory()
    before = [dict(m) for m in messages]
    trace = _Trace(tmp_path)

    agent._auto_compact(messages, tmp_path, trace, iteration=3)

    assert messages == before
    assert agent._stats["compact_failures"] == 1
    assert trace.events[-1]["type"] == "compact_failed"


# ── swarm worker ────────────────────────────────────────────────────────────


def _run_worker(tmp_path: Path, llm: Any, *, timeout_seconds: int = 600, tool: BaseTool | None = None):
    class _Reg:
        def get_definitions(self):
            return []

        def get(self, name):
            return tool if tool is not None and name == tool.name else None

        def execute(self, name, args):
            return tool.execute(**args)

    spec = SwarmAgentSpec(id="a", role="r", system_prompt="s", tools=["write_file"], skills=[],
                          max_iterations=4, timeout_seconds=timeout_seconds)
    task = SwarmTask(id="t", agent_id="a", prompt_template="Do it.")
    events: list = []
    with (
        patch.object(worker_mod, "build_swarm_registry", lambda *a, **k: _Reg()),
        patch.object(worker_mod, "ChatLLM", lambda *a, **k: llm),
    ):
        result = run_worker(agent_spec=spec, task=task, upstream_summaries={}, user_vars={},
                            run_dir=tmp_path, event_callback=events.append)
    return result, events


def test_worker_call_cut_at_its_deadline_ends_as_timeout(tmp_path: Path) -> None:
    tool = _Write()

    class _LLM:
        def __init__(self) -> None:
            self.timeouts: list = []

        def stream_chat(self, messages, tools=None, on_text_chunk=None, timeout=None,
                        should_cancel=None, tool_choice=None) -> LLMResponse:
            self.timeouts.append(timeout)
            return LLMResponse(content="", interrupted="deadline",
                               tool_calls=[ToolCallRequest(id="c", name="write_file",
                                                           arguments={"content": "hal"})])

    llm = _LLM()
    result, events = _run_worker(tmp_path, llm, tool=tool)

    assert result.status == "timeout"
    assert tool.calls == 0
    assert 0 < llm.timeouts[0] <= 600
    assert any(e.type == "worker_timeout" for e in events)


def test_worker_failure_past_its_budget_is_a_timeout(tmp_path: Path) -> None:
    class _LLM:
        def stream_chat(self, messages, tools=None, on_text_chunk=None, timeout=None,
                        should_cancel=None, tool_choice=None) -> LLMResponse:
            time.sleep(1.2)
            raise ProviderStreamError(provider="p", model="m",
                                      original=RuntimeError("read timeout"))

    result, _ = _run_worker(tmp_path, _LLM(), timeout_seconds=1)

    assert result.status == "timeout"


# ── attempt-scoped context ──────────────────────────────────────────────────


def test_run_restores_cancel_deadline_and_collector_bindings(tmp_path: Path) -> None:
    """A cancelled run with an explicit deadline leaves no trace in the
    caller's context — otherwise the next attempt (or test) on this thread
    starts out cancelled, with an expired deadline."""
    from src.core import cancel as cancel_mod
    from src.core import fetch_stats

    llm = _BudgetLLM([LLMResponse(content="x")])
    agent, _ = _loop(llm, tmp_path)
    agent.cancel()
    before = (cancel_mod.get_cancel_event(), budget.get_deadline(), fetch_stats.current())

    result = agent.run("q", deadline=time.monotonic() - 5)

    assert result["status"] == "cancelled"
    assert (cancel_mod.get_cancel_event(), budget.get_deadline(), fetch_stats.current()) == before
    assert cancel_mod.is_cancelled() is False
