"""V2: oversized tool results must be offloaded with an EXPLICIT truncation mark.

The pre-V2 behavior was ``result[:10_000]`` with nothing appended. A model
handed the first 10k characters of a 40k-character JSON document had no way to
know anything was missing — it parsed the prefix and reported the absent rows
as data the source did not have.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.agent.context_policy import TRUNCATED_TAG
from src.agent.tool_result_store import (
    PREVIEW_TAIL,
    TOOL_RESULT_LIMIT,
    build_preview,
    offload,
    prepare_for_context,
    result_path,
)


def _big(n: int = TOOL_RESULT_LIMIT * 3) -> str:
    return "HEAD" + "x" * (n - 8) + "TAIL"


class TestPassThrough:
    def test_small_results_are_untouched(self, tmp_path: Path) -> None:
        payload, failed = prepare_for_context(
            "small", base_dir=tmp_path, iteration=1, tool_name="t", call_id="c"
        )
        assert payload == "small"
        assert failed is False
        assert not (tmp_path / "tool-results").exists()

    def test_exactly_at_the_limit_is_not_truncated(self, tmp_path: Path) -> None:
        text = "y" * TOOL_RESULT_LIMIT
        payload, failed = prepare_for_context(
            text, base_dir=tmp_path, iteration=1, tool_name="t", call_id="c"
        )
        assert payload == text
        assert failed is False


class TestExplicitTruncationMarker:
    def test_preview_carries_a_machine_readable_truncation_tag(self, tmp_path: Path) -> None:
        raw = _big()
        payload, failed = prepare_for_context(
            raw, base_dir=tmp_path, iteration=7, tool_name="get_market_data",
            call_id="call_abcdef123456",
        )

        assert failed is False
        # The marker Layer 2 also keys its skip rule on.
        assert payload.startswith(TRUNCATED_TAG)
        assert f'total_chars="{len(raw)}"' in payload
        assert f'shown="{TOOL_RESULT_LIMIT}"' in payload
        assert "</tool-result-truncated>" in payload

    def test_preview_keeps_head_and_tail_and_names_the_omission(self) -> None:
        raw = _big()
        preview = build_preview(raw, Path("/runs/r1/tool-results/007-t-abc.json"), "t")

        assert "HEAD" in preview
        assert "TAIL" in preview  # the tail is where conclusions/totals live
        assert f"[{len(raw) - TOOL_RESULT_LIMIT} chars omitted from the middle]" in preview

    def test_preview_tells_the_model_not_to_parse_it(self) -> None:
        preview = build_preview(_big(), Path("/runs/r1/x.json"), "t")
        assert "do NOT parse it as a whole" in preview
        assert "NOT conclude data is missing from the source" in preview
        assert "read_file(" in preview

    def test_preview_points_at_the_file_that_was_actually_written(
        self, tmp_path: Path
    ) -> None:
        raw = _big()
        payload, _ = prepare_for_context(
            raw, base_dir=tmp_path, iteration=3, tool_name="backtest", call_id="cid12345678"
        )
        expected = result_path(tmp_path, 3, "backtest", "cid12345678", raw)

        assert str(expected) in payload
        assert expected.read_text(encoding="utf-8") == raw

    def test_json_and_text_results_get_the_right_extension(self, tmp_path: Path) -> None:
        j = offload(tmp_path, 1, "t", "cid", '{"a": 1}')
        t = offload(tmp_path, 1, "u", "cid", "plain text")
        assert j.suffix == ".json"
        assert t.suffix == ".txt"


class TestByteStability:
    def test_same_call_produces_the_same_path_and_bytes(self, tmp_path: Path) -> None:
        """Book §2.3.4: a replay must not shift the cache-invalidation point."""
        raw = _big()
        first, _ = prepare_for_context(
            raw, base_dir=tmp_path, iteration=4, tool_name="t", call_id="cid12345678"
        )
        second, _ = prepare_for_context(
            raw, base_dir=tmp_path, iteration=4, tool_name="t", call_id="cid12345678"
        )
        assert first == second
        assert "tool-results" in first

    def test_filename_has_no_timestamp_or_random_component(self, tmp_path: Path) -> None:
        p = result_path(tmp_path, 12, "get_market_data", "call_deadbeefcafe", "{}")
        assert p.name == "012-get_market_data-call_dea.json"

    def test_unsafe_tool_names_are_sanitized(self, tmp_path: Path) -> None:
        p = result_path(tmp_path, 1, "remote/../etc", "cid", "{}")
        assert "/" not in p.name
        assert ".." not in p.name


class TestStorageFailureDegrades:
    def test_a_failed_write_still_marks_the_truncation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A full tenant volume must not silently hand back an unmarked cut."""

        def _boom(*_args, **_kwargs):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(Path, "write_text", _boom)
        payload, failed = prepare_for_context(
            _big(), base_dir=tmp_path, iteration=1, tool_name="t", call_id="c"
        )

        assert failed is True
        assert payload.startswith(TRUNCATED_TAG)
        assert "could NOT be written to disk" in payload
        assert "FULL RESULT ON DISK" not in payload

    def test_no_base_dir_degrades_the_same_way(self) -> None:
        payload, failed = prepare_for_context(
            _big(), base_dir=None, iteration=1, tool_name="t", call_id="c"
        )
        assert failed is True
        assert payload.startswith(TRUNCATED_TAG)


class TestPreviewBudget:
    def test_the_visible_slice_matches_the_declared_limit(self) -> None:
        raw = _big()
        preview = build_preview(raw, None, "t")
        # head + tail == the shown budget advertised in the envelope.
        assert raw[: TOOL_RESULT_LIMIT - PREVIEW_TAIL] in preview
        assert raw[-PREVIEW_TAIL:] in preview
