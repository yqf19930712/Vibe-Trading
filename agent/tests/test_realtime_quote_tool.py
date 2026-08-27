"""Tests for the TickFlow realtime quote tool (offline: HTTP layer mocked)."""

from __future__ import annotations

import json
from unittest.mock import patch

from src.tools.realtime_quote_tool import RealtimeQuoteTool, _to_us_symbol

# Captured live 2026-08-26 from GET /v1/quotes?symbols=INTC.US (abridged).
SAMPLE_QUOTE = {
    "symbol": "INTC.US",
    "region": "US",
    "last_price": 87.48,
    "prev_close": 87.26,
    "open": 90.06,
    "high": 90.18,
    "low": 87.29,
    "volume": 81395037,
    "amount": 7196938076.136,
    "timestamp": 1787688001000,
    "ext": {"type": "us_equity", "name": "英特尔", "change_pct": 0.00252, "change_amount": 0.22},
}


class TestToUsSymbol:
    def test_forms(self):
        assert _to_us_symbol("INTC.US") == "INTC.US"
        assert _to_us_symbol("avgo") == "AVGO.US"
        assert _to_us_symbol("600036.SH") is None
        assert _to_us_symbol("03690.HK") is None


class TestExecute:
    def test_unavailable_without_key(self, monkeypatch):
        monkeypatch.delenv("TICKFLOW_API_KEY", raising=False)
        assert RealtimeQuoteTool().check_available() is False
        out = json.loads(RealtimeQuoteTool().execute(codes=["INTC.US"]))
        assert out["status"] == "error"

    def test_quotes_and_unsupported(self, monkeypatch):
        monkeypatch.setenv("TICKFLOW_API_KEY", "tk_x")
        with patch(
            "src.tools.realtime_quote_tool._http_get_quotes",
            return_value=[SAMPLE_QUOTE],
        ) as mock_get:
            out = json.loads(
                RealtimeQuoteTool().execute(codes=["INTC", "600036.SH"])
            )
        assert out["status"] == "ok"
        assert out["quotes"][0]["symbol"] == "INTC.US"
        assert out["quotes"][0]["name"] == "英特尔"
        assert out["quotes"][0]["change_pct"] == 0.00252
        assert out["unsupported_non_us"] == ["600036.SH"]
        # bare INTC normalized to INTC.US before the HTTP call
        assert mock_get.call_args[0][0] == ["INTC.US"]

    def test_batching_five_per_request(self, monkeypatch):
        monkeypatch.setenv("TICKFLOW_API_KEY", "tk_x")
        codes = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG"]  # 7 -> 2 batches
        with patch(
            "src.tools.realtime_quote_tool._http_get_quotes", return_value=[]
        ) as mock_get:
            out = json.loads(RealtimeQuoteTool().execute(codes=codes))
        assert mock_get.call_count == 2
        assert len(mock_get.call_args_list[0][0][0]) == 5
        assert len(mock_get.call_args_list[1][0][0]) == 2
        assert out["status"] == "error"  # nothing returned
        assert len(out["missing"]) == 7

    def test_batch_error_degrades(self, monkeypatch):
        monkeypatch.setenv("TICKFLOW_API_KEY", "tk_x")
        with patch(
            "src.tools.realtime_quote_tool._http_get_quotes",
            side_effect=RuntimeError("tickflow quotes HTTP 429: slow down"),
        ):
            out = json.loads(RealtimeQuoteTool().execute(codes=["INTC.US"]))
        assert out["status"] == "error"
        assert "429" in out["errors"][0]

    def test_repeatable_flag(self):
        # Quotes are volatile — the duplicate guard must never block re-calls.
        assert RealtimeQuoteTool.repeatable is True
