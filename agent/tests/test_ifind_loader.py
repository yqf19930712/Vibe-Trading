"""Tests for ifind_loader: query building and markdown-answer parsing.

Offline only — the live-response fixture is a captured 2026-08-25 INTC answer
(nested-JSON shape verified against the real MCP endpoint that day).
"""
import pandas as pd

from backtest.loaders.ifind_loader import (
    _build_query,
    _parse_answer_table,
    _parse_number,
    DataLoader,
)

# Captured (abridged) from a live global_stock_quotes call for INTC. Rows for
# non-trading days carry the previous close with the other cells as a literal
# tab; the first table is a currency summary that must be ignored.
SAMPLE_ANSWER = (
    "|证券代码|证券简称|交易币种|\n"
    "|---|---|---|\n"
    "|INTC.O|英特尔|USD|\n"
    "\n"
    "|证券代码|证券简称|日期|收盘价（单位：元）|开盘价（单位：元）|最低价（单位：元）|最高价（单位：元）|成交量|\n"
    "|---|---|---|---|---|---|---|---|\n"
    "|INTC.O|英特尔|20260825|87.26|\t|\t|\t|\t|\n"
    "|INTC.O|英特尔|20260824|87.26|88.86|85.14|88.91|9690.3359万|\n"
    "|INTC.O|英特尔|20260823|90.07|\t|\t|\t|\t|\n"
    "|INTC.O|英特尔|20260821|90.07|92.71|89.75|92.768|9167.0104万|\n"
    "|INTC.O|英特尔|20260819|92.8|97.41|91.28|98.2|1.1027亿|\n"
)


class TestParseNumber:
    def test_plain(self):
        assert _parse_number("87.26") == 87.26

    def test_wan(self):
        assert _parse_number("9690.3359万") == 9690.3359e4

    def test_yi(self):
        assert _parse_number("1.1027亿") == 1.1027e8

    def test_empty_and_placeholders(self):
        assert _parse_number("") is None
        assert _parse_number("\t") is None
        assert _parse_number("--") is None

    def test_thousands_separator(self):
        assert _parse_number("1,234.5") == 1234.5


class TestParseAnswerTable:
    def test_parses_price_table_and_skips_currency_table(self):
        df = _parse_answer_table(SAMPLE_ANSWER)
        assert df is not None
        # Non-trading carry-forward rows (0825/0823) dropped.
        assert len(df) == 3
        assert list(df.columns) == ["open", "high", "low", "close", "volume"]
        assert df.index.is_monotonic_increasing

    def test_row_values(self):
        df = _parse_answer_table(SAMPLE_ANSWER)
        row = df.loc[pd.Timestamp("2026-08-24")]
        assert row["open"] == 88.86
        assert row["high"] == 88.91
        assert row["low"] == 85.14
        assert row["close"] == 87.26
        assert row["volume"] == 9690.3359e4

    def test_no_price_table(self):
        assert _parse_answer_table("没有查询到相关数据") is None
        assert _parse_answer_table("|a|b|\n|---|---|\n|1|2|\n") is None


class TestBuildQuery:
    def test_us(self):
        q = _build_query("INTC.US", "2026-06-01", "2026-08-25")
        assert q is not None
        assert "美股INTC" in q
        assert "20260601-20260825" in q

    def test_hk_strips_leading_zeros(self):
        q = _build_query("03690.HK", "2026-06-01", "2026-08-25")
        assert q is not None
        assert "港股3690" in q

    def test_bare_us_ticker(self):
        # attempt f9b0c0cdcded: bare "AVGO" must not no-op the loader.
        q = _build_query("AVGO", "2026-06-01", "2026-08-25")
        assert q is not None
        assert "美股AVGO" in q

    def test_unsupported_market(self):
        assert _build_query("600036.SH", "2026-06-01", "2026-08-25") is None
        assert _build_query("BTC-USDT", "2026-06-01", "2026-08-25") is None
        assert _build_query("GC=F", "2026-06-01", "2026-08-25") is None
        assert _build_query("^TNX", "2026-06-01", "2026-08-25") is None


class TestLoader:
    def test_unavailable_without_token(self, monkeypatch):
        monkeypatch.delenv("IFIND_MCP_TOKEN", raising=False)
        assert DataLoader().is_available() is False

    def test_available_with_token(self, monkeypatch):
        monkeypatch.setenv("IFIND_MCP_TOKEN", "tok")
        assert DataLoader().is_available() is True

    def test_non_daily_interval_returns_empty(self, monkeypatch):
        monkeypatch.setenv("IFIND_MCP_TOKEN", "tok")
        out = DataLoader().fetch(
            ["INTC.US"], "2026-06-01", "2026-08-25", interval="5m"
        )
        assert out == {}
