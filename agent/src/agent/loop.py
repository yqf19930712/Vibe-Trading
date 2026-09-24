"""AgentLoop: ReAct core loop.

Five-layer context management:
  Layer 1 (microcompact)     — prunes old tool results once the context passes
                               a token threshold (keeps a recency token budget;
                               grounding/deliverable tools are never pruned)
  Layer 2 (context_collapse) — folds long text blocks without LLM call (zero cost)
  Layer 3 (auto_compact)     — LLM structured summary with token-budget tail protection
  Layer 4 (compact tool)     — model explicitly calls the compact tool to trigger L3
  Layer 5 (iterative update) — Nth compression updates previous summary instead of starting fresh

Tool execution:
  - Read/write batching: consecutive readonly tools run in parallel via threads
"""

from __future__ import annotations

import concurrent.futures
import contextvars
import hashlib
import json
import logging
import os
import queue
import threading
import time as _time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from src.agent.context import ContextBuilder
from src.agent.context_policy import (
    CLEARED_PREFIX,
    HANDOFF_PREFIX,
    PROTECTED_TOOLS,
    STATUS_PREFIX,
    collapse_rule,
    first_user_index,
    is_prunable_by_microcompact,
)
from src.agent.memory import WorkspaceMemory
from src.agent.progress import HeartbeatTimer, ProgressEvent, _set_emitter
from src.agent.tool_result_store import TOOL_RESULT_LIMIT, prepare_for_context
from src.agent.tools import ToolRegistry
from src.agent.trace import TraceWriter
from src.agent.verify import verify_run
from src.core.state import RunStateStore
from src.goal.context import (
    format_goal_continuation_prompt,
    get_current_goal_context,
    goal_needs_continuation,
    goal_progress_tuple,
)
from src.providers.chat import TOOL_CHOICE_NONE, ChatLLM, ProviderStreamError
from src.session import handoff
from src.tools.background_tools import get_background_manager
from src.tools.redaction import redact_payload, redact_secret_values
from src.core import budget as _budget
from src.core import cancel as _cancel
from src.core import fetch_stats as _fetch_stats
from src.core.paths import data_root, runs_root
from src.core.token_estimate import (
    estimate_messages_tokens,
    estimate_text_tokens,
    messages_for_estimate,
)

# Honor VIBE_DATA_DIR (multi-tenant per-user HOME) so the agent loop writes run
# artifacts under the tenant root, not the shared install dir. See core/paths.py.
RUNS_DIR = runs_root()
SESSIONS_DIR = data_root() / "sessions"
TOKEN_THRESHOLD = int(os.getenv("TOKEN_THRESHOLD", "40000"))
# Layer 1 (microcompact) tuning. The old behavior — unconditionally pruning
# every tool result older than the last 3, every iteration — was a sliding
# window anti-pattern: the model kept re-fetching data the loop had just
# thrown away (see the dea1222743ef notes below), and rewriting the middle of
# the trajectory each turn invalidated the provider prompt cache wholesale.
# Now pruning only triggers past a token threshold, and retention is a token
# budget instead of a fixed count.
MICROCOMPACT_TRIGGER_RATIO = 0.5   # prune only when > TOKEN_THRESHOLD * ratio
MICROCOMPACT_KEEP_BUDGET_RATIO = 0.25  # keep newest tool results up to this budget
# Hysteresis. With a single trigger line, every iteration past it would
# recompute the keep set and the newest results push one or two older ones out
# of the budget — the middle of the trajectory then changes EVERY turn and the
# provider prompt cache rebuilds from that diff point each time.
# Instead: crossing the trigger arms the layer and cuts once, deeper; the layer
# stays armed (still cutting to the deep water mark) until the estimate falls
# back below the release line, at which point the trajectory is left alone for
# many turns and the cache stays hot. Book §2.7.3 "compact in batches near the
# threshold, not every turn".
MICROCOMPACT_RELEASE_RATIO = 0.35  # disarm once the estimate falls back here
MICROCOMPACT_ARMED_KEEP_RATIO = 0.15  # deeper cut while armed
KEEP_RECENT = 3  # hard floor: newest N tool results are always kept intact
# Tool results that are never pruned by microcompact. Rationale: these carry
# the run's grounding data or its key deliverables — re-fetching them is
# either expensive (backtest / factor_analysis / options_pricing recompute,
# run_swarm re-runs a whole multi-agent team for tens of minutes) or defeats
# the anti-hallucination grounding (get_market_data / get_realtime_quotes are
# the live-price sources every cited number must trace back to). Layer 2/3
# can still fold/summarize them when the context truly overflows.
#
# The set itself lives in ``src.agent.context_policy`` so Layer 2 obeys it too
# (otherwise it would fold the middle out of exactly these results). This name
# is kept as an alias for existing call sites and tests.
MICROCOMPACT_PROTECTED_TOOLS = PROTECTED_TOOLS
# Re-exported for callers that know this constant as a loop-module name; it
# (and the offload path that enforces it) lives in tool_result_store.
__all_reexports__ = ("TOOL_RESULT_LIMIT",)
# Successful results from these tools are kept (raw) for the
# zero-LLM finalization verification — the final answer's price claims are
# cross-checked against what the run actually fetched (see src/agent/verify.py).
VERIFY_GROUNDING_TOOLS = frozenset({"get_market_data", "get_realtime_quotes"})
VERIFY_GROUNDING_MAX_RESULTS = 40
HEARTBEAT_INTERVAL_S = float(os.getenv("VT_HEARTBEAT_INTERVAL_S", "3.0"))
REASONING_DELTA_MIN_INTERVAL_S = float(os.getenv("VT_REASONING_DELTA_MIN_INTERVAL_S", "1.0"))
STREAM_RETRY_DELAY_S = float(os.getenv("VT_STREAM_RETRY_DELAY_S", "2.0"))
# In-place retries after the initial attempt (N+1 attempts total, exponential
# backoff base×4^i capped at 60s). Same rationale as the swarm worker: upstream
# proxies drop long opus streams in bursts; a single immediate retry lands
# inside the same burst and kills the whole attempt.
STREAM_RETRIES = max(0, int(os.getenv("VT_STREAM_RETRIES", "3")))
STREAM_RETRY_MAX_DELAY_S = 60.0
TOOL_TIMEOUT_SECONDS = float(os.getenv("VIBE_TRADING_TOOL_TIMEOUT_SECONDS", "1800"))
# Write tools are not "never killed": a watchdog that only warns and then waits
# forever lets one hung write tool eat the whole attempt budget and defeat the
# FINALIZE_RESERVE partial-answer path. They get a grace window of this factor × the per-call (budget-capped) timeout:
# warn at 1×, abandon waiting at 2×. Abandoning marks the run degraded, returns
# a structured timeout error to the model, and discards the late result via the
# same queue mechanism the readonly path uses (the worker thread may still
# finish its side effect in the background — that is announced in the error).
#
# The base of that 1×/2× window is per-tool (``_tool_timeout``), not the
# tenant-wide constant: pinned to TOOL_TIMEOUT_SECONDS the watchdog would fire
# at 600s on a run_swarm whose own wait budget is 7200s, making the two-hour
# swarm tier unreachable and the ``wait_budget_exhausted`` salvage path (which
# is what carries the run_id back) dead code.
WRITE_TOOL_TIMEOUT_FACTOR = 2.0
# When an attempt deadline is bound, force the final text answer once
# less than this many seconds (or ~1.2 avg iterations) remain — a partial
# answer beats the caller timing out on nothing.
FINALIZE_RESERVE_S = float(os.getenv("VIBE_FINALIZE_RESERVE_S", "60"))
# Seconds held back when clamping a tool timeout to the attempt budget. Keep
# at least as much back as the forced-finalize path needs — a reserve shorter
# than FINALIZE_RESERVE_S lets abandoning a tool leave the loop with less time
# than the forced-finalize path requires, and the "a partial answer beats a
# timeout" guarantee becomes nominal.
_TOOL_CAP_RESERVE_S = max(45.0, FINALIZE_RESERVE_S)
# Minimum window a tool gets even on a nearly-spent budget, so a late call
# still gets one quick shot instead of an instant failure. Named constants so
# the nesting/clamp regressions can scale them, and so the overshoot they permit
# (up to floor + grace floor past the deadline) is visible in one place.
_TOOL_CAP_FLOOR_S = 10.0
_TOOL_GRACE_FLOOR_S = 5.0
GOAL_MAX_CONTINUATIONS = int(os.getenv("VIBE_TRADING_GOAL_MAX_CONTINUATIONS", "3"))
# Consecutive failures of the SAME (tool, args) pair before the call is
# refused outright. Keyed identically to the duplicate guard, which only ever
# registered successes — so a dead upstream could burn 40+ iterations of LLM
# spend before max_iterations stopped it.
TOOL_CIRCUIT_FAILURE_LIMIT = max(
    1, int(os.getenv("VIBE_TOOL_CIRCUIT_FAILURE_LIMIT", "3"))
)
# In-place retries for a stream that SUCCEEDS but returns neither text nor
# tool calls (relay truncation, upstream degraded empty turn). The transport
# layer only retries transport failures; without this a degenerate provider
# response would fail a possibly hour-long attempt without a single retry.
EMPTY_RESPONSE_RETRIES = max(0, int(os.getenv("VIBE_EMPTY_RESPONSE_RETRIES", "1")))
_EMPTY_RESPONSE_NUDGE = (
    "[SYSTEM] Your previous turn returned no content and no tool calls. "
    "Respond now: either call a tool, or write your answer as text."
)
LLM_USAGE_ARTIFACT = "llm_usage.json"

# A reply cut by the output-token ceiling (``finish_reason == "length"``,
# Anthropic ``stop_reason == "max_tokens"``) is not a final answer: the loop
# appends the partial text and asks the model to continue from where it
# stopped, up to this many times per attempt; a still-truncated reply, or one
# truncated on the last turn, is delivered with an explicit marker instead
# of passing as complete.
LENGTH_CONTINUATIONS = max(0, int(os.getenv("VIBE_LENGTH_CONTINUATIONS", "2")))
_LENGTH_CONTINUE_NUDGE = (
    "[SYSTEM] Your previous reply was cut off by the output length limit "
    "(finish_reason=length). Continue EXACTLY from where it stopped: do not "
    "repeat what you already wrote and do not restart the document. Be concise "
    "in the remaining part."
)
OUTPUT_TRUNCATED_MARK = "\n\n（输出被截断）"

# A tool call whose arguments were still streaming when the output ceiling
# hit is NOT executed: LangChain completes the cut JSON (``parse_partial_json``)
# into a valid-looking dict, so a half-written file or script would otherwise
# land on disk and report ``ok``. Each such call gets this structured error
# instead, and the turn counts against ``LENGTH_CONTINUATIONS``. The refused
# call stays in the trajectory (tool_use / tool_result pairing), with long
# string arguments shortened so the partial payload does not bloat it.
TRUNCATED_TOOL_CALL_ERROR = "tool_call_truncated"
_TRUNCATED_ARG_MAX_CHARS = 2000
_TRUNCATED_ARG_HEAD = 1500
_TRUNCATED_ARG_TAIL = 500


def _truncated_tool_call_error(tool_name: str) -> str:
    """Structured tool result for a call cut by the output-token ceiling."""
    return json.dumps(
        {
            "status": "error",
            "error_code": TRUNCATED_TOOL_CALL_ERROR,
            "tool": tool_name,
            "message": (
                "This call was NOT executed: your reply hit the output token "
                "limit (finish_reason=length) while its arguments were still "
                "being written, so they are incomplete. Re-issue it with "
                "shorter arguments — split long content into several smaller "
                "calls (write a long file in parts, keep scripts short) and "
                "keep the text before the call brief."
            ),
        },
        ensure_ascii=False,
    )


def _shorten_truncated_value(value: Any) -> Any:
    """Shorten long strings inside a refused call's arguments (recursive)."""
    if isinstance(value, str):
        if len(value) <= _TRUNCATED_ARG_MAX_CHARS:
            return value
        omitted = len(value) - _TRUNCATED_ARG_HEAD - _TRUNCATED_ARG_TAIL
        return (
            f"{value[:_TRUNCATED_ARG_HEAD]}\n...[{omitted} chars omitted — "
            f"truncated call, not executed]...\n{value[-_TRUNCATED_ARG_TAIL:]}"
        )
    if isinstance(value, dict):
        return {k: _shorten_truncated_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_shorten_truncated_value(v) for v in value]
    return value


class _RefusedToolCall:
    """Tool-call view with shortened arguments, for the trajectory only."""

    def __init__(self, tc: Any) -> None:
        self.id = tc.id
        self.name = tc.name
        self.arguments = _shorten_truncated_value(tc.arguments)
        self.thought_signature = getattr(tc, "thought_signature", None)


def truncated_tool_call_messages(
    tool_calls: list,
    *,
    content: Optional[str],
    reasoning_content: Optional[str],
) -> list[dict[str, Any]]:
    """Trajectory messages for a turn whose tool calls were cut by the ceiling.

    Shared by the main loop and the swarm worker.

    Args:
        tool_calls: The (truncated) tool calls of the turn — none is executed.
        content: The turn's visible text.
        reasoning_content: The channel's reasoning field, as the caller would
            pass it for an executed turn.

    Returns:
        The assistant tool-call message followed by one structured
        ``tool_call_truncated`` error result per call.
    """
    assistant = ContextBuilder.format_assistant_tool_calls(
        [_RefusedToolCall(tc) for tc in tool_calls],
        content=content,
        reasoning_content=reasoning_content,
    )
    _attach_tool_call_thought_signatures(assistant, tool_calls)
    return [assistant] + [
        ContextBuilder.format_tool_result(tc.id, tc.name, _truncated_tool_call_error(tc.name))
        for tc in tool_calls
    ]

# Layer 2: Context collapse thresholds
COLLAPSE_THRESHOLD = int(TOKEN_THRESHOLD * 0.7)
COLLAPSE_PRESERVE_RECENT = 6
COLLAPSE_TEXT_MIN = 2400
COLLAPSE_HEAD = 900
COLLAPSE_TAIL = 500

# Layer 3: Token-budget tail protection
TAIL_TOKEN_BUDGET = 20_000
# Layer 3 summary INPUT budget (V2). The old ``json.dumps(head)[:80000]`` cut
# from the tail, i.e. it discarded the newest and densest turns in the head —
# and 80k ASCII chars is only ~20k tokens, so an English-heavy session hit it
# almost every time. Now the input is filled newest-first against a token
# budget and the OLDEST turns are the ones dropped (they are already covered
# by the previous summary and by the full transcript on disk).
SUMMARY_INPUT_TOKEN_BUDGET = int(TOKEN_THRESHOLD * 0.5)

logger = logging.getLogger(__name__)


def _coerce_usage_int(value: Any) -> int:
    """Coerce provider token counts to non-negative ints."""
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


# Prompt-cache counters carried through from LangChain's
# ``usage_metadata.input_token_details`` (both already INCLUDED in
# ``input_tokens`` — they break it down, they do not add to it). Output keys
# are the names the ``llm_usage`` event / ``llm_usage.json`` / attempt_stats
# use; each appears only when non-zero so channels without caching keep the
# original three-field shape.
_CACHE_USAGE_FIELDS = (
    ("cache_read", "cache_read_tokens"),
    ("cache_creation", "cache_creation_tokens"),
)
_CACHE_CREATION_TTL_KEYS = ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens")


def _normalize_llm_usage(usage: Any) -> dict[str, int] | None:
    """Normalize provider-reported usage metadata without estimating tokens."""
    if usage is None:
        return None
    if not isinstance(usage, dict):
        try:
            usage = dict(usage)
        except (TypeError, ValueError):
            return None

    input_tokens = _coerce_usage_int(usage.get("input_tokens"))
    output_tokens = _coerce_usage_int(usage.get("output_tokens"))
    total_tokens = _coerce_usage_int(usage.get("total_tokens"))
    if total_tokens == 0 and (input_tokens or output_tokens):
        total_tokens = input_tokens + output_tokens
    if not (input_tokens or output_tokens or total_tokens):
        return None
    normalized = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }
    details = usage.get("input_token_details")
    if isinstance(details, dict):
        for source_key, out_key in _CACHE_USAGE_FIELDS:
            value = _coerce_usage_int(details.get(source_key))
            if not value and source_key == "cache_creation":
                # langchain-anthropic zeroes the generic key when the API
                # reports the per-TTL split; the split then carries the count.
                value = sum(
                    _coerce_usage_int(details.get(key)) for key in _CACHE_CREATION_TTL_KEYS
                )
            if value:
                normalized[out_key] = value
    return normalized


def _new_llm_usage_summary(llm: Any) -> dict[str, Any]:
    """Create the run-scoped provider usage accumulator."""
    provider = os.getenv("LANGCHAIN_PROVIDER", "openai").strip() or "openai"
    model = getattr(llm, "model_name", None) or os.getenv("LANGCHAIN_MODEL_NAME", "").strip()
    return {
        "provider": provider,
        "model": model,
        "totals": {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "calls": 0,
        },
        "per_iteration": [],
    }


def _record_llm_usage(
    run_dir: Path,
    summary: dict[str, Any],
    usage: Any,
    iteration: int,
) -> dict[str, int] | None:
    """Accumulate and persist one provider-reported usage event."""
    normalized = _normalize_llm_usage(usage)
    if normalized is None:
        return None

    totals = summary.setdefault("totals", {})
    totals["input_tokens"] = int(totals.get("input_tokens") or 0) + normalized["input_tokens"]
    totals["output_tokens"] = int(totals.get("output_tokens") or 0) + normalized["output_tokens"]
    totals["total_tokens"] = int(totals.get("total_tokens") or 0) + normalized["total_tokens"]
    totals["calls"] = int(totals.get("calls") or 0) + 1
    for _source_key, out_key in _CACHE_USAGE_FIELDS:
        if normalized.get(out_key):
            totals[out_key] = int(totals.get(out_key) or 0) + normalized[out_key]
    summary.setdefault("per_iteration", []).append({"iter": iteration, **normalized})
    summary["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    try:
        path = run_dir / LLM_USAGE_ARTIFACT
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        tmp_path.replace(path)
    except OSError as exc:
        logger.debug("LLM usage artifact write skipped: %s", exc)

    return normalized


def _redact_trace_result(result: str) -> str:
    """Redact structured sensitive fields before persisting trace/event previews.

    Args:
        result: Raw tool result string.

    Returns:
        Redacted JSON string when ``result`` is JSON, otherwise the original
        text. Plain text is left unchanged because reliable free-text secret
        scrubbing would be more error-prone than helpful here.
    """
    try:
        payload = json.loads(result)
    except (TypeError, json.JSONDecodeError):
        return result
    return json.dumps(redact_payload(payload), ensure_ascii=False)


def _best_effort(fn: Any, *args: Any, **kwargs: Any) -> None:
    """Call ``fn`` and swallow any exception (debug-logged).

    For the trace / state writes on a failure path: they run when the disk
    may already be the problem, and a second exception there would replace
    the ``failed`` result with a bare crash.
    """
    try:
        fn(*args, **kwargs)
    except Exception:  # noqa: BLE001 - failure-path bookkeeping never re-raises
        logger.debug("best-effort call %s failed", getattr(fn, "__name__", fn), exc_info=True)


def _new_run_stats() -> dict[str, Any]:
    """Per-run accumulator behind the ``attempt_stats`` summary event."""
    return {"llm_calls": 0, "llm_ms": 0, "compact_calls": 0, "tools": {}}


def _format_timeout(seconds: float) -> str:
    """Return a human-readable timeout label."""
    if seconds < 1:
        return f"{seconds:.2f}s"
    return f"{seconds:.0f}s"


def estimate_tokens(messages: list, *, count_reasoning: bool = False) -> int:
    """Rough token count estimate, weighted by character class.

    ASCII ~4 chars/token, CJK ~0.6 token/char, other ~3 chars/token — see
    :mod:`src.core.token_estimate`. The old flat ``// 4`` heuristic assumed
    English and under-estimated Chinese contexts 2-3x, so compaction fired
    far too late for CJK-heavy sessions.

    Args:
        messages: Message list.
        count_reasoning: Include assistant ``reasoning_content`` — only when
            the provider sends it back upstream (``ChatLLM.sends_reasoning_content``).

    Returns:
        Estimated token count.
    """
    return estimate_messages_tokens(messages, count_reasoning=count_reasoning)


# Placeholder for pruned tool results. MUST tell the model the data was
# dropped and can be re-fetched — the bare "[cleared]" marker plus the
# name-level duplicate guard can dead-lock an attempt into retracting REAL
# numbers as hallucinations (result pruned, every re-fetch refused with
# "already succeeded").
# Aliased from context_policy (the shared marker registry) so the duplicate
# guard's "was this result pruned?" test and Layer 2's skip rule can never
# disagree about what a cleared placeholder looks like.
_CLEARED_PREFIX = CLEARED_PREFIX
_CLEARED_PLACEHOLDER = (
    "[cleared — this old tool result was pruned to keep the context small. "
    "The call DID succeed earlier; if you need the data again, re-call the "
    "tool with the same arguments.]"
)


def _tool_call_key(name: str, arguments: Any) -> str:
    """Duplicate-guard key: tool name + canonicalized arguments."""
    try:
        args_repr = json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        args_repr = repr(arguments)
    return f"{name}:{hashlib.sha1(args_repr.encode('utf-8', 'replace')).hexdigest()}"


def _microcompact(
    messages: list,
    token_threshold: int = TOKEN_THRESHOLD,
    state: dict | None = None,
    *,
    count_reasoning: bool = False,
) -> None:
    """Layer 1: prune old tool results — threshold-triggered, token-budget keep.

    Trigger: only runs when the estimated context exceeds
    ``token_threshold * MICROCOMPACT_TRIGGER_RATIO``; below that the trajectory
    is left byte-identical so the provider prompt cache stays warm.

    Hysteresis (V2): crossing the trigger *arms* the layer, which then cuts to
    the deeper ``MICROCOMPACT_ARMED_KEEP_RATIO`` water mark and stays armed
    until the estimate falls back under ``MICROCOMPACT_RELEASE_RATIO``. One
    deep cut every so often replaces a shallow cut every single turn, which is
    what kept invalidating the provider cache mid-trajectory.

    Retention: walks tool results newest→oldest, keeping them intact until the
    accumulated estimate reaches the keep budget (and always at least the
    newest ``KEEP_RECENT``, matching the old floor). Older results are replaced
    with the informative cleared placeholder — except results from
    ``MICROCOMPACT_PROTECTED_TOOLS`` (grounding data and expensive key
    deliverables), which are never pruned here.

    Args:
        messages: Message list (mutated in place).
        token_threshold: Context budget this trajectory is managed against
            (the main loop's ``TOKEN_THRESHOLD``; swarm workers pass their own
            ``_MAX_TOKEN_ESTIMATE``).
        state: Caller-owned dict carrying the armed flag across iterations.
            Omitted (None) reproduces the pre-V2 single-line behavior, so the
            function stays usable stateless.
        count_reasoning: See :func:`estimate_tokens`.
    """
    estimate = estimate_tokens(messages, count_reasoning=count_reasoning)
    keep_ratio = MICROCOMPACT_KEEP_BUDGET_RATIO
    if state is None:
        if estimate <= token_threshold * MICROCOMPACT_TRIGGER_RATIO:
            return
    elif state.get("armed"):
        if estimate <= token_threshold * MICROCOMPACT_RELEASE_RATIO:
            state["armed"] = False
            return
        keep_ratio = MICROCOMPACT_ARMED_KEEP_RATIO
    else:
        if estimate <= token_threshold * MICROCOMPACT_TRIGGER_RATIO:
            return
        state["armed"] = True
        keep_ratio = MICROCOMPACT_ARMED_KEEP_RATIO

    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    if len(tool_msgs) <= KEEP_RECENT:
        return

    keep_budget = token_threshold * keep_ratio
    keep_ids: set[int] = set()
    accumulated = 0
    for msg in reversed(tool_msgs):
        cost = estimate_text_tokens(str(msg.get("content", "")))
        if len(keep_ids) < KEEP_RECENT:
            keep_ids.add(id(msg))
            accumulated += cost
            continue
        if accumulated + cost > keep_budget:
            break
        keep_ids.add(id(msg))
        accumulated += cost

    for msg in tool_msgs:
        if id(msg) in keep_ids:
            continue
        if not is_prunable_by_microcompact(msg):
            continue
        content = msg.get("content", "")
        if isinstance(content, str) and len(content) > 100:
            msg["content"] = _CLEARED_PLACEHOLDER


# Dynamic status bar. A minute-level timestamp or the WorkspaceMemory "## State"
# block embedded in the system prompt changes between turns, so the very first
# bytes of the context diverge every iteration and the provider prompt cache
# never hits. That dynamic information instead rides a single ephemeral ``<agent_status>`` user message appended to the END of the
# trajectory each iteration (the previous one is removed first — "use and
# discard"), together with any budget / wrap-up nudge lines. The system
# prompt itself is byte-stable for the whole session.
_STATUS_PREFIX = STATUS_PREFIX


def _remove_status_messages(messages: list) -> None:
    """Drop previous ``<agent_status>`` user messages (mutates in place)."""
    i = 0
    while i < len(messages):
        msg = messages[i]
        content = msg.get("content")
        if (
            msg.get("role") == "user"
            and isinstance(content, str)
            and content.startswith(_STATUS_PREFIX)
        ):
            messages.pop(i)
        else:
            i += 1


def _build_status_message(state_summary: str, nudge_lines: list[str]) -> dict[str, Any]:
    """Build the per-iteration status-bar user message.

    Args:
        state_summary: ``WorkspaceMemory.to_summary()`` output.
        nudge_lines: Optional ``[SYSTEM]`` nudge lines (budget / wrap-up),
            folded into the same message so the trajectory gains at most one
            transient message per iteration.

    Returns:
        OpenAI-format user message dict.
    """
    now_iso = datetime.now().astimezone().isoformat(timespec="seconds")
    content = (
        f"{_STATUS_PREFIX}\n"
        f"Now: {now_iso}\n"
        f"State: {state_summary}\n"
        "</agent_status>"
    )
    if nudge_lines:
        content += "\n\n" + "\n\n".join(nudge_lines)
    return {"role": "user", "content": content}


def _context_collapse(messages: list) -> None:
    """Layer 2: fold long text blocks in older messages without LLM call.

    Preserves head + tail of large text, collapses the middle.
    Zero API cost — pure string operation.

    Which messages may be folded, and how hard, comes from
    ``src.agent.context_policy`` — the single rule source Layers 1 and 3 also
    read, so this layer never cuts the middle out of the grounding results
    Layer 1 refuses to prune or out of the Layer 3 handoff summary.

    Args:
        messages: Message list (mutated in place).
    """
    if len(messages) <= COLLAPSE_PRESERVE_RECENT + 1:
        return
    fu_index = first_user_index(messages)
    stop = len(messages) - COLLAPSE_PRESERVE_RECENT
    for idx in range(1, stop):
        msg = messages[idx]
        rule = collapse_rule(msg, index=idx, first_user_index=fu_index)
        if rule.skip:
            continue
        content = msg.get("content")
        if not isinstance(content, str) or len(content) <= rule.min_chars:
            continue
        head = content[: rule.head]
        tail = content[-rule.tail:]
        trimmed = len(content) - rule.head - rule.tail
        msg["content"] = f"{head}\n\n...[{trimmed} chars collapsed]...\n\n{tail}"


def _fix_tool_pairs(messages: list) -> None:
    """Repair orphaned tool_call / tool_result pairs after compression.

    Two fixes:
      1. Remove tool results whose matching tool_call was compressed away.
      2. Insert stub results for tool_calls whose results were compressed away.

    Args:
        messages: Message list (mutated in place).
    """
    # Collect all tool_call IDs from assistant messages
    call_ids: set[str] = set()
    for msg in messages:
        if msg.get("role") == "assistant":
            for tc in msg.get("tool_calls", []):
                tc_id = tc.get("id", "")
                if tc_id:
                    call_ids.add(tc_id)

    # Remove orphaned tool results
    i = 0
    while i < len(messages):
        msg = messages[i]
        if msg.get("role") == "tool" and msg.get("tool_call_id") not in call_ids:
            messages.pop(i)
        else:
            i += 1

    # Collect existing result IDs
    result_ids: set[str] = set()
    for msg in messages:
        if msg.get("role") == "tool":
            tcid = msg.get("tool_call_id", "")
            if tcid:
                result_ids.add(tcid)

    # Insert stub results for orphaned tool_calls
    inserts: list[tuple[int, dict]] = []
    for idx, msg in enumerate(messages):
        if msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls", []):
            tc_id = tc.get("id", "")
            if tc_id and tc_id not in result_ids:
                stub = {
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "name": tc.get("function", {}).get("name", "unknown"),
                    "content": "[Result from earlier context — see summary above]",
                }
                inserts.append((idx + 1, stub))
                result_ids.add(tc_id)

    for pos, stub in reversed(inserts):
        messages.insert(pos, stub)


def _attach_tool_call_thought_signatures(message: dict[str, Any], tool_calls: list) -> None:
    """Attach Gemini thought signatures to replayed assistant tool calls."""
    outbound_tool_calls = message.get("tool_calls")
    if not isinstance(outbound_tool_calls, list):
        return

    signatures_by_id = {
        tc.id: tc.thought_signature
        for tc in tool_calls
        if getattr(tc, "thought_signature", None)
    }
    for index, outbound_tool_call in enumerate(outbound_tool_calls):
        if not isinstance(outbound_tool_call, dict):
            continue
        signature = signatures_by_id.get(outbound_tool_call.get("id"))
        if not signature and index < len(tool_calls):
            signature = getattr(tool_calls[index], "thought_signature", None)
        if not signature:
            continue

        extra_content = outbound_tool_call.get("extra_content")
        if not isinstance(extra_content, dict):
            extra_content = {}
            outbound_tool_call["extra_content"] = extra_content
        google = extra_content.get("google")
        if not isinstance(google, dict):
            google = {}
            extra_content["google"] = google
        google["thought_signature"] = signature


# -- Structured summary templates ------------------------------------------

_STRUCTURED_SUMMARY_PROMPT = """\
Summarize this conversation for handoff to a fresh context window.
This summary is the ONLY context available — omitted information is lost.

Use EXACTLY this structure:

## Goal
What the user is trying to accomplish.

## Constraints & Preferences
User-stated requirements: risk tolerance, strategy parameters, asset preferences.

## Progress
### Done
- Completed steps with key results and specific numbers.
### In Progress
- Current work when compression triggered.

## Key Decisions
Choices made and rationale.

## Resolved Questions
Questions already answered — do NOT re-answer these.

## Pending User Asks
Unfinished requests still needing action.

## Relevant Files
File paths, run_dir, signal engines, artifact locations.

## Remaining Work
What still needs to be done (background reference, NOT active instructions).

## Critical Context
Specific numbers, parameters, error messages, configuration values.

## Tools & Patterns
Which tools worked, what failed, effective approaches.

IMPORTANT: This is a handoff — background reference, NOT active instructions.
Preserve ALL specific numbers, file paths, and parameter values.
{focus_section}
Conversation to summarize:
"""

_FOCUS_SECTION = """
FOCUS TOPIC: {topic}
Allocate 60-70% of the summary budget to content related to this topic.
Aggressively compress unrelated content to make room.
"""

_ITERATIVE_UPDATE_PROMPT = """\
Update the existing summary with new conversation turns.

PREVIOUS SUMMARY:
{previous_summary}

NEW TURNS TO INCORPORATE:
{new_turns}

Rules:
- PRESERVE all existing information from the previous summary.
- ADD new progress, decisions, and findings.
- Move "In Progress" items to "Done" when completed.
- Move answered questions to "Resolved Questions".
- Keep the same section structure.
- Do NOT drop any critical context from the previous summary.
{focus_section}"""


def _select_summary_input(head: list[dict]) -> tuple[str, int]:
    """Serialize the summary input newest-first within a token budget (V2).

    Args:
        head: The messages Layer 3 is about to summarize.

    Returns:
        Tuple of (serialized text, number of older messages dropped). When
        anything was dropped the text is prefixed with an explicit note so the
        summarizer does not read the gap as "nothing happened before".
    """
    kept: list[dict] = []
    budget = SUMMARY_INPUT_TOKEN_BUDGET
    # The thinking transcript is not part of the conversation being
    # summarised (and would eat the budget several times over).
    for msg in reversed(messages_for_estimate(head)):
        try:
            blob = json.dumps(msg, default=str, ensure_ascii=False)
        except (TypeError, ValueError):
            blob = str(msg)
        cost = estimate_text_tokens(blob)
        if cost > budget:
            # A single message bigger than the whole remaining budget is
            # skipped, not a stop condition: shorter older messages after it
            # can still fit.
            continue
        kept.append(msg)
        budget -= cost
    kept.reverse()
    dropped = len(head) - len(kept)
    try:
        text = json.dumps(kept, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(kept)
    if dropped:
        text = (
            f"[{dropped} older messages omitted from this summary input; they "
            "are covered by the previous summary and by the full transcript on "
            "disk.]\n" + text
        )
    return text, dropped


def _is_tool_success(result: str) -> bool:
    """Return True if the tool result does not look like an error response."""
    try:
        data = json.loads(result)
        if isinstance(data, dict) and data.get("status") == "error":
            return False
    except (json.JSONDecodeError, TypeError):
        pass
    return True


def _normalize_tool_run_dir(args: dict[str, Any], memory_run_dir: str | None) -> dict[str, Any]:
    """Normalize ``run_dir`` in tool args to an absolute path when possible.

    If the model supplies a relative ``run_dir`` (for example ``"."`` or
    ``"risk_parity_run"``), resolve it against the active run directory.
    """
    normalized = dict(args)
    if not memory_run_dir:
        return normalized

    if "run_dir" not in normalized:
        normalized["run_dir"] = memory_run_dir
        return normalized

    run_dir_value = str(normalized["run_dir"]).strip()
    if not run_dir_value:
        normalized["run_dir"] = memory_run_dir
        return normalized

    candidate = Path(run_dir_value)
    if not candidate.is_absolute():
        normalized["run_dir"] = str((Path(memory_run_dir) / candidate).resolve())
    return normalized


def tool_timeout_for(registry: Any, tool_name: str) -> float | None:
    """Per-call hard timeout for ``tool_name`` (None = no watchdog).

    Defaults to the tenant-wide ``TOOL_TIMEOUT_SECONDS``. A tool whose NORMAL
    runtime legitimately exceeds it declares ``timeout_seconds``
    (``run_swarm``: SWARM_TIMEOUT + margin). The declaration only RAISES the
    base of the 1x-warn / 2x-abandon window, never lowers it, and the
    attempt budget still clamps the result via ``cap_timeout`` at the call
    site — so a hung tool can never outlive the caller's deadline regardless
    of what it declares.

    ``max()`` rather than a plain override is deliberate: an operator lowering
    ``VIBE_TRADING_TOOL_TIMEOUT_SECONDS`` for a tenant must not silently
    truncate a swarm (``SWARM_TIMEOUT`` is that knob), and a tool author must
    not be able to shorten its own window and have its result thrown away.

    Args:
        registry: Tool registry.
        tool_name: Name of the tool about to be invoked.

    Returns:
        Timeout in seconds, or None when the watchdog is disabled.
    """
    base = TOOL_TIMEOUT_SECONDS
    get_tool = getattr(registry, "get", None)
    if callable(get_tool):
        try:
            # Read through the instance so a property-backed declaration
            # (SwarmTool) is evaluated at call time.
            declared = getattr(get_tool(tool_name), "timeout_seconds", None)
        except Exception:  # noqa: BLE001 - unknown tool keeps the default
            declared = None
        try:
            if declared:
                base = max(base, float(declared))
        except (TypeError, ValueError):  # noqa: BLE001 - malformed declaration
            pass
    return base if base > 0 else None


def tool_is_readonly(registry: Any, tool_name: str) -> bool:
    """Return whether a tool is known to be side-effect free.

    Args:
        registry: Tool registry.
        tool_name: Tool name.

    Returns:
        True only when the registry classifies the tool as readonly.
    """
    get_tool = getattr(registry, "get", None)
    if not callable(get_tool):
        return False
    try:
        tool_def = get_tool(tool_name)
    except Exception:  # noqa: BLE001 - unknown classification is not readonly
        return False
    return bool(tool_def and getattr(tool_def, "is_readonly", False))


def invoke_tool_guarded(
    registry: Any,
    tool_name: str,
    args: Dict[str, Any],
    *,
    readonly: bool,
    timeout: float | None,
    emit: Callable[[str, Dict[str, Any]], None],
    on_degraded: Callable[[], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> tuple[str, int]:
    """Run one tool under the watchdog: thread + timeout + heartbeat + progress.

    Shared with the swarm worker so it runs its tools through the SAME guard:
    a worker calling ``registry.execute`` inline would block forever on a tool
    that hangs inside an iteration — it only checks its deadline at iteration
    boundaries, and the layer-level deadline in ``swarm/runtime.py`` would
    then need ``layer_budget + 60s`` to notice.

    Semantics are unchanged from the main loop: a readonly tool that overruns
    is abandoned immediately with a structured ``tool_timeout``; a write tool
    (which cannot be safely cancelled) warns at 1x and abandons at
    ``WRITE_TOOL_TIMEOUT_FACTOR`` x, marking the run degraded. Late results are
    discarded and the emitters suppressed via the ``timed_out`` flag.

    Args:
        registry: Tool registry exposing ``execute(name, args)``.
        tool_name: Tool to run.
        args: Tool arguments.
        readonly: Whether the tool is known side-effect free.
        timeout: Per-call hard timeout, already budget-capped by the caller
            (None disables the watchdog).
        emit: ``(event_type, payload)`` sink for progress/heartbeat events.
        on_degraded: Called when a write tool is abandoned past its hard cap.
        cancel_event: Attempt-level cancel signal (defaults to the one bound
            in :mod:`src.core.cancel`). While set, the wait on the worker
            thread is abandoned within ``CANCEL_POLL_S`` and a structured
            ``cancelled`` result is returned — a cancel does not have to wait
            for a 30-minute tool to come back on its own.

    Returns:
        Tuple of (result_str, elapsed_ms).
    """
    timed_out = threading.Event()
    if cancel_event is None:
        cancel_event = _cancel.get_cancel_event()

    def _noop_degraded() -> None:
        return None

    if on_degraded is None:
        on_degraded = _noop_degraded

    def _on_progress(event: ProgressEvent) -> None:
        if timed_out.is_set():
            return
        payload = event.to_dict()
        payload["tool"] = tool_name
        emit("tool_progress", payload)

    def _on_heartbeat(payload: Dict[str, Any]) -> None:
        if timed_out.is_set():
            return
        emit("tool_heartbeat", payload)

    t0 = _time.perf_counter()
    timeout_label = _format_timeout(timeout) if timeout is not None else ""

    def _elapsed_ms() -> int:
        """Return milliseconds elapsed since tool start.

        Returns:
            Elapsed wall-clock time in milliseconds.
        """
        return int((_time.perf_counter() - t0) * 1000)

    def _heartbeat_timer() -> HeartbeatTimer:
        """Build the per-invocation heartbeat timer.

        Returns:
            HeartbeatTimer wired to this invocation's heartbeat emitter.
        """
        return HeartbeatTimer(
            tool_name=tool_name,
            interval=HEARTBEAT_INTERVAL_S,
            emit=_on_heartbeat,
        )

    def _emit_timeout_progress(stage: str, message: str, **extra: Any) -> int:
        """Emit a timeout-related tool_progress event.

        Args:
            stage: Progress stage label ("timeout" or "timeout_warning").
            message: Human-readable timeout message.
            **extra: Additional payload fields.

        Returns:
            Elapsed milliseconds at emission time.
        """
        elapsed_ms = _elapsed_ms()
        payload: Dict[str, Any] = {
            "tool": tool_name,
            "stage": stage,
            "message": message,
            "elapsed_s": round(elapsed_ms / 1000, 2),
        }
        payload.update(extra)
        emit("tool_progress", payload)
        return elapsed_ms

    # Both read and write tools run in a worker thread so a hung tool
    # becomes a bounded error: late results are discarded and the emitters
    # are suppressed via the timed_out event. Write tools cannot be safely
    # cancelled mid-flight, so they get a longer leash (see
    # WRITE_TOOL_TIMEOUT_FACTOR): warn at 1× the timeout, abandon at 2×.
    result_queue: queue.Queue[tuple[str | None, BaseException | None]] = queue.Queue(maxsize=1)

    def _worker() -> None:
        _set_emitter(_on_progress)
        try:
            result_queue.put((registry.execute(tool_name, args), None))
        except BaseException as exc:  # noqa: BLE001 - propagate through caller thread
            result_queue.put((None, exc))
        finally:
            _set_emitter(None)

    def _wait_result(
        budget_s: float | None,
    ) -> tuple[str | None, BaseException | None] | None:
        """Wait for the worker in ≤1s slices; ``None`` means cancelled.

        Raises:
            queue.Empty: When ``budget_s`` elapsed without a result.
        """
        if cancel_event is None:
            return result_queue.get(timeout=budget_s)
        deadline = None if budget_s is None else _time.monotonic() + budget_s
        while True:
            if cancel_event.is_set():
                return None
            if deadline is None:
                slice_s = _cancel.CANCEL_POLL_S
            else:
                left = deadline - _time.monotonic()
                if left <= 0:
                    raise queue.Empty()
                slice_s = min(_cancel.CANCEL_POLL_S, left)
            try:
                return result_queue.get(timeout=slice_s)
            except queue.Empty:
                # Cancel wins over an expiring deadline: a cancel that landed
                # during the last slice must surface as "cancelled", not as
                # the tool timeout it happened to coincide with.
                if cancel_event.is_set():
                    return None
                if deadline is not None and _time.monotonic() >= deadline:
                    raise
                continue

    def _cancelled_result() -> tuple[str, int]:
        timed_out.set()  # suppress late progress/heartbeat from the worker
        elapsed_ms = _emit_timeout_progress(
            "cancelled", "Attempt cancelled; tool wait abandoned"
        )
        return (
            json.dumps(
                {
                    "status": "error",
                    "error_code": "cancelled",
                    "tool": tool_name,
                    "message": "Attempt cancelled by the caller; the tool's wait was abandoned.",
                },
                ensure_ascii=False,
            ),
            elapsed_ms,
        )

    worker_ctx = contextvars.copy_context()
    worker = threading.Thread(
        target=lambda: worker_ctx.run(_worker),
        name=f"tool-{tool_name}",
        daemon=True,
    )
    worker.start()
    with _heartbeat_timer():
        try:
            got = _wait_result(timeout)
            if got is None:
                return _cancelled_result()
            result, exc = got
        except queue.Empty:
            if readonly:
                timed_out.set()
                elapsed_ms = _emit_timeout_progress(
                    "timeout", f"Tool exceeded {timeout_label} timeout"
                )
                return (
                    json.dumps(
                        {
                            "status": "error",
                            "error_code": "tool_timeout",
                            "tool": tool_name,
                            "timeout_seconds": timeout,
                            "message": f"Tool exceeded {timeout_label} timeout",
                        },
                        ensure_ascii=False,
                    ),
                    elapsed_ms,
                )
            # Write tool past 1× the timeout: warn once, then keep waiting
            # up to the hard cap (it cannot be safely cancelled, but it
            # must not be allowed to eat the whole attempt budget either).
            _emit_timeout_progress(
                "timeout_warning",
                (
                    f"Write tool exceeded {timeout_label} timeout; "
                    "waiting up to the hard cap because it cannot be "
                    "safely cancelled"
                ),
                readonly=False,
            )
            # The grace window is budget-capped too, so the total wait can
            # never overshoot the attempt deadline past its reserve.
            grace = (
                _budget.cap_timeout(
                    timeout * (WRITE_TOOL_TIMEOUT_FACTOR - 1.0),
                    reserve_s=_TOOL_CAP_RESERVE_S,
                    floor_s=_TOOL_GRACE_FLOOR_S,
                )
                if timeout is not None
                else None
            )
            try:
                got = _wait_result(grace)
                if got is None:
                    return _cancelled_result()
                result, exc = got
            except queue.Empty:
                timed_out.set()
                on_degraded()
                hard_label = _format_timeout(
                    (timeout or 0.0) * WRITE_TOOL_TIMEOUT_FACTOR
                )
                elapsed_ms = _emit_timeout_progress(
                    "timeout",
                    (
                        f"Write tool exceeded the {hard_label} hard timeout "
                        f"({WRITE_TOOL_TIMEOUT_FACTOR:g}x the regular limit); "
                        "abandoning the wait"
                    ),
                    readonly=False,
                )
                return (
                    json.dumps(
                        {
                            "status": "error",
                            "error_code": "write_tool_timeout",
                            "tool": tool_name,
                            "timeout_seconds": (
                                (timeout or 0.0) * WRITE_TOOL_TIMEOUT_FACTOR
                            ),
                            "degraded": True,
                            "message": (
                                f"Write tool exceeded the {hard_label} hard "
                                "timeout and its result was abandoned. Its "
                                "side effect may still complete in the "
                                "background — verify before retrying, and "
                                "do NOT assume the operation failed cleanly."
                            ),
                        },
                        ensure_ascii=False,
                    ),
                    elapsed_ms,
                )
    if exc is not None:
        raise exc
    return result or "", _elapsed_ms()


class AgentLoop:
    """ReAct Agent core loop.

    Attributes:
        registry: Tool registry.
        llm: ChatLLM client.
        memory: Workspace memory.
        max_iterations: Maximum number of iterations.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        llm: ChatLLM,
        memory: Optional[WorkspaceMemory] = None,
        event_callback: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        max_iterations: int = 50,
        persistent_memory: Optional[Any] = None,
    ) -> None:
        """Initialize AgentLoop.

        Args:
            registry: Tool registry.
            llm: ChatLLM client.
            memory: Workspace memory (created fresh if not provided).
            event_callback: Event callback (event_type, data).
            max_iterations: Maximum number of loop iterations.
            persistent_memory: PersistentMemory for cross-session recall.
        """
        self.registry = registry
        self.llm = llm
        self.memory = memory or WorkspaceMemory()
        self._event_callback = event_callback
        self.max_iterations = max_iterations
        # call_key -> the appended tool-result message dict. Keyed by
        # (name, args) — a name-level guard would refuse every follow-up
        # get_market_data with different symbols. The message
        # ref lets the guard see whether _microcompact pruned the result:
        # a pruned result means the model no longer has the data, so an
        # identical re-fetch must be allowed through.
        self._called_ok: Dict[str, Dict[str, Any]] = {}
        self._cancel_event = threading.Event()
        self._previous_summary: str = ""
        self._persistent_memory = persistent_memory
        self._run_iteration: int = 0
        self._stats: Dict[str, Any] = _new_run_stats()
        # (tool_name, raw_result) pairs feeding the finalization verifier.
        self._grounding_results: List[tuple[str, str]] = []
        # Layer 1 hysteresis state (armed flag), carried across iterations.
        self._microcompact_state: Dict[str, Any] = {}
        # Circuit breaker: call_key -> consecutive failure count. Keyed the
        # same way as the duplicate guard, which only ever registered SUCCESSES
        # — so an identical failing call could repeat until the iteration cap.
        self._consecutive_failures: Dict[str, int] = {}
        self._session_id: str = ""

    def cancel(self) -> None:
        """Cancel the current loop.

        Sets a thread-safe flag polled at every iteration boundary, per LLM
        stream chunk, and between tool batches, so a running turn stops at the
        next cooperative checkpoint instead of only at the next iteration.
        """
        self._cancel_event.set()

    def run(
        self,
        user_message: str,
        history: Optional[List[Dict[str, Any]]] = None,
        session_id: str = "",
        deadline: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Run the ReAct loop synchronously.

        Args:
            user_message: User message.
            history: Prior conversation messages.
            session_id: Session ID.
            deadline: Absolute ``time.monotonic()`` instant to finalize by.
                Defaults to the deadline bound in the ambient budget context
                (set by SessionService from the caller's ``deadline_s``).

        Returns:
            Execution result dict.
        """
        # The cancel token belongs to this attempt and is never reset here: a
        # cancel that lands before run() starts (executor queue, registry
        # build) must still take effect, so the first checkpoint below turns
        # an already-set token into a "cancelled" terminal state instead of
        # an orphaned loop nobody can reach any more.
        if self._cancel_event.is_set():
            logger.info("AgentLoop cancelled before start")
        # Expose the cancel signal to every tool thread (copy_context) so
        # long polls (swarm wait, tool watchdog) can stop between ticks.
        _cancel.bind_cancel_event(self._cancel_event)
        self._called_ok = {}
        self._session_id = session_id or ""
        # Resume Layer 5 from the session's stored handoff summary instead
        # of restarting from zero. The next compaction then takes the iterative
        # update path, so decisions and constraints compressed away in an
        # earlier attempt are inherited rather than lost (no extra LLM call).
        self._previous_summary = handoff.load(session_id) if session_id else ""
        self._stats = _new_run_stats()
        self._grounding_results = []
        self._microcompact_state = {}
        self._consecutive_failures = {}
        run_t0 = _time.perf_counter()
        _fetch_stats.start_collect()
        if deadline is None:
            deadline = _budget.get_deadline()
        else:
            _budget.bind_deadline(deadline)
        budget_total_s = (
            max(0.0, deadline - _time.monotonic()) if deadline is not None else None
        )

        state_store = RunStateStore()
        # Everything before the ReAct loop (run dir, request snapshot, prompt
        # assembly, trace file) can fail on a full or read-only tenant disk.
        # Such a failure must still end as a regular ``failed`` result with an
        # attempt_stats frame — otherwise the session layer sees a bare
        # exception, writes no receipt, and the router waits out the whole
        # budget for an answer that will never come.
        run_dir: Optional[Path] = None
        trace: Optional[TraceWriter] = None
        try:
            RUNS_DIR.mkdir(parents=True, exist_ok=True)

            if self.memory.run_dir and Path(self.memory.run_dir).exists():
                run_dir = Path(self.memory.run_dir)
            else:
                run_dir = state_store.create_run_dir(RUNS_DIR)
                self.memory.run_dir = str(run_dir)

            state_store.save_request(run_dir, user_message, {"session_id": session_id})

            context = ContextBuilder(self.registry, self.memory,
                                      persistent_memory=self._persistent_memory)
            goal_context, active_goal_id = get_current_goal_context(session_id) if session_id else ("", None)
            llm_user_message = user_message
            if goal_context:
                llm_user_message = (
                    f"{goal_context}\n\n"
                    f"<user-message>\n{user_message}\n</user-message>"
                )
            goal_store = None
            goal_turn_accounted = False
            messages = context.build_messages(llm_user_message, history)
            react_trace: List[Dict[str, Any]] = []

            trace_dir = SESSIONS_DIR / session_id if session_id else run_dir
            trace = TraceWriter(trace_dir)
            if self._run_iteration == 0 and trace.path.exists():
                existing = TraceWriter.read(trace_dir)
                self._run_iteration = max(
                    (int(e.get("iter", 0)) for e in existing if "iter" in e),
                    default=0,
                )
            trace.write_text_entry(
                {"type": "start", "iter": self._run_iteration + 1},
                field="prompt",
                value=user_message,
                offload_kind=f"start-{self._run_iteration + 1}",
            )
            trace.write_text_entry(
                {"type": "message", "iter": self._run_iteration + 1, "role": "user"},
                field="content",
                value=user_message,
                offload_kind=f"user-message-{self._run_iteration + 1}",
            )
        except Exception as exc:
            return self._fail_before_loop(
                exc, run_dir=run_dir, trace=trace, state_store=state_store, run_t0=run_t0
            )

        iteration = 0
        final_content = ""
        empty_model_response_iter: int | None = None
        empty_response_retries = 0
        length_continuations = 0
        # Partial replies cut by the output ceiling, in order, awaiting the
        # continuation that completes them.
        truncated_parts: list[str] = []
        llm_usage_summary = _new_llm_usage_summary(self.llm)
        goal_continuations = 0
        goal_last_progress: tuple[int, int] | None = None
        wrap_up_at = max(1, int(self.max_iterations * 0.8))
        force_final = False

        try:
            while iteration < self.max_iterations:
                if self._cancel_event.is_set():
                    trace.write({"type": "cancelled", "iter": self._run_iteration + 1})
                    logger.info("AgentLoop cancelled by user")
                    break

                iteration += 1
                self._run_iteration += 1
                current_iter = self._run_iteration

                # Inject background task notifications
                bg = get_background_manager()
                notifs = bg.drain_notifications()
                if notifs:
                    notif_text = "\n".join(f"[bg:{n['task_id']}] {n['status']}: {n['result']}" for n in notifs)
                    messages.append({"role": "user", "content": f"<background-results>\n{notif_text}\n</background-results>\n\n<system>Continue processing with the background results above.</system>"})

                # Drop the previous iteration's ephemeral status bar before
                # any compaction, so it never survives into summaries.
                _remove_status_messages(messages)

                # ``reasoning_content`` counts only when the channel sends it.
                count_reasoning = bool(getattr(self.llm, "sends_reasoning_content", False))

                # Layer 1: microcompact (threshold-triggered + armed hysteresis)
                _microcompact(
                    messages, state=self._microcompact_state, count_reasoning=count_reasoning
                )

                # Layer 2: context collapse (fold long text, zero API cost)
                tokens = estimate_tokens(messages, count_reasoning=count_reasoning)
                if tokens > COLLAPSE_THRESHOLD:
                    _context_collapse(messages)
                    tokens = estimate_tokens(messages, count_reasoning=count_reasoning)

                # Layer 3: auto_compact (token threshold exceeded)
                if tokens > TOKEN_THRESHOLD:
                    logger.info(f"Auto compact triggered: {tokens} tokens > {TOKEN_THRESHOLD}")
                    self._auto_compact(messages, run_dir, trace, iteration=current_iter)

                logger.info(f"ReAct iteration {iteration}/{self.max_iterations}")

                # Per-iteration status bar: time + State counters live at
                # the trajectory tail, keeping the system prompt byte-stable.
                # Budget / wrap-up nudges fold into the same message and are
                # recomputed while their condition holds (the bar is replaced
                # every iteration, so a one-shot append would vanish).
                nudge_lines: list[str] = []

                # Wrap-up nudge when approaching the iteration limit. Skips
                # the first iteration (tiny budgets) and the last iteration
                # (the forced text-only path already guarantees an answer).
                if wrap_up_at <= iteration < self.max_iterations and iteration > 1:
                    remaining = self.max_iterations - iteration
                    nudge_lines.append(
                        f"[SYSTEM] You have {remaining} iterations remaining out of "
                        f"{self.max_iterations}. Please wrap up your work. "
                        "Stop calling tools and provide your final answer as plain text. "
                        "If you have partial results, summarize what you have so far."
                    )

                # Batch 3 — wall-clock budget management. Two escalations:
                # (a) <25% of the budget left → wrap-up nudge, independent
                #     of the iteration counter (which fires far too late when
                #     iterations are slow);
                # (b) not enough time left for another full iteration → force
                #     the final text answer NOW, before the caller times out.
                remaining_s = (
                    deadline - _time.monotonic() if deadline is not None else None
                )
                if remaining_s is not None and iteration > 1:
                    avg_iter_s = (_time.perf_counter() - run_t0) / max(1, iteration - 1)
                    if remaining_s < max(FINALIZE_RESERVE_S, avg_iter_s * 1.2):
                        if not force_final:
                            # trace/emit once; the nudge line itself repeats
                            # with the status bar for as long as needed.
                            self._stats["early_finalize"] = True
                            trace.write(
                                {
                                    "type": "early_finalize",
                                    "iter": current_iter,
                                    "remaining_s": round(remaining_s, 1),
                                    "avg_iter_s": round(avg_iter_s, 1),
                                }
                            )
                            self._emit(
                                "early_finalize",
                                {"iter": current_iter, "remaining_s": round(remaining_s, 1)},
                            )
                        force_final = True
                        nudge_lines.append(
                            "[SYSTEM] The time budget for this request is nearly "
                            "exhausted. Stop all tool use and give your final answer "
                            "NOW based on the material you already gathered. State "
                            "explicitly which parts are incomplete or unverified."
                        )
                    elif budget_total_s and remaining_s / budget_total_s < 0.25:
                        nudge_lines.append(
                            f"[SYSTEM] Less than 25% of the time budget remains "
                            f"(~{int(remaining_s)}s). Prioritize concluding: avoid "
                            "new lines of investigation, finish with the data you "
                            "have, and prepare your final answer."
                        )

                # The last iteration (or a deadline-driven early finalize) is
                # a forced text turn: the tool definitions stay in the request
                # and ``tool_choice=none`` tells the model not to call any (the
                # Anthropic Messages API rejects a history with tool_use /
                # tool_result blocks but no ``tools``); the [SYSTEM] line says
                # what is expected of the turn.
                is_last_iteration = (iteration == self.max_iterations) or force_final
                if is_last_iteration and not force_final:
                    nudge_lines.append(
                        "[SYSTEM] This is the final turn and tool calls are disabled. "
                        "Write your final answer now as plain text, based on the "
                        "material you already gathered; state explicitly which "
                        "parts are incomplete or unverified."
                    )

                messages.append(
                    _build_status_message(self.memory.to_summary(), nudge_lines)
                )

                # Streaming output + collect thinking text
                thinking_chunks: List[str] = []
                reasoning_chars = 0
                last_reasoning_emit: float | None = None

                def _on_text_chunk(delta: str) -> None:
                    thinking_chunks.append(delta)
                    self._emit("text_delta", {"delta": delta, "iter": current_iter})

                def _on_reasoning_chunk(delta: str) -> None:
                    # Throttled: long reasoning streams produce hundreds of
                    # chunks; emitting each one floods the SSE replay buffer
                    # and evicts tool_call/text_delta events. The first chunk
                    # of each iteration always emits immediately so the UI
                    # flips to "Reasoning…" without delay.
                    nonlocal reasoning_chars, last_reasoning_emit
                    reasoning_chars += len(delta)
                    now = _time.monotonic()
                    if (
                        last_reasoning_emit is not None
                        and now - last_reasoning_emit < REASONING_DELTA_MIN_INTERVAL_S
                    ):
                        return
                    last_reasoning_emit = now
                    self._emit(
                        "reasoning_delta",
                        {"iter": current_iter, "chars": reasoning_chars},
                    )

                tool_defs = self.registry.get_definitions()
                tool_choice = TOOL_CHOICE_NONE if is_last_iteration else None
                if is_last_iteration:
                    trace.write(
                        {
                            "type": "forced_text_only",
                            "iter": current_iter,
                            "mode": (
                                "tool_choice_none"
                                if getattr(self.llm, "supports_tool_choice_none", True)
                                else "tools_omitted"
                            ),
                        }
                    )

                llm_t0 = _time.perf_counter()
                # In-place recovery for transient mid-stream failures
                # (ReadTimeout, connection reset, relay hiccup, 5xx/429):
                # retry with exponential backoff WITHOUT resetting the loop —
                # messages/iteration progress are preserved, only this call
                # repeats. Mirrors the swarm worker policy. Deterministic 4xx
                # errors fail immediately; retries stop when the attempt's
                # remaining budget can't absorb the next backoff sleep. Deltas
                # from a failed attempt are dropped so the trace does not
                # contain duplicated thinking text.
                response = None
                for stream_attempt in range(1 + STREAM_RETRIES):
                    try:
                        self._stats["llm_calls"] += 1
                        response = self.llm.stream_chat(
                            messages,
                            tools=tool_defs,
                            on_text_chunk=_on_text_chunk,
                            on_reasoning_chunk=_on_reasoning_chunk,
                            should_cancel=self._cancel_event.is_set,
                            tool_choice=tool_choice,
                        )
                        break
                    except ProviderStreamError as exc:
                        if not exc.retryable or stream_attempt >= STREAM_RETRIES:
                            raise
                        delay = min(
                            STREAM_RETRY_DELAY_S * (4**stream_attempt),
                            STREAM_RETRY_MAX_DELAY_S,
                        )
                        remaining = _budget.remaining_s()
                        if remaining is not None and remaining <= delay + 30:
                            raise
                        logger.warning(
                            "Provider stream failed (iter %s), retry %d/%d in %.0fs: %s",
                            current_iter,
                            stream_attempt + 1,
                            STREAM_RETRIES,
                            delay,
                            exc,
                        )
                        reset_payload = {
                            "iter": current_iter,
                            "reason": "provider_stream_retry",
                            "attempt": stream_attempt + 1,
                            "max_retries": STREAM_RETRIES,
                            "delay_s": delay,
                            "provider": exc.provider,
                            "model": exc.model,
                        }
                        self._emit("stream_reset", reset_payload)
                        _fetch_stats.record_stream_retry("main")
                        try:
                            trace.write({"type": "stream_reset", **reset_payload})
                        except Exception:  # noqa: BLE001 - trace must never break the run
                            logger.debug("stream_reset trace write failed", exc_info=True)
                        thinking_chunks.clear()
                        reasoning_chars = 0
                        last_reasoning_emit = None
                        # A cancel during the backoff ends the run at the
                        # check below instead of after the full sleep.
                        if _cancel.sleep_unless_cancelled(delay, self._cancel_event):
                            break
                llm_elapsed_ms = int((_time.perf_counter() - llm_t0) * 1000)
                self._stats["llm_ms"] += llm_elapsed_ms
                # Persist the LLM call as a trace block (ts = end time) so the
                # execution gantt can draw exact LLM segments instead of
                # inferring them from gaps between tool calls.
                try:
                    trace.write(
                        {"type": "llm_call", "iter": current_iter, "elapsed_ms": llm_elapsed_ms}
                    )
                except Exception:  # noqa: BLE001 - trace must never break the run
                    logger.debug("llm_call trace write failed", exc_info=True)

                # Cancelled mid-stream (or during a retry backoff): discard
                # this turn's partial response and end the run now, without
                # executing any of its tool calls.
                if self._cancel_event.is_set() or response is None:
                    break

                usage = getattr(response, "usage_metadata", None)
                usage_delta = _record_llm_usage(
                    run_dir,
                    llm_usage_summary,
                    usage,
                    current_iter,
                )
                if usage_delta:
                    self._emit(
                        "llm_usage",
                        {
                            **usage_delta,
                            "iter": current_iter,
                        },
                    )
                if active_goal_id and session_id:
                    token_delta = int(usage_delta.get("total_tokens") or 0) if usage_delta else 0
                    turn_delta = 0 if goal_turn_accounted else 1
                    if token_delta or turn_delta:
                        try:
                            if goal_store is None:
                                from src.goal import GoalStore

                                goal_store = GoalStore()
                            goal_store.account_usage(
                                session_id=session_id,
                                goal_id=active_goal_id,
                                expected_goal_id=active_goal_id,
                                token_delta=token_delta,
                                turn_delta=turn_delta,
                            )
                            goal_turn_accounted = True
                            snapshot = goal_store.get_goal_snapshot(active_goal_id)
                            if snapshot is not None:
                                self._emit(
                                    "goal.updated",
                                    {"goal": snapshot["goal"], "snapshot": snapshot},
                                )
                        except Exception as exc:  # noqa: BLE001
                            logger.debug("Goal usage accounting skipped: %s", exc)

                thinking_text = "".join(thinking_chunks)
                if thinking_text:
                    trace.write_text_entry(
                        {"type": "thinking", "iter": current_iter},
                        field="content",
                        value=thinking_text,
                        offload_kind=f"thinking-{current_iter}",
                    )
                    self._emit("thinking_done", {"iter": current_iter, "content": thinking_text[:500]})

                # Duck-typed: LLM stand-ins may omit finish_reason.
                finish_reason = getattr(response, "finish_reason", "stop")
                if finish_reason == "length":
                    truncated_payload = {
                        "iter": current_iter,
                        "chars": len(response.content or ""),
                        "has_tool_calls": response.has_tool_calls,
                    }
                    trace.write({"type": "output_truncated", **truncated_payload})
                    self._emit("output_truncated", truncated_payload)
                    self._stats["output_truncations"] = (
                        self._stats.get("output_truncations", 0) + 1
                    )

                if finish_reason == "length" and response.has_tool_calls:
                    # The calls' arguments were cut mid-stream: refuse all of
                    # them (see TRUNCATED_TOOL_CALL_ERROR) and let the model
                    # re-issue shorter ones. Counts as a length continuation.
                    length_continuations += 1
                    truncated_parts = []
                    refused = [tc.name for tc in response.tool_calls]
                    messages.extend(
                        truncated_tool_call_messages(
                            response.tool_calls,
                            content=response.content,
                            reasoning_content=response.reasoning_content or None,
                        )
                    )
                    self._stats["truncated_tool_calls"] = (
                        self._stats.get("truncated_tool_calls", 0) + len(refused)
                    )
                    trace.write(
                        {
                            "type": "tool_calls_truncated",
                            "iter": current_iter,
                            "tools": refused,
                            "attempt": length_continuations,
                            "max_continuations": LENGTH_CONTINUATIONS,
                        }
                    )
                    react_trace.append({"type": "tool_calls_truncated", "tools": refused})
                    continue

                if not response.has_tool_calls:
                    final_content = response.content or ""
                    if (
                        finish_reason == "length"
                        and final_content
                        and not is_last_iteration
                        and length_continuations < LENGTH_CONTINUATIONS
                    ):
                        # Keep the partial reply in the trajectory and ask for
                        # the rest; the continuation consumes a normal
                        # iteration (never rewind the counters — the trace is
                        # indexed by ``iter``).
                        length_continuations += 1
                        truncated_parts.append(final_content)
                        trace.write_text_entry(
                            {"type": "message", "iter": current_iter, "role": "assistant"},
                            field="content",
                            value=final_content,
                            offload_kind=f"assistant-message-{current_iter}",
                        )
                        trace.write(
                            {
                                "type": "output_truncated_continue",
                                "iter": current_iter,
                                "attempt": length_continuations,
                                "max_continuations": LENGTH_CONTINUATIONS,
                            }
                        )
                        messages.append({"role": "assistant", "content": final_content})
                        messages.append({"role": "user", "content": _LENGTH_CONTINUE_NUDGE})
                        # Fallback answer should the run end without another
                        # text turn: the partial, marked as such.
                        final_content += OUTPUT_TRUNCATED_MARK
                        continue
                    if truncated_parts:
                        # The continuation(s) complete the earlier partial text.
                        final_content = "".join(truncated_parts) + final_content
                        truncated_parts = []
                    if finish_reason == "length" and final_content:
                        final_content += OUTPUT_TRUNCATED_MARK
                    if not final_content:
                        empty_payload = {
                            "iter": current_iter,
                            "provider": os.getenv("LANGCHAIN_PROVIDER", "openai"),
                            "model": getattr(self.llm, "model_name", None) or os.getenv("LANGCHAIN_MODEL_NAME", ""),
                        }
                        # One in-place retry with an explicit nudge before
                        # writing off the attempt. The stream SUCCEEDED — this
                        # is a degraded provider turn, not a transport failure,
                        # so the STREAM_RETRIES path above never covered it.
                        if empty_response_retries < EMPTY_RESPONSE_RETRIES:
                            empty_response_retries += 1
                            trace.write(
                                {
                                    "type": "empty_model_response_retry",
                                    "attempt": empty_response_retries,
                                    "max_retries": EMPTY_RESPONSE_RETRIES,
                                    **empty_payload,
                                }
                            )
                            self._emit(
                                "empty_model_response_retry",
                                {"attempt": empty_response_retries, **empty_payload},
                            )
                            # The nudge is appended (never rewritten), and the
                            # retry consumes a normal iteration — rewinding the
                            # counters would emit duplicate ``iter`` keys into
                            # the trace the observability waterfall indexes by.
                            messages.append(
                                {"role": "user", "content": _EMPTY_RESPONSE_NUDGE}
                            )
                            continue
                        empty_model_response_iter = iteration
                        trace.write({"type": "empty_model_response", **empty_payload})
                        break
                    should_continue_goal = False
                    continuation_snapshot = None
                    if active_goal_id and session_id and GOAL_MAX_CONTINUATIONS > 0:
                        try:
                            if goal_store is None:
                                from src.goal import GoalStore

                                goal_store = GoalStore()
                            continuation_snapshot = goal_store.get_goal_snapshot(active_goal_id)
                            should_continue_goal = bool(
                                continuation_snapshot
                                and goal_needs_continuation(continuation_snapshot)
                            )
                        except Exception as exc:  # noqa: BLE001
                            logger.debug("Goal continuation check skipped: %s", exc)

                    if should_continue_goal and continuation_snapshot is not None:
                        current_progress = goal_progress_tuple(continuation_snapshot)
                        no_new_progress = (
                            goal_last_progress is not None
                            and current_progress <= goal_last_progress
                        )
                        if goal_continuations >= GOAL_MAX_CONTINUATIONS or (
                            no_new_progress and goal_continuations > 0
                        ):
                            trace.write(
                                {
                                    "type": "goal_continuation_suppressed",
                                    "iter": current_iter,
                                    "goal_id": active_goal_id,
                                    "progress": current_progress,
                                    "continuations": goal_continuations,
                                }
                            )
                        else:
                            trace.write_text_entry(
                                {
                                    "type": "goal_intermediate_answer",
                                    "iter": current_iter,
                                    "goal_id": active_goal_id,
                                    "progress": current_progress,
                                },
                                field="content",
                                value=final_content,
                                offload_kind=f"goal-intermediate-answer-{current_iter}",
                            )
                            trace.write_text_entry(
                                {"type": "message", "iter": current_iter, "role": "assistant"},
                                field="content",
                                value=final_content,
                                offload_kind=f"assistant-message-{current_iter}",
                            )
                            react_trace.append(
                                {"type": "goal_intermediate_answer", "content": final_content[:500]}
                            )
                            messages.append({"role": "assistant", "content": final_content})
                            messages.append(
                                {
                                    "role": "user",
                                    "content": format_goal_continuation_prompt(
                                        continuation_snapshot,
                                        previous_answer=final_content,
                                    ),
                                }
                            )
                            goal_last_progress = current_progress
                            goal_continuations += 1
                            continue

                    trace.write_text_entry(
                        {"type": "answer", "iter": current_iter},
                        field="content",
                        value=final_content,
                        offload_kind=f"answer-{current_iter}",
                    )
                    trace.write_text_entry(
                        {"type": "message", "iter": current_iter, "role": "assistant"},
                        field="content",
                        value=final_content,
                        offload_kind=f"assistant-message-{current_iter}",
                    )
                    react_trace.append({"type": "answer", "content": final_content[:500]})
                    break

                # A tool-calling turn after a length continuation restarts the
                # model's own reasoning; the partial text stays in the
                # trajectory for it to reuse, not as a prefix of the answer.
                truncated_parts = []
                assistant_message = context.format_assistant_tool_calls(
                    response.tool_calls,
                    content=response.content,
                    # Only the channel's own reasoning field; the visible text
                    # is already ``content`` and must not be mirrored here.
                    reasoning_content=response.reasoning_content or None,
                )
                _attach_tool_call_thought_signatures(assistant_message, response.tool_calls)
                messages.append(assistant_message)

                # Execute tools with read/write batching
                compact_requested, focus_topic = self._process_tool_calls(
                    response.tool_calls, context, messages, trace, react_trace, current_iter,
                )

                # Layer 3: compress after all tools have executed
                if compact_requested:
                    logger.info("Manual compact triggered by model")
                    self._auto_compact(messages, run_dir, trace, focus_topic=focus_topic, iteration=current_iter)

        except Exception as exc:
            logger.exception(f"AgentLoop error: {exc}")
            error_code = (
                "provider_stream_error"
                if isinstance(exc, ProviderStreamError)
                else "agent_loop_error"
            )
            _best_effort(
                trace.write,
                {"type": "end", "iter": self._run_iteration, "status": "error",
                 "reason": str(exc), "iterations": iteration},
            )
            self._emit_attempt_stats(
                "error", iteration, run_t0, llm_usage_summary, trace, reason=str(exc)
            )
            _best_effort(trace.close)
            _best_effort(state_store.mark_failure, run_dir, str(exc))
            return {
                "status": "failed",
                "error_code": error_code,
                "reason": str(exc),
                "run_dir": str(run_dir),
                "run_id": run_dir.name,
                "content": "",
                "react_trace": react_trace,
                "iterations": iteration,
                "max_iterations": self.max_iterations,
            }

        # Tidy the long-term memory index at run end when it nears its cap,
        # instead of waiting for the model to act on the "index is full"
        # warning itself. Runs here, after the trajectory is finished, so
        # the session-start snapshot frozen into the system prompt is never
        # churned mid-run. Best effort — it never affects the result.
        if self._persistent_memory is not None:
            try:
                consolidation = self._persistent_memory.maybe_auto_consolidate()
                if consolidation:
                    logger.info("Auto-consolidated memory index: %s", consolidation)
                    trace.write({"type": "memory_auto_consolidated", **consolidation})
            except Exception:  # noqa: BLE001 - memory tidying is never fatal
                logger.debug("memory auto-consolidation skipped", exc_info=True)

        # Determine final status. The reason is also propagated into the
        # returned dict so SessionService can surface a meaningful UI
        # message instead of "Execution failed: unknown" (issue #114).
        final_reason: str | None = None
        if self._cancel_event.is_set():
            final_reason = "cancelled by user"
            state_store.mark_failure(run_dir, final_reason)
            final_status = "cancelled"
        elif (run_dir / "artifacts" / "metrics.csv").exists() or final_content:
            state_store.mark_success(run_dir)
            final_status = "success"
            # F1: zero-LLM structural verification. Warnings never flip the
            # success status — they ride attempt_stats / trace / an event so
            # the observability panel can surface suspect runs.
            try:
                verify_warnings = verify_run(
                    run_dir, final_content, self._grounding_results
                )
            except Exception:  # noqa: BLE001 - verification must never break the run
                logger.debug("run verification failed", exc_info=True)
                verify_warnings = []
            if verify_warnings:
                self._stats["verify_warnings"] = verify_warnings
                try:
                    trace.write(
                        {
                            "type": "verify_warnings",
                            "iter": self._run_iteration,
                            "warnings": verify_warnings,
                        }
                    )
                except Exception:  # noqa: BLE001 - trace must never break the run
                    logger.debug("verify_warnings trace write failed", exc_info=True)
                self._emit("verify_warnings", {"warnings": verify_warnings})
        elif empty_model_response_iter is not None:
            provider = os.getenv("LANGCHAIN_PROVIDER", "openai").strip().lower() or "openai"
            model = getattr(self.llm, "model_name", None) or os.getenv("LANGCHAIN_MODEL_NAME", "").strip() or "(unset)"
            final_reason = (
                "empty_model_response: "
                f"provider={provider} model={model} iteration {empty_model_response_iter} "
                "returned no content and no tool calls"
            )
            state_store.mark_failure(run_dir, final_reason)
            final_status = "failed"
        else:
            final_reason = (
                f"reached max iterations ({self.max_iterations}) without final answer"
            )
            state_store.mark_failure(run_dir, final_reason)
            final_status = "failed"

        end_event: dict[str, Any] = {
            "type": "end",
            "iter": self._run_iteration,
            "status": final_status,
            "iterations": iteration,
        }
        if final_reason is not None:
            end_event["reason"] = final_reason
        trace.write(end_event)
        self._emit_attempt_stats(
            {"success": "ok", "cancelled": "cancelled"}.get(final_status, "failed"),
            iteration,
            run_t0,
            llm_usage_summary,
            trace,
            reason=final_reason,
        )
        trace.close()

        result: dict[str, Any] = {
            "status": final_status,
            "run_dir": str(run_dir),
            "run_id": run_dir.name,
            "content": final_content,
            "react_trace": react_trace,
            "iterations": iteration,
            "max_iterations": self.max_iterations,
        }
        if final_reason is not None:
            result["reason"] = final_reason
        return result

    def _emit_attempt_stats(
        self,
        status: str,
        iterations: int,
        run_t0: float,
        llm_usage_summary: dict[str, Any] | None,
        trace: Optional[TraceWriter],
        reason: str | None = None,
    ) -> None:
        """Emit the per-attempt observability summary (SSE + trace).

        One frame per attempt, at the very end, regardless of outcome (``trace``
        is ``None`` only when the run failed before its trace file existed). The
        multi-tenant router forwards it to laicai as a progress frame; laicai
        persists it into ``deep_engine_runs``. ``data_fetches`` / ``data_gaps``
        are reserved for the data-reliability batch and empty for now, so the
        frame shape is stable for consumers from day one.
        """
        totals = (llm_usage_summary or {}).get("totals", {}) or {}
        tool_map: dict[str, dict[str, int]] = self._stats.get("tools", {})
        tools = [
            {"name": name, **vals}
            for name, vals in sorted(tool_map.items(), key=lambda kv: -kv[1]["ms"])
        ]
        stats: dict[str, Any] = {
            "status": status,
            "total_ms": int((_time.perf_counter() - run_t0) * 1000),
            "iterations": iterations,
            "max_iterations": self.max_iterations,
            "llm_calls": int(self._stats.get("llm_calls", 0)),
            "llm_ms": int(self._stats.get("llm_ms", 0)),
            "compact_calls": int(self._stats.get("compact_calls", 0)),
            "tool_ms": sum(v.get("ms", 0) for v in tool_map.values()),
            "tokens": {
                "input": int(totals.get("input_tokens") or 0),
                "output": int(totals.get("output_tokens") or 0),
                "total": int(totals.get("total_tokens") or 0),
                # Prompt-cache breakdown of ``input`` (only when non-zero):
                # cache_read / input is the cache hit rate.
                **{
                    out_key.removesuffix("_tokens"): int(totals[out_key])
                    for _source_key, out_key in _CACHE_USAGE_FIELDS
                    if totals.get(out_key)
                },
            },
            "tools": tools,
            "data_fetches": [],
            "data_gaps": [],
            "early_finalize": bool(self._stats.get("early_finalize")),
            # F2: set when a write tool blew through its hard timeout and its
            # result was abandoned — the attempt finished on partial footing.
            "degraded": bool(self._stats.get("degraded")),
            "model": getattr(self.llm, "model_name", None)
            or os.getenv("LANGCHAIN_MODEL_NAME", ""),
        }
        # F1: structural verification warnings (only present when non-empty).
        if self._stats.get("verify_warnings"):
            stats["verify_warnings"] = self._stats["verify_warnings"]
        # Degradation counters (only present when non-zero): L3 summary call
        # failures, oversized-result offload failures, replies cut by the
        # output ceiling and the tool calls refused because of it; counted
        # into _stats and emitted here.
        for counter in (
            "compact_failures",
            "offload_failures",
            "output_truncations",
            "truncated_tool_calls",
        ):
            if self._stats.get(counter):
                stats[counter] = int(self._stats[counter])
        collector = _fetch_stats.current()
        if collector is not None:
            fetches, gaps = collector.snapshot()
            stats["data_fetches"] = fetches
            stats["data_gaps"] = gaps
            stats["skills"] = collector.snapshot_skills()
            stats["swarm_runs"] = collector.snapshot_swarm()
            background = collector.snapshot_background()
            if background:
                stats["background_tasks"] = background
            stream_retries = collector.snapshot_stream()
            if stream_retries:
                # Rate = (main + swarm) / (llm_calls + swarm_llm_calls);
                # llm_calls above already counts main-loop attempts incl.
                # retries, swarm attempts ride in this dict.
                stats["stream_retries"] = stream_retries
        if reason:
            stats["reason"] = str(reason)[:500]
        if trace is not None:
            _best_effort(trace.write, {"type": "attempt_stats", **stats})
        self._emit("attempt_stats", stats)

    def _fail_before_loop(
        self,
        exc: Exception,
        *,
        run_dir: Optional[Path],
        trace: Optional[TraceWriter],
        state_store: RunStateStore,
        run_t0: float,
    ) -> Dict[str, Any]:
        """Terminal ``failed`` result for an exception raised before the loop.

        Same envelope as a mid-loop failure (``status`` / ``reason`` /
        ``error_code`` / ``run_id``) and the same ``attempt_stats`` frame, so
        the session layer and the router treat both alike. Every write here
        is best-effort: the usual cause is a disk that cannot be written to.
        """
        logger.exception("AgentLoop failed before the loop started: %s", exc)
        reason = str(exc)
        if trace is not None:
            _best_effort(
                trace.write,
                {"type": "end", "iter": self._run_iteration, "status": "error",
                 "reason": reason, "iterations": 0},
            )
        self._emit_attempt_stats("error", 0, run_t0, None, trace, reason=reason)
        if trace is not None:
            _best_effort(trace.close)
        if run_dir is not None:
            _best_effort(state_store.mark_failure, run_dir, reason)
        return {
            "status": "failed",
            "error_code": "agent_loop_error",
            "reason": reason,
            "run_dir": str(run_dir) if run_dir is not None else None,
            "run_id": run_dir.name if run_dir is not None else None,
            "content": "",
            "react_trace": [],
            "iterations": 0,
            "max_iterations": self.max_iterations,
        }

    # -- Tool execution with read/write batching --------------------------------

    def _process_tool_calls(
        self,
        tool_calls: list,
        context: ContextBuilder,
        messages: list,
        trace: TraceWriter,
        react_trace: list,
        iteration: int,
    ) -> tuple[bool, str]:
        """Pre-process tool calls: handle compact, filter duplicates, batch execute.

        Args:
            tool_calls: Raw tool calls from LLM response.
            context: ContextBuilder for formatting messages.
            messages: Conversation messages (appended in place).
            trace: TraceWriter.
            react_trace: React trace list.
            iteration: Current iteration number.

        Returns:
            Tuple of (compact_requested, focus_topic).
        """
        compact_requested = False
        focus_topic = ""
        to_execute = []

        # Cancelled before this turn's tools ran — skip execution entirely.
        if self._cancel_event.is_set():
            return compact_requested, focus_topic

        for tc in tool_calls:
            # Layer 4: compact tool — mark then defer execution
            if tc.name == "compact":
                compact_requested = True
                focus_topic = tc.arguments.get("focus_topic", "")
                messages.append(context.format_tool_result(tc.id, "compact", '{"status":"ok","message":"Compressing..."}'))
                trace.write({"type": "compact_requested", "iter": iteration})
                continue

            call_key = _tool_call_key(tc.name, tc.arguments)

            # V2 circuit breaker (book §1.2 "Correct"): the duplicate guard
            # only ever registered SUCCESSES, so an identical call that keeps
            # failing — a dead upstream, a malformed argument the model won't
            # revise — could repeat until the iteration cap or the wall-clock
            # budget ran out. After TOOL_CIRCUIT_FAILURE_LIMIT consecutive
            # failures of the SAME (tool, args) pair the call is refused with
            # an actionable structured error instead of being executed again.
            if self._consecutive_failures.get(call_key, 0) >= TOOL_CIRCUIT_FAILURE_LIMIT:
                logger.warning(
                    "Circuit open for %s (%d consecutive identical failures)",
                    tc.name,
                    self._consecutive_failures[call_key],
                )
                open_msg = json.dumps(
                    {
                        "status": "error",
                        "error_code": "circuit_open",
                        "tool": tc.name,
                        "consecutive_failures": self._consecutive_failures[call_key],
                        "message": (
                            f"This exact {tc.name} call (same arguments) has "
                            f"failed {self._consecutive_failures[call_key]} times "
                            "in a row and is now blocked. Do NOT retry it "
                            "unchanged: change the arguments, use a different "
                            "tool or data source, or answer with the data you "
                            "already have and state the gap explicitly."
                        ),
                    },
                    ensure_ascii=False,
                )
                messages.append(context.format_tool_result(tc.id, tc.name, open_msg))
                trace.write(
                    {
                        "type": "tool_circuit_open",
                        "iter": iteration,
                        "tool": tc.name,
                        "consecutive_failures": self._consecutive_failures[call_key],
                    }
                )
                react_trace.append({"type": "tool_circuit_open", "tool": tc.name})
                self._emit(
                    "tool_circuit_open",
                    {
                        "tool": tc.name,
                        "consecutive_failures": self._consecutive_failures[call_key],
                        "iter": iteration,
                    },
                )
                continue

            tool_def = self.registry.get(tc.name)
            is_repeatable = tool_def.repeatable if tool_def else False
            prior = self._called_ok.get(call_key)
            prior_intact = (
                prior is not None
                and isinstance(prior.get("content"), str)
                and not prior["content"].startswith(_CLEARED_PREFIX)
            )
            if prior_intact and not is_repeatable:
                logger.warning(f"Blocked duplicate call: {tc.name} (identical args already succeeded)")
                skip_msg = json.dumps({"skipped": True, "reason": f"An identical {tc.name} call (same arguments) already succeeded above — use that result. To fetch different data, change the arguments."})
                messages.append(context.format_tool_result(tc.id, tc.name, skip_msg))
                trace.write({"type": "tool_skipped", "iter": iteration, "tool": tc.name})
                react_trace.append({"type": "tool_skipped", "tool": tc.name})
                continue

            to_execute.append(tc)

        if not to_execute:
            return compact_requested, focus_topic

        # Batch execute: consecutive readonly → parallel, write → serial
        if len(to_execute) == 1:
            self._execute_single(to_execute[0], context, messages, trace, react_trace, iteration)
        else:
            self._batch_execute(to_execute, context, messages, trace, react_trace, iteration)

        return compact_requested, focus_topic

    def _batch_execute(
        self,
        tool_calls: list,
        context: ContextBuilder,
        messages: list,
        trace: TraceWriter,
        react_trace: list,
        iteration: int,
    ) -> None:
        """Execute tools with read/write batching.

        Consecutive readonly tools run in parallel via ThreadPoolExecutor.
        Write tools run serially between readonly batches.

        Args:
            tool_calls: Tool calls to execute.
            context: ContextBuilder.
            messages: Conversation messages.
            trace: TraceWriter.
            react_trace: React trace list.
            iteration: Current iteration.
        """
        # Split into batches: consecutive readonly → parallel, write → serial
        batches: list[tuple[str, list]] = []
        current_ro: list = []

        for tc in tool_calls:
            tool_def = self.registry.get(tc.name)
            if tool_def and tool_def.is_readonly:
                current_ro.append(tc)
            else:
                if current_ro:
                    batches.append(("parallel", current_ro))
                    current_ro = []
                batches.append(("serial", [tc]))
        if current_ro:
            batches.append(("parallel", current_ro))

        for mode, batch in batches:
            # Stop launching further tool batches once cancelled — the current
            # batch (if any) finishes, but no new work starts.
            if self._cancel_event.is_set():
                break
            if mode == "parallel" and len(batch) > 1:
                self._execute_parallel(batch, context, messages, trace, react_trace, iteration)
            else:
                for tc in batch:
                    self._execute_single(tc, context, messages, trace, react_trace, iteration)

    def _execute_parallel(
        self,
        tool_calls: list,
        context: ContextBuilder,
        messages: list,
        trace: TraceWriter,
        react_trace: list,
        iteration: int,
    ) -> None:
        """Execute readonly tools in parallel using threads.

        Args:
            tool_calls: Readonly tool calls to execute in parallel.
            context: ContextBuilder.
            messages: Conversation messages.
            trace: TraceWriter.
            react_trace: React trace list.
            iteration: Current iteration.
        """
        # Prepare args + emit events
        runnable: list[tuple] = []
        for tc in tool_calls:
            args = _normalize_tool_run_dir(tc.arguments, self.memory.run_dir)
            redacted_args = redact_payload(args)
            event_args = {k: str(v)[:200] for k, v in redacted_args.items()}
            self._emit("tool_call", {"tool": tc.name, "arguments": event_args, "iter": iteration})
            trace.write({"type": "tool_call", "iter": iteration, "tool": tc.name, "call_id": tc.id, "args": redacted_args})
            runnable.append((tc, args))

        # Execute in parallel — each worker gets its own heartbeat + progress emitter.
        def _run(tc_args: tuple) -> tuple:
            tc, args = tc_args
            result, elapsed_ms = self._invoke_tool(tc.name, args)
            return tc, result, elapsed_ms

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(runnable), 8)) as pool:
            # copy_context per worker keeps the bound session/attempt log ids
            # visible inside tool threads (each Context can only be entered once
            # at a time, hence one copy per submission).
            futures = [
                pool.submit(contextvars.copy_context().run, _run, item)
                for item in runnable
            ]
            results = []
            for i, f in enumerate(futures):
                try:
                    results.append(f.result())
                except Exception as exc:
                    tc = runnable[i][0]
                    results.append((tc, json.dumps({"status": "error", "error": str(exc)}), 0))

        # Process results in order
        for tc, result, elapsed_ms in results:
            self._finalize_tool_result(tc, result, elapsed_ms, context, messages, trace, react_trace, iteration)

    def _execute_single(
        self,
        tc: Any,
        context: ContextBuilder,
        messages: list,
        trace: TraceWriter,
        react_trace: list,
        iteration: int,
    ) -> None:
        """Execute a single tool call.

        Args:
            tc: Tool call object.
            context: ContextBuilder.
            messages: Conversation messages.
            trace: TraceWriter.
            react_trace: React trace list.
            iteration: Current iteration.
        """
        args = _normalize_tool_run_dir(tc.arguments, self.memory.run_dir)

        redacted_args = redact_payload(args)
        event_args = {k: str(v)[:200] for k, v in redacted_args.items()}
        self._emit("tool_call", {"tool": tc.name, "arguments": event_args, "iter": iteration})
        trace.write({"type": "tool_call", "iter": iteration, "tool": tc.name, "call_id": tc.id, "args": redacted_args})
        logger.info(f"Tool call: {tc.name}({list(args.keys())})")

        result, elapsed_ms = self._invoke_tool(tc.name, args)

        self._finalize_tool_result(tc, result, elapsed_ms, context, messages, trace, react_trace, iteration)

    def _invoke_tool(self, tool_name: str, args: Dict[str, Any]) -> tuple[str, int]:
        """Execute a tool with heartbeat + structured progress emission.

        Thin wrapper over :func:`invoke_tool_guarded`: resolves the per-tool
        timeout, clamps it to the attempt budget, and wires the loop's event
        sink. The guard body is shared with the swarm worker so the two
        cannot drift on timeout / heartbeat / budget-clamp semantics.

        Args:
            tool_name: Tool name to execute.
            args: Tool arguments dict.

        Returns:
            Tuple of (result_str, elapsed_ms).
        """
        timeout = self._tool_timeout(tool_name)
        if timeout is not None:
            # Never let a single tool outlive the attempt budget:
            # keep a reserve so the loop can still produce a final answer.
            timeout = _budget.cap_timeout(
                timeout, reserve_s=_TOOL_CAP_RESERVE_S, floor_s=_TOOL_CAP_FLOOR_S
            )

        def _mark_degraded() -> None:
            self._stats["degraded"] = True

        return invoke_tool_guarded(
            self.registry,
            tool_name,
            args,
            readonly=self._is_tool_readonly(tool_name),
            timeout=timeout,
            emit=self._emit,
            on_degraded=_mark_degraded,
            cancel_event=self._cancel_event,
        )

    def _tool_timeout(self, tool_name: str) -> float | None:
        """Per-call hard timeout for ``tool_name`` (None = no watchdog).

        Defaults to the tenant-wide ``TOOL_TIMEOUT_SECONDS``. A tool whose
        NORMAL runtime legitimately exceeds it declares ``timeout_seconds``
        (``run_swarm``: SWARM_TIMEOUT + margin). The declaration only RAISES
        the base of the 1×-warn / 2×-abandon window, never lowers it, and
        the attempt budget still clamps the result via ``cap_timeout`` at the
        call site — so a hung tool can never outlive the caller's deadline
        regardless of what it declares.

        ``max()`` rather than a plain override is deliberate: an operator
        lowering ``VIBE_TRADING_TOOL_TIMEOUT_SECONDS`` for a tenant must not
        silently truncate a swarm (``SWARM_TIMEOUT`` is that knob), and a tool
        author must not be able to shorten its own window and have its result
        thrown away.

        Args:
            tool_name: Name of the tool about to be invoked.

        Returns:
            Timeout in seconds, or None when the watchdog is disabled.
        """
        return tool_timeout_for(self.registry, tool_name)

    def _is_tool_readonly(self, tool_name: str) -> bool:
        """Return whether a tool is known to be side-effect free."""
        return tool_is_readonly(self.registry, tool_name)

    def _finalize_tool_result(
        self,
        tc: Any,
        result: str,
        elapsed_ms: int,
        context: ContextBuilder,
        messages: list,
        trace: TraceWriter,
        react_trace: list,
        iteration: int,
    ) -> None:
        """Record a tool result: update memory, append message, write trace, emit event.

        Args:
            tc: Tool call object.
            result: Raw tool result string.
            elapsed_ms: Execution time in milliseconds.
            context: ContextBuilder.
            messages: Conversation messages.
            trace: TraceWriter.
            react_trace: React trace list.
            iteration: Current iteration.
        """
        self._update_memory(tc.name)

        # Scrub env-derived credential VALUES before the result
        # reaches the trajectory, the trace or the grounding verifier. The
        # shell tools already scrub their own stdout; this covers every other
        # tool (read_file on a dumped .env, an MCP error echoing a header …).
        result = redact_secret_values(result)

        success = _is_tool_success(result)

        tool_stats = self._stats.setdefault("tools", {}).setdefault(
            tc.name, {"calls": 0, "ms": 0, "errors": 0}
        )
        tool_stats["calls"] += 1
        tool_stats["ms"] += int(elapsed_ms or 0)
        if not success:
            tool_stats["errors"] += 1

        status = "ok" if success else "error"
        # Oversized results go to disk and the model gets an EXPLICIT
        # preview envelope pointing at the file. The raw ``result`` is
        # deliberately still what the success classifier, the grounding
        # verifier and the trace consume — only the trajectory copy shrinks.
        payload, offload_failed = prepare_for_context(
            result,
            base_dir=Path(self.memory.run_dir) if self.memory.run_dir else None,
            iteration=iteration,
            tool_name=tc.name,
            call_id=getattr(tc, "id", "") or "",
        )
        if offload_failed:
            self._stats["offload_failures"] = self._stats.get("offload_failures", 0) + 1
        result_msg = context.format_tool_result(tc.id, tc.name, payload)
        messages.append(result_msg)
        call_key = _tool_call_key(tc.name, tc.arguments)
        if success:
            # Keep the message REF so the duplicate guard can tell whether
            # _microcompact has since pruned this result (pruned -> re-allow).
            self._called_ok[call_key] = result_msg
            self._consecutive_failures.pop(call_key, None)
            # F1: keep raw grounding results for the finalization verifier.
            if tc.name in VERIFY_GROUNDING_TOOLS:
                self._grounding_results.append((tc.name, result))
                if len(self._grounding_results) > VERIFY_GROUNDING_MAX_RESULTS:
                    self._grounding_results.pop(0)
        else:
            self._consecutive_failures[call_key] = (
                self._consecutive_failures.get(call_key, 0) + 1
            )

        trace_result = _redact_trace_result(result)
        trace.write_tool_result(
            call_id=tc.id,
            result=trace_result,
            tool_name=tc.name,
            status=status,
            elapsed_ms=elapsed_ms,
            iteration=iteration,
        )
        preview = trace_result[:200]
        react_trace.append({"type": "tool_call", "tool": tc.name, "result_preview": preview})
        self._emit("tool_result", {"tool": tc.name, "status": status, "elapsed_ms": elapsed_ms, "preview": preview})

    # -- Context compression ---------------------------------------------------

    def _auto_compact(
        self,
        messages: list,
        run_dir: Path,
        trace: TraceWriter,
        focus_topic: str = "",
        iteration: int = 0,
    ) -> None:
        """Layer 3/4/5: structured LLM summary with token-budget tail protection.

        Upgrades over the original:
          - Token-budget tail: keeps ~20K tokens of recent messages (not a fixed count).
          - Structured summary template: preserves goal, progress, decisions, files, etc.
          - Iterative update: Nth compression updates previous summary, zero info decay.
          - Tool pair fix: repairs orphaned tool_call/tool_result after compression.
          - Focus-topic: optionally prioritize specific topic in summary.

        Args:
            messages: Message list (replaced in place).
            run_dir: Run directory.
            trace: TraceWriter.
            focus_topic: Optional topic to prioritize in the summary.
            iteration: Current trace iteration.
        """
        del run_dir
        # Save full transcript before compressing next to the active trace.
        transcript_path = trace.dir_path / f"transcript_{int(_time.time())}.jsonl"
        with open(transcript_path, "w", encoding="utf-8") as f:
            for msg in messages:
                f.write(json.dumps(msg, default=str, ensure_ascii=False) + "\n")

        system_msg = messages[0]
        body = messages[1:]

        # Token-budget tail: walk backward to find how many recent messages to preserve
        accumulated = 0
        cut_idx = len(body)
        for i in range(len(body) - 1, -1, -1):
            content = body[i].get("content", "")
            msg_tokens = estimate_text_tokens(str(content)) + 10
            if accumulated + msg_tokens > TAIL_TOKEN_BUDGET:
                cut_idx = i + 1
                break
            accumulated += msg_tokens
            cut_idx = i

        # Don't split in the middle of a tool_call/tool_result pair
        while 0 < cut_idx < len(body) and body[cut_idx].get("role") == "tool":
            cut_idx += 1

        head = body[:cut_idx]
        tail = body[cut_idx:]

        if not head:
            # All body fits in tail budget — force a split to avoid infinite loop
            if len(body) > 2:
                cut_idx = max(1, len(body) // 2)
                head = body[:cut_idx]
                tail = body[cut_idx:]
            else:
                logger.warning("Auto compact: nothing to compress (body too small)")
                return

        # Build focus section
        focus_section = _FOCUS_SECTION.format(topic=focus_topic) if focus_topic else ""

        # Build summary prompt (structured template or iterative update)
        conv_text, dropped_msgs = _select_summary_input(head)

        if self._previous_summary:
            prompt = _ITERATIVE_UPDATE_PROMPT.format(
                previous_summary=self._previous_summary,
                new_turns=conv_text,
                focus_section=focus_section,
            )
        else:
            prompt = _STRUCTURED_SUMMARY_PROMPT.format(focus_section=focus_section) + conv_text

        compact_t0 = _time.perf_counter()
        # Compaction is a CORRECT mechanism — it must never be the thing that
        # kills an otherwise healthy run: without this guard one provider
        # hiccup on the summary call would propagate to run()'s top-level
        # except and fail the whole attempt. On failure we degrade to the zero-LLM
        # layers (L1/L2 already ran this iteration) and leave the trajectory
        # untouched; the next iteration retries compaction.
        try:
            summary_resp = self.llm.chat([{"role": "user", "content": prompt}])
            summary = summary_resp.content or ""
        except Exception as exc:  # noqa: BLE001 - degrade, never fail the run
            self._stats["llm_ms"] += int((_time.perf_counter() - compact_t0) * 1000)
            self._stats["compact_failures"] = self._stats.get("compact_failures", 0) + 1
            logger.warning("Auto compact LLM call failed, degrading to L1/L2: %s", exc)
            payload = {"iter": iteration, "error": str(exc)[:300]}
            try:
                trace.write({"type": "compact_failed", **payload})
            except Exception:  # noqa: BLE001 - trace must never break the run
                logger.debug("compact_failed trace write failed", exc_info=True)
            self._emit("compact_failed", payload)
            return
        self._stats["compact_calls"] += 1
        self._stats["llm_calls"] += 1
        self._stats["llm_ms"] += int((_time.perf_counter() - compact_t0) * 1000)
        if not summary.strip():
            # An empty summary would erase the head without replacing it.
            logger.warning("Auto compact produced an empty summary; skipping rebuild")
            return
        self._previous_summary = summary
        # Persist the moment it exists, not at run end — the attempt that
        # times out or crashes is exactly the one whose summary the NEXT
        # attempt needs. See src/session/handoff.py.
        handoff.save(self._session_id, summary, attempt_iter=iteration)

        tokens_before = estimate_tokens(messages)
        trace.write_text_entry(
            {
                "type": "compact",
                "iter": iteration,
                "tokens_before": tokens_before,
                "focus_topic": focus_topic or "(none)",
                "input_messages_dropped": dropped_msgs,
            },
            field="summary",
            value=summary,
            offload_kind=f"compact-summary-{iteration}",
        )
        self._emit("compact", {"tokens_before": tokens_before, "summary": summary[:200]})

        # Reconstruct: system + summary + acknowledge + preserved tail
        state_summary = self.memory.to_summary()
        # HANDOFF_PREFIX (not a literal) so Layer 2's skip rule and the
        # session-level replay header recognise the same marker.
        compressed = (
            f"{HANDOFF_PREFIX} — handoff summary. "
            f"Transcript: {transcript_path}]\n\n{summary}"
        )
        if state_summary and state_summary != "(empty state)":
            compressed += f"\n\nCurrent agent state:\n{state_summary}"

        messages.clear()
        messages.append(system_msg)
        messages.append({"role": "user", "content": f"{compressed}\n\n<system>Continue from the summary above.</system>"})
        messages.extend(tail)

        # Fix orphaned tool pairs in the reconstructed message list
        _fix_tool_pairs(messages)

        # The duplicate-call guard keys on the tool-result
        # message OBJECT. Results compressed into the summary are gone from
        # the trajectory, so an identical re-fetch must be allowed again —
        # otherwise the model is told "use the result above" about data it
        # can no longer see. Keep only entries whose message survived in the
        # tail (identity comparison, same as the _microcompact pruning rule).
        live = {id(m) for m in messages}
        self._called_ok = {
            key: msg for key, msg in self._called_ok.items() if id(msg) in live
        }

    def _emit(self, event_type: str, data: Dict[str, Any]) -> None:
        """Fire an event via the callback."""
        if self._event_callback:
            try:
                self._event_callback(event_type, data)
            except Exception:
                pass

    def _update_memory(self, tool_name: str) -> None:
        """Update workspace memory counters after tool execution."""
        self.memory.increment(tool_name)
