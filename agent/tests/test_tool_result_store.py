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


# ── 2026-09-04 review round 3 P0: tool-aware truncation ─────────────────────

from src.agent.tool_result_store import (  # noqa: E402
    SKILL_RESULT_LIMIT,
    build_skill_preview,
    disk_text,
)
from src.market_data import DEFAULT_MAX_ROWS, fetch_market_data_json  # noqa: E402


def _market_data_json(rows: int, symbols: tuple[str, ...] = ("000001.SZ",), *, max_rows=DEFAULT_MAX_ROWS) -> str:
    import numpy as np
    import pandas as pd

    rng = np.random.default_rng(0)

    def _frame():
        idx = pd.date_range("2025-01-01", periods=rows, freq="B")
        close = 100 + rng.standard_normal(rows).cumsum()
        df = pd.DataFrame(
            {
                "open": close + rng.standard_normal(rows) * 0.3,
                "high": close + abs(rng.standard_normal(rows)),
                "low": close - abs(rng.standard_normal(rows)),
                "close": close,
                "volume": rng.integers(1_000_000, 50_000_000, rows).astype(float),
                "amount": rng.random(rows) * 1e9,
            },
            index=idx,
        )
        df.index.name = "trade_date"
        return df

    class _Loader:
        def fetch(self, codes, start, end, interval="1D"):
            return {code: _frame() for code in codes}

    return fetch_market_data_json(
        codes=list(symbols), start_date="2025-01-01", end_date="2026-01-01",
        source="tushare", max_rows=max_rows, loader_resolver=lambda _s: _Loader,
    )


class TestMarketDataFitsTheBudget:
    def test_default_single_symbol_call_is_under_the_limit(self) -> None:
        """The P0: 250 indented records were 56k+; the default call must fit."""
        text = _market_data_json(250)  # a full trading year, default cap
        assert len(text) <= TOOL_RESULT_LIMIT, len(text)
        text = _market_data_json(120, max_rows=120)  # 7 columns, every row kept
        assert len(text) <= TOOL_RESULT_LIMIT, len(text)

    def test_payload_is_compact_and_rounded(self) -> None:
        text = _market_data_json(5, max_rows=5)
        assert "\n" not in text and ": " not in text
        table = json.loads(text)["000001.SZ"]
        assert table["columns"][0] == "trade_date"
        assert table["rows"][0][0] == "2025-01-01"  # no T00:00:00
        for value in table["rows"][0][1:]:
            assert isinstance(value, (int, float))
            assert value == round(value, 4)
        summary = table["summary"]
        assert summary["rows"] == 5 and summary["start"] == "2025-01-01"
        assert {"first_close", "last_close", "high", "low", "change_pct"} <= summary.keys()


class TestMarketDataStructuredTruncation:
    def test_oversized_result_keeps_summary_and_edge_rows(self, tmp_path: Path) -> None:
        raw = _market_data_json(250, max_rows=0)
        assert len(raw) > TOOL_RESULT_LIMIT
        payload, failed = prepare_for_context(
            raw, base_dir=tmp_path, iteration=1, tool_name="get_market_data", call_id="c1"
        )
        assert failed is False
        assert payload.startswith(TRUNCATED_TAG) and 'unit="rows"' in payload
        body = payload.split("\n", 1)[1].split("\n</tool-result-truncated>")[0]
        table = json.loads(body)["000001.SZ"]  # the preview is VALID JSON
        assert table["summary"]["rows"] == 250
        assert len(table["rows"]) == 40 and table["rows_omitted"] == 210
        full = json.loads(raw)["000001.SZ"]["rows"]
        assert table["rows"][:20] == full[:20] and table["rows"][-20:] == full[-20:]
        assert "grep_file" not in payload
        assert "read_file(" in payload and "offset=" in payload

    def test_disk_copy_is_one_bar_per_line(self, tmp_path: Path) -> None:
        raw = _market_data_json(250, max_rows=0)
        prepare_for_context(
            raw, base_dir=tmp_path, iteration=1, tool_name="get_market_data", call_id="c1"
        )
        on_disk = next((tmp_path / "tool-results").iterdir()).read_text(encoding="utf-8")
        assert json.loads(on_disk) == json.loads(raw)
        bar_lines = [line for line in on_disk.splitlines() if line.startswith('  ["2025-')]
        assert len(bar_lines) == 250

    def test_many_symbols_shrink_the_edge_before_falling_back(self, tmp_path: Path) -> None:
        raw = _market_data_json(120, tuple(f"{i:06d}.SZ" for i in range(8)), max_rows=120)
        payload, _ = prepare_for_context(
            raw, base_dir=tmp_path, iteration=1, tool_name="get_market_data", call_id="c1"
        )
        assert 'unit="rows"' in payload
        assert len(payload) <= TOOL_RESULT_LIMIT + 1_500  # body ≤ limit + pointer text

    def test_unparseable_market_data_falls_back_to_the_generic_envelope(self, tmp_path: Path) -> None:
        raw = "{" + "x" * (TOOL_RESULT_LIMIT * 2)
        payload, _ = prepare_for_context(
            raw, base_dir=tmp_path, iteration=1, tool_name="get_market_data", call_id="c1"
        )
        assert f'shown="{TOOL_RESULT_LIMIT}"' in payload


def _skill(sections: int, section_chars: int = 3_000) -> str:
    parts = ["# skill: big-skill\n\n# Big skill\n\nintro line\n\n"]
    for i in range(sections):
        parts.append(f"## Section {i:02d}\n\n" + ("lorem ipsum " * (section_chars // 12)) + "\n\n")
    return "".join(parts)


class TestLoadSkillExemption:
    def test_a_30k_skill_passes_through_untouched(self, tmp_path: Path) -> None:
        raw = _skill(10)
        assert TOOL_RESULT_LIMIT < len(raw) <= SKILL_RESULT_LIMIT
        payload, failed = prepare_for_context(
            raw, base_dir=tmp_path, iteration=1, tool_name="load_skill", call_id="c1"
        )
        assert payload == raw and failed is False
        assert not (tmp_path / "tool-results").exists()

    def test_a_100k_skill_is_trimmed_by_section(self, tmp_path: Path) -> None:
        raw = _skill(33)
        assert len(raw) > SKILL_RESULT_LIMIT
        payload, failed = prepare_for_context(
            raw, base_dir=tmp_path, iteration=1, tool_name="load_skill", call_id="c1"
        )
        assert failed is False
        assert payload.startswith(TRUNCATED_TAG) and 'unit="sections"' in payload
        assert len(payload) <= SKILL_RESULT_LIMIT
        assert "## Section 00" in payload and "## Section 32" not in payload.split("</tool-result-truncated>")[0]
        # Cut lands on a section boundary — the last kept section is complete.
        kept = payload.split("\n</tool-result-truncated>")[0]
        assert kept.rstrip().endswith("lorem ipsum")
        assert "[Omitted " in payload and '"## Section 32"' in payload
        # Pointer: on-disk Markdown + the exact line to resume from.
        on_disk = next((tmp_path / "tool-results").iterdir())
        assert on_disk.suffix == ".md" and on_disk.read_text(encoding="utf-8") == raw
        resume = int(payload.split("start at line ")[1].split(".")[0])
        assert raw.splitlines()[resume - 1].startswith("## Section ")
        assert "grep_file" not in payload

    def test_no_disk_points_at_the_bundled_skill_file(self) -> None:
        preview = build_skill_preview(_skill(33), None)
        assert 'read_file(path="big-skill/SKILL.md"' in preview

    def test_headings_inside_code_fences_do_not_split(self) -> None:
        raw = "# skill: x\n\n## Real\n\n```python\n## not a heading\n```\n" + "z" * SKILL_RESULT_LIMIT
        preview = build_skill_preview(raw, Path("/r/tool-results/001-load_skill-c.md"))
        assert '"## not a heading"' not in preview


class TestSingleLineJsonReflow:
    def test_single_line_json_is_pretty_printed_on_disk(self, tmp_path: Path) -> None:
        raw = json.dumps({"rows": [{"i": i, "v": "x" * 40} for i in range(500)]})
        assert "\n" not in raw and len(raw) > TOOL_RESULT_LIMIT
        payload, _ = prepare_for_context(
            raw, base_dir=tmp_path, iteration=1, tool_name="some_tool", call_id="c1"
        )
        on_disk = next((tmp_path / "tool-results").iterdir()).read_text(encoding="utf-8")
        assert json.loads(on_disk) == json.loads(raw)
        assert on_disk.count("\n") > 500
        # The preview still shows the ORIGINAL bytes (byte-stable head/tail).
        assert raw[:100] in payload

    def test_multiline_and_non_json_are_stored_verbatim(self) -> None:
        assert disk_text("t", '{\n"a": 1\n}') == '{\n"a": 1\n}'
        assert disk_text("t", "plain " * 10) == "plain " * 10
        assert disk_text("t", "{not json") == "{not json"


class TestEnvelopeWording:
    def test_generic_envelope_names_a_tool_that_exists(self) -> None:
        preview = build_preview(_big(), Path("/runs/r1/x.json"), "t")
        assert "grep_file" not in preview
        assert 'read_file(path="/runs/r1/x.json", offset=<start line>, limit=<line count>)' in preview
        assert "grep -n" in preview
