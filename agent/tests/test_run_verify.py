"""Tests for the zero-LLM finalization verifier (batch F, F1)."""

from __future__ import annotations

import json
from pathlib import Path

from src.agent.verify import (
    extract_reference_prices,
    verify_final_text_prices,
    verify_metrics_csv,
    verify_run,
)


def _write_metrics(tmp_path: Path, header: str, row: str) -> Path:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "metrics.csv").write_text(f"{header}\n{row}\n", encoding="utf-8")
    return tmp_path


class TestVerifyMetricsCsv:
    def test_missing_file_is_clean(self, tmp_path) -> None:
        assert verify_metrics_csv(tmp_path) == []

    def test_plausible_metrics_pass(self, tmp_path) -> None:
        _write_metrics(
            tmp_path,
            "final_value,total_return,sharpe,max_drawdown,win_rate,trade_count",
            "120000,0.2,1.5,-0.12,0.55,42",
        )
        assert verify_metrics_csv(tmp_path) == []

    def test_out_of_range_sharpe_flagged(self, tmp_path) -> None:
        _write_metrics(tmp_path, "total_return,sharpe", "0.3,412.0")
        warnings = verify_metrics_csv(tmp_path)
        assert len(warnings) == 1
        assert warnings[0]["code"] == "out_of_range"
        assert warnings[0]["field"] == "sharpe"

    def test_unparseable_value_flagged(self, tmp_path) -> None:
        _write_metrics(tmp_path, "total_return,sharpe", "N/A,1.0")
        warnings = verify_metrics_csv(tmp_path)
        assert len(warnings) == 1
        assert warnings[0]["code"] == "unparseable"
        assert warnings[0]["field"] == "total_return"

    def test_empty_file_flagged(self, tmp_path) -> None:
        artifacts = tmp_path / "artifacts"
        artifacts.mkdir(parents=True)
        (artifacts / "metrics.csv").write_text("total_return,sharpe\n", encoding="utf-8")
        warnings = verify_metrics_csv(tmp_path)
        assert warnings and warnings[0]["code"] == "empty"


class TestExtractReferencePrices:
    def test_realtime_quotes_shape(self) -> None:
        raw = json.dumps({
            "status": "ok",
            "quotes": [
                {"symbol": "NVDA.US", "last_price": 181.5, "prev_close": 180.0},
                {"symbol": "INTC.US", "last_price": None},
            ],
        })
        refs = extract_reference_prices([("get_realtime_quotes", raw)])
        assert refs == {"NVDA.US": 181.5}

    def test_market_data_shape_uses_last_close(self) -> None:
        raw = json.dumps({
            "AAPL.US": [
                {"close": 210.0, "open": 208.0},
                {"close": 214.5, "open": 211.0},
            ],
            "_gaps": [{"code": "X", "reason": "no data"}],
        })
        refs = extract_reference_prices([("get_market_data", raw)])
        assert refs == {"AAPL.US": 214.5}

    def test_market_data_compact_table_uses_last_close(self) -> None:
        """The 2026-09 compact ``{"columns", "rows"}`` table shape."""
        raw = json.dumps({
            "AAPL.US": {
                "summary": {"rows": 2, "last_close": 214.5},
                "columns": ["trade_date", "open", "close"],
                "rows": [["2026-01-02", 208.0, 210.0], ["2026-01-05", 211.0, 214.5]],
            },
            "_unresolved": ["X"],
        })
        refs = extract_reference_prices([("get_market_data", raw)])
        assert refs == {"AAPL.US": 214.5}

    def test_garbage_results_ignored(self) -> None:
        assert extract_reference_prices([("get_market_data", "not json")]) == {}


class TestVerifyFinalTextPrices:
    def test_divergent_price_flagged(self) -> None:
        text = "NVDA 当前股价约 300 美元，建议买入。"
        warnings = verify_final_text_prices(text, {"NVDA.US": 181.5})
        assert len(warnings) == 1
        assert warnings[0]["code"] == "price_divergence"
        assert warnings[0]["symbol"] == "NVDA.US"

    def test_matching_price_clean(self) -> None:
        text = "NVDA closed at 182.3 today, up 0.4%."
        assert verify_final_text_prices(text, {"NVDA.US": 181.5}) == []

    def test_non_price_numbers_ignored(self) -> None:
        # 2026 (year) and 3.5% are outside the plausible band around 181.5.
        text = "NVDA 在 2026 年的数据中心收入增长 3.5%。"
        assert verify_final_text_prices(text, {"NVDA.US": 181.5}) == []


class TestVerifyRun:
    def test_combined_and_never_raises(self, tmp_path) -> None:
        _write_metrics(tmp_path, "sharpe", "999")
        raw = json.dumps({"quotes": [{"symbol": "NVDA.US", "last_price": 181.5}]})
        warnings = verify_run(
            tmp_path,
            "NVDA price target 300 based on backtest",
            [("get_realtime_quotes", raw)],
        )
        codes = {w["code"] for w in warnings}
        assert "out_of_range" in codes
        assert "price_divergence" in codes

    def test_bad_inputs_return_empty(self, tmp_path) -> None:
        # None final text / broken grounding payloads must not raise.
        assert verify_run(tmp_path, "", [("get_market_data", "{broken")]) == []
