"""V2: the three compression layers must share one protection rule source.

Before ``src.agent.context_policy`` existed, Layer 2 folded the middle out of
exactly the results Layer 1 refuses to prune, and out of the handoff summary
Layer 3 pays an LLM call to produce. These tests pin the graded policy: which
messages Layer 2 must not touch, and which ones fold under a looser rule
rather than being exempted outright (a blanket exemption would make the first
user message — the largest one in a long session — permanently uncompressible).
"""

from __future__ import annotations

from src.agent.context_policy import (
    CLEARED_PREFIX,
    DEFAULT,
    FIRST_USER,
    HANDOFF_PREFIX,
    PROTECTED_HARD_CAP,
    PROTECTED_TOOLS,
    STATUS_PREFIX,
    TRUNCATED_TAG,
    collapse_rule,
    first_user_index,
    is_prunable_by_microcompact,
)
from src.agent.loop import (
    COLLAPSE_HEAD,
    COLLAPSE_PRESERVE_RECENT,
    COLLAPSE_TAIL,
    COLLAPSE_TEXT_MIN,
    MICROCOMPACT_PROTECTED_TOOLS,
    _context_collapse,
)


class TestGradedRules:
    def test_default_matches_the_historic_collapse_constants(self) -> None:
        """The ordinary path must not change behavior versus pre-V2."""
        assert DEFAULT.min_chars == COLLAPSE_TEXT_MIN
        assert DEFAULT.head == COLLAPSE_HEAD
        assert DEFAULT.tail == COLLAPSE_TAIL
        assert DEFAULT.skip is False

    def test_first_user_is_graded_not_exempt(self) -> None:
        """The first user message folds later and keeps more — but it folds.

        This is the whole point of the graded design: an outright ``skip``
        here would leave the biggest message in the trajectory permanently
        uncompressible.
        """
        assert FIRST_USER.skip is False
        assert FIRST_USER.min_chars > DEFAULT.min_chars
        assert FIRST_USER.head > DEFAULT.head
        assert FIRST_USER.tail > DEFAULT.tail

    def test_first_user_message_gets_the_first_user_rule(self) -> None:
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "q" * 5000},
            {"role": "user", "content": "q" * 5000},
        ]
        idx = first_user_index(msgs)
        assert idx == 1
        assert collapse_rule(msgs[1], index=1, first_user_index=idx) is FIRST_USER
        assert collapse_rule(msgs[2], index=2, first_user_index=idx) is DEFAULT

    def test_first_user_index_skips_a_handoff_summary_in_slot_one(self) -> None:
        """After Layer 3, slot 1 holds the summary — itself skipped by prefix."""
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": f"{HANDOFF_PREFIX} …]\n\nsummary"},
            {"role": "user", "content": "the real request"},
        ]
        # The summary IS a user message, so it takes index 1 …
        assert first_user_index(msgs) == 1
        # … but it is skipped by prefix regardless of the graded rule.
        assert collapse_rule(msgs[1], index=1, first_user_index=1).skip is True


class TestSkipRules:
    def test_marker_prefixes_are_skipped(self) -> None:
        for prefix in (CLEARED_PREFIX, HANDOFF_PREFIX, STATUS_PREFIX, TRUNCATED_TAG):
            msg = {"role": "user", "content": prefix + "x" * 50_000}
            assert collapse_rule(msg, index=5, first_user_index=1).skip is True, prefix

    def test_protected_tool_results_are_not_blind_folded(self) -> None:
        for name in PROTECTED_TOOLS:
            msg = {"role": "tool", "name": name, "content": "x" * 12_000}
            rule = collapse_rule(msg, index=5, first_user_index=1)
            assert rule is PROTECTED_HARD_CAP
            # 12k is under the escape-valve threshold, so nothing folds.
            assert len(msg["content"]) <= rule.min_chars

    def test_pathological_protected_result_still_has_an_escape_valve(self) -> None:
        """A protected result nobody can shrink must not be able to overflow."""
        msg = {"role": "tool", "name": "backtest", "content": "x" * 100_000}
        rule = collapse_rule(msg, index=5, first_user_index=1)
        assert rule.skip is False
        assert len(msg["content"]) > rule.min_chars

    def test_non_string_content_is_skipped(self) -> None:
        msg = {"role": "assistant", "content": None, "tool_calls": [{"id": "a"}]}
        assert collapse_rule(msg, index=3, first_user_index=1).skip is True

    def test_microcompact_and_layer2_read_the_same_protected_list(self) -> None:
        assert MICROCOMPACT_PROTECTED_TOOLS is PROTECTED_TOOLS
        for name in PROTECTED_TOOLS:
            assert is_prunable_by_microcompact({"role": "tool", "name": name}) is False
        assert is_prunable_by_microcompact({"role": "tool", "name": "read_file"}) is True


class TestLayer2HonoursThePolicy:
    def _pad(self, messages: list) -> list:
        """Append enough recent messages that the ones under test are foldable."""
        return messages + [
            {"role": "user", "content": f"recent {i}"}
            for i in range(COLLAPSE_PRESERVE_RECENT + 1)
        ]

    def test_protected_tool_result_survives_layer_2_byte_for_byte(self) -> None:
        """The P1-1 regression: L2 used to cut the middle out of grounding data."""
        payload = '{"prices": [' + ",".join(str(i) for i in range(2000)) + "]}"
        assert len(payload) > COLLAPSE_TEXT_MIN
        messages = self._pad(
            [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "q"},
                {"role": "tool", "name": "get_market_data", "content": payload},
            ]
        )

        _context_collapse(messages)

        assert messages[2]["content"] == payload
        assert "collapsed" not in messages[2]["content"]

    def test_handoff_summary_survives_layer_2_byte_for_byte(self) -> None:
        summary = f"{HANDOFF_PREFIX} — handoff summary.]\n\n" + "d" * 8000
        messages = self._pad(
            [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "q"},
                {"role": "user", "content": summary},
            ]
        )

        _context_collapse(messages)

        assert messages[2]["content"] == summary

    def test_truncation_envelope_is_not_folded_again(self) -> None:
        """A tool result already replaced by an explicit preview stays intact."""
        envelope = f'{TRUNCATED_TAG} tool="x" total_chars="1">\n' + "y" * 9000
        messages = self._pad(
            [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "q"},
                {"role": "tool", "name": "read_file", "content": envelope},
            ]
        )

        _context_collapse(messages)

        assert messages[2]["content"] == envelope

    def test_ordinary_long_message_still_folds(self) -> None:
        """The policy must not turn Layer 2 into a no-op."""
        long_text = "z" * (COLLAPSE_TEXT_MIN + 2000)
        messages = self._pad(
            [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "first user, short"},
                {"role": "tool", "name": "read_file", "content": long_text},
            ]
        )

        _context_collapse(messages)

        assert "collapsed" in messages[2]["content"]
        assert len(messages[2]["content"]) < len(long_text)

    def test_first_user_message_folds_only_past_its_own_threshold(self) -> None:
        under = "u" * (FIRST_USER.min_chars - 100)
        over = "o" * (FIRST_USER.min_chars + 2000)

        msgs_under = self._pad(
            [{"role": "system", "content": "sys"}, {"role": "user", "content": under}]
        )
        _context_collapse(msgs_under)
        assert msgs_under[1]["content"] == under

        msgs_over = self._pad(
            [{"role": "system", "content": "sys"}, {"role": "user", "content": over}]
        )
        _context_collapse(msgs_over)
        assert "collapsed" in msgs_over[1]["content"]
        assert msgs_over[1]["content"].startswith("o" * FIRST_USER.head)
