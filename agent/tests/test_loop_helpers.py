"""Tests for AgentLoop pure helper functions (zero LLM dependency)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from src.agent.loop import (
    KEEP_RECENT,
    COLLAPSE_PRESERVE_RECENT,
    COLLAPSE_TEXT_MIN,
    MICROCOMPACT_KEEP_BUDGET_RATIO,
    MICROCOMPACT_PROTECTED_TOOLS,
    estimate_tokens,
    _build_status_message,
    _microcompact,
    _context_collapse,
    _fix_tool_pairs,
    _is_tool_success,
    _normalize_tool_run_dir,
    _remove_status_messages,
    _CLEARED_PLACEHOLDER,
    _CLEARED_PREFIX,
    _STATUS_PREFIX,
    _tool_call_key,
)


# ---------------------------------------------------------------------------
# estimate_tokens
# ---------------------------------------------------------------------------


class TestEstimateTokens:
    def test_empty(self) -> None:
        assert estimate_tokens([]) == len("[]") // 4

    def test_proportional(self) -> None:
        short = [{"role": "user", "content": "hi"}]
        long = [{"role": "user", "content": "x" * 4000}]
        assert estimate_tokens(long) > estimate_tokens(short)

    def test_rough_accuracy(self) -> None:
        # ~4 chars per token
        msg = [{"role": "user", "content": "a" * 400}]
        tokens = estimate_tokens(msg)
        # Should be roughly 100 tokens for 400 chars of content (plus overhead)
        assert 80 < tokens < 200

    def test_cjk_weighted(self) -> None:
        """CJK counts ~0.6 token/char — the old flat //4 under-counted
        Chinese contexts 2-3x (E4)."""
        en = [{"role": "user", "content": "a" * 400}]
        zh = [{"role": "user", "content": "股" * 400}]
        en_tokens = estimate_tokens(en)
        zh_tokens = estimate_tokens(zh)
        # 400 CJK chars ≈ 240 tokens vs 400 ASCII chars ≈ 100 tokens
        assert zh_tokens > en_tokens * 2
        assert 240 <= zh_tokens < 300


# ---------------------------------------------------------------------------
# _microcompact
# ---------------------------------------------------------------------------


class TestMicrocompact:
    """Threshold-triggered pruning (E1). Passing ``token_threshold=0`` forces
    the trigger with a zero keep-budget — reproducing the old fixed
    keep-last-3 behavior for these legacy assertions."""

    def test_clears_old_tool_messages(self) -> None:
        messages = [
            {"role": "system", "content": "system"},
        ]
        # Add KEEP_RECENT + 5 tool messages with long content
        for i in range(KEEP_RECENT + 5):
            messages.append({"role": "tool", "content": f"{'x' * 200} result_{i}", "tool_call_id": f"tc_{i}"})

        _microcompact(messages, token_threshold=0)

        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        # Old ones should carry the informative cleared placeholder
        cleared = [m for m in tool_msgs if m["content"] == _CLEARED_PLACEHOLDER]
        preserved = [m for m in tool_msgs if m["content"] != _CLEARED_PLACEHOLDER]
        assert len(cleared) == 5
        assert len(preserved) == KEEP_RECENT
        # The placeholder must explain re-fetching is allowed (dea1222743ef)
        assert _CLEARED_PLACEHOLDER.startswith(_CLEARED_PREFIX)
        assert "re-call" in _CLEARED_PLACEHOLDER

    def test_not_triggered_below_threshold(self) -> None:
        """Below the trigger ratio the trajectory stays byte-identical —
        no more unconditional every-iteration pruning (prompt cache)."""
        messages = [{"role": "system", "content": "system"}]
        for i in range(KEEP_RECENT + 5):
            messages.append({"role": "tool", "content": f"{'x' * 200} result_{i}", "tool_call_id": f"tc_{i}"})
        before = [m["content"] for m in messages]

        _microcompact(messages)  # default TOKEN_THRESHOLD ≫ this tiny context

        assert [m["content"] for m in messages] == before

    def test_token_budget_keeps_more_than_floor(self) -> None:
        """When the keep-budget affords it, more than KEEP_RECENT recent
        results survive intact."""
        messages = [{"role": "system", "content": "system"}]
        for i in range(16):
            messages.append({"role": "tool", "content": f"{'x' * 400} result_{i}", "tool_call_id": f"tc_{i}"})
        # Each result ≈ 100 tokens; the 16-message context estimates well
        # above the trigger (0.5 × threshold). Budget = threshold × ratio
        # ≈ 550 tokens → keeps ~5 newest results, prunes the rest.
        threshold = int(550 / MICROCOMPACT_KEEP_BUDGET_RATIO)

        _microcompact(messages, token_threshold=threshold)

        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        preserved = [m for m in tool_msgs if m["content"] != _CLEARED_PLACEHOLDER]
        cleared = [m for m in tool_msgs if m["content"] == _CLEARED_PLACEHOLDER]
        assert len(preserved) > KEEP_RECENT
        assert cleared, "oldest results beyond the budget must still be pruned"
        # The preserved ones are the newest
        assert preserved[-1]["content"].endswith("result_15")

    def test_protected_tools_never_cleared(self) -> None:
        """Grounding / key-deliverable tool results survive pruning."""
        protected_name = next(iter(MICROCOMPACT_PROTECTED_TOOLS))
        messages = [{"role": "system", "content": "system"}]
        messages.append(
            {"role": "tool", "name": protected_name, "content": "P" * 300, "tool_call_id": "tc_p"}
        )
        for i in range(KEEP_RECENT + 4):
            messages.append({"role": "tool", "name": "bash", "content": f"{'x' * 200} r_{i}", "tool_call_id": f"tc_{i}"})

        _microcompact(messages, token_threshold=0)

        assert messages[1]["content"] == "P" * 300
        cleared = [m for m in messages if m.get("content") == _CLEARED_PLACEHOLDER]
        assert cleared, "unprotected old results are still pruned"

    def test_preserves_short_content(self) -> None:
        messages = [
            {"role": "tool", "content": "short", "tool_call_id": "tc_0"},
            {"role": "tool", "content": "also short", "tool_call_id": "tc_1"},
            {"role": "tool", "content": "short too", "tool_call_id": "tc_2"},
            {"role": "tool", "content": "x" * 200, "tool_call_id": "tc_3"},
            {"role": "tool", "content": "x" * 200, "tool_call_id": "tc_4"},
            {"role": "tool", "content": "x" * 200, "tool_call_id": "tc_5"},
            {"role": "tool", "content": "x" * 200, "tool_call_id": "tc_6"},
        ]
        _microcompact(messages, token_threshold=0)
        # First tool msg is old and long enough → cleared
        # But "short" is ≤100 chars → not cleared even if old
        short_msgs = [m for m in messages if m["content"] in ("short", "also short")]
        assert len(short_msgs) == 2

    def test_no_op_when_few_messages(self) -> None:
        messages = [
            {"role": "tool", "content": "x" * 200, "tool_call_id": "tc_0"},
        ]
        _microcompact(messages, token_threshold=0)
        assert messages[0]["content"] == "x" * 200


class TestToolCallKey:
    """Duplicate-guard keys must be (name, args)-scoped — the old name-level
    guard refused every follow-up get_market_data with different symbols
    (attempt dea1222743ef)."""

    def test_same_name_different_args_differ(self) -> None:
        a = _tool_call_key("get_market_data", {"codes": ["INTC.US"]})
        b = _tool_call_key("get_market_data", {"codes": ["AVGO.US"]})
        assert a != b

    def test_same_args_stable_regardless_of_key_order(self) -> None:
        a = _tool_call_key("t", {"x": 1, "y": 2})
        b = _tool_call_key("t", {"y": 2, "x": 1})
        assert a == b

    def test_unserializable_args_do_not_raise(self) -> None:
        key = _tool_call_key("t", {"f": object()})
        assert key.startswith("t:")

    def test_does_not_touch_non_tool(self) -> None:
        messages = [
            {"role": "user", "content": "x" * 500},
            {"role": "assistant", "content": "x" * 500},
            {"role": "tool", "content": "x" * 200, "tool_call_id": "tc_0"},
            {"role": "tool", "content": "x" * 200, "tool_call_id": "tc_1"},
            {"role": "tool", "content": "x" * 200, "tool_call_id": "tc_2"},
            {"role": "tool", "content": "x" * 200, "tool_call_id": "tc_3"},
        ]
        _microcompact(messages)
        assert messages[0]["content"] == "x" * 500
        assert messages[1]["content"] == "x" * 500


# ---------------------------------------------------------------------------
# _context_collapse
# ---------------------------------------------------------------------------


class TestContextCollapse:
    def test_collapses_long_content(self) -> None:
        messages = [{"role": "system", "content": "sys"}]
        # Add enough messages to exceed COLLAPSE_PRESERVE_RECENT
        for i in range(COLLAPSE_PRESERVE_RECENT + 5):
            messages.append({"role": "user", "content": f"{'z' * (COLLAPSE_TEXT_MIN + 500)} msg_{i}"})

        _context_collapse(messages)

        # Early messages should be collapsed. Index 2, not 1: since V2 the
        # FIRST user message gets the looser FIRST_USER rule (see
        # TestContextCollapsePolicy below).
        assert "collapsed" in messages[2]["content"]
        # Recent messages should be intact
        assert "collapsed" not in messages[-1]["content"]

    def test_skips_short_content(self) -> None:
        messages = [{"role": "system", "content": "sys"}]
        for i in range(COLLAPSE_PRESERVE_RECENT + 3):
            messages.append({"role": "user", "content": f"short msg {i}"})
        originals = [m["content"] for m in messages]
        _context_collapse(messages)
        # Nothing should change because all content is short
        for orig, msg in zip(originals, messages):
            assert msg["content"] == orig

    def test_skips_cleared_content(self) -> None:
        messages = [{"role": "system", "content": "sys"}]
        for _ in range(COLLAPSE_PRESERVE_RECENT + 3):
            messages.append({"role": "tool", "content": "[cleared]"})
        _context_collapse(messages)
        # [cleared] should remain [cleared], not be collapsed
        for m in messages[1:]:
            assert m["content"] == "[cleared]"

    def test_no_op_when_too_few_messages(self) -> None:
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "x" * 5000},
        ]
        _context_collapse(messages)
        assert "collapsed" not in messages[1]["content"]

    def test_preserves_head_and_tail(self) -> None:
        messages = [{"role": "system", "content": "sys"}]
        for i in range(COLLAPSE_PRESERVE_RECENT + 3):
            messages.append({"role": "user", "content": f"HEAD_MARKER{'x' * COLLAPSE_TEXT_MIN}TAIL_MARKER msg_{i}"})

        _context_collapse(messages)

        # messages[1] is the first user message and folds under the looser
        # FIRST_USER rule; messages[2] is an ordinary one.
        collapsed_msg = messages[2]["content"]
        assert "HEAD_MARKER" in collapsed_msg
        assert "TAIL_MARKER" in collapsed_msg
        assert "collapsed" in collapsed_msg


# ---------------------------------------------------------------------------
# Status bar (E2): dynamic time/state at the trajectory tail
# ---------------------------------------------------------------------------


class TestStatusBar:
    def test_build_contains_time_state_and_nudges(self) -> None:
        msg = _build_status_message("run_dir=/tmp/r1", ["[SYSTEM] wrap up"])
        assert msg["role"] == "user"
        content = msg["content"]
        assert content.startswith(_STATUS_PREFIX)
        assert "</agent_status>" in content
        # ISO timestamp with timezone (session time-awareness must not be lost)
        assert "Now: 2" in content and "T" in content
        assert "State: run_dir=/tmp/r1" in content
        assert "[SYSTEM] wrap up" in content

    def test_build_without_nudges(self) -> None:
        msg = _build_status_message("(empty state)", [])
        assert "[SYSTEM]" not in msg["content"]
        assert msg["content"].rstrip().endswith("</agent_status>")

    def test_remove_strips_only_status_messages(self) -> None:
        status = _build_status_message("(empty state)", [])
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "real question"},
            status,
            {"role": "assistant", "content": "answer"},
            _build_status_message("(empty state)", ["[SYSTEM] hurry"]),
        ]
        _remove_status_messages(messages)
        assert len(messages) == 3
        assert all(
            not (isinstance(m.get("content"), str) and m["content"].startswith(_STATUS_PREFIX))
            for m in messages
        )
        assert messages[1]["content"] == "real question"

    def test_remove_ignores_non_string_content(self) -> None:
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "block"}]},
        ]
        _remove_status_messages(messages)
        assert len(messages) == 1


# ---------------------------------------------------------------------------
# _fix_tool_pairs
# ---------------------------------------------------------------------------


class TestFixToolPairs:
    def test_removes_orphan_result(self) -> None:
        messages = [
            {"role": "assistant", "content": "thinking", "tool_calls": [
                {"id": "tc_1", "function": {"name": "bash"}},
            ]},
            {"role": "tool", "tool_call_id": "tc_1", "name": "bash", "content": "ok"},
            # Orphan: no matching tool_call
            {"role": "tool", "tool_call_id": "tc_orphan", "name": "ghost", "content": "orphan"},
        ]
        _fix_tool_pairs(messages)
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0]["tool_call_id"] == "tc_1"

    def test_inserts_stub_for_orphan_call(self) -> None:
        messages = [
            {"role": "assistant", "content": "thinking", "tool_calls": [
                {"id": "tc_1", "function": {"name": "bash"}},
                {"id": "tc_2", "function": {"name": "read_file"}},
            ]},
            # Only result for tc_1, tc_2 is missing
            {"role": "tool", "tool_call_id": "tc_1", "name": "bash", "content": "ok"},
        ]
        _fix_tool_pairs(messages)
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        assert len(tool_msgs) == 2
        stub = [m for m in tool_msgs if m["tool_call_id"] == "tc_2"]
        assert len(stub) == 1
        assert "earlier context" in stub[0]["content"]

    def test_no_op_when_balanced(self) -> None:
        messages = [
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "tc_1", "function": {"name": "bash"}},
            ]},
            {"role": "tool", "tool_call_id": "tc_1", "name": "bash", "content": "ok"},
        ]
        before = len(messages)
        _fix_tool_pairs(messages)
        assert len(messages) == before

    def test_handles_empty_messages(self) -> None:
        messages = []
        _fix_tool_pairs(messages)
        assert messages == []

    def test_multiple_orphans(self) -> None:
        messages = [
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "tc_1", "function": {"name": "a"}},
                {"id": "tc_2", "function": {"name": "b"}},
                {"id": "tc_3", "function": {"name": "c"}},
            ]},
            # No results at all
        ]
        _fix_tool_pairs(messages)
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        assert len(tool_msgs) == 3


# ---------------------------------------------------------------------------
# _is_tool_success
# ---------------------------------------------------------------------------


class TestIsToolSuccess:
    def test_success_plain_text(self) -> None:
        assert _is_tool_success("some output text") is True

    def test_success_json_ok(self) -> None:
        assert _is_tool_success('{"status": "ok", "data": 42}') is True

    def test_failure_json_error(self) -> None:
        assert _is_tool_success('{"status": "error", "error": "boom"}') is False

    def test_success_non_dict_json(self) -> None:
        assert _is_tool_success("[1, 2, 3]") is True

    def test_success_empty_string(self) -> None:
        assert _is_tool_success("") is True

    def test_success_invalid_json(self) -> None:
        assert _is_tool_success("{not json}") is True


# ---------------------------------------------------------------------------
# _normalize_tool_run_dir
# ---------------------------------------------------------------------------


class TestNormalizeToolRunDir:
    def test_injects_memory_run_dir_when_missing(self) -> None:
        args = {"path": "config.json"}
        out = _normalize_tool_run_dir(args, "/tmp/run_123")
        assert out["run_dir"] == "/tmp/run_123"

    def test_resolves_relative_dot_to_memory_run_dir(self) -> None:
        args = {"run_dir": "."}
        out = _normalize_tool_run_dir(args, "/tmp/run_123")
        assert out["run_dir"] == str(Path("/tmp/run_123").resolve())

    def test_resolves_relative_child_to_memory_run_dir(self) -> None:
        args = {"run_dir": "risk_parity_run"}
        out = _normalize_tool_run_dir(args, "/tmp/run_123")
        assert out["run_dir"] == str((Path("/tmp/run_123") / "risk_parity_run").resolve())

    def test_preserves_absolute_run_dir(self) -> None:
        # ``os.path.abspath`` produces a platform-correct absolute path: on
        # POSIX it stays ``/var/tmp/custom_run``; on Windows it becomes
        # ``C:\var\tmp\custom_run``. ``Path.is_absolute()`` only treats the
        # latter as absolute on Windows, so the bare Unix-style literal would
        # otherwise be classified as relative and resolved against
        # ``memory_run_dir`` — defeating the point of the test.
        absolute_run_dir = os.path.abspath("/var/tmp/custom_run")
        args = {"run_dir": absolute_run_dir}
        out = _normalize_tool_run_dir(args, "/tmp/run_123")
        assert out["run_dir"] == absolute_run_dir
