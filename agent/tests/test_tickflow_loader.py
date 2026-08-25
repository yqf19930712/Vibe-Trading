"""Tests for tickflow_loader: payload framing and symbol/interval gating.

Offline only — the fixture is a captured 2026-08-25 INTC response (column-
oriented shape verified against the live endpoint that day).
"""
import pandas as pd

from backtest.loaders.tickflow_loader import _to_frame, _to_ms, DataLoader

# Captured live: GET /v1/klines?symbol=INTC.US&period=1d&count=5&adjust=forward
# Timestamps are 04:00 UTC of the trade date (US midnight ET, DST).
SAMPLE_PAYLOAD = {
    "data": {
        "timestamp": [1787025600000, 1787112000000, 1787198400000, 1787284800000, 1787544000000],
        "open": [99.22, 97.41, 91.87, 92.71, 88.86],
        "high": [99.31, 98.2, 92.68, 92.77, 88.91],
        "low": [95.25, 91.28, 89.85, 89.75, 85.14],
        "close": [96.69, 92.8, 92.13, 90.07, 87.26],
        "volume": [121969500, 110268600, 85654700, 91192100, 96903359],
        "amount": [0.0, 0.0, 0.0, 0.0, 0.0],
    }
}


class TestToFrame:
    def test_shapes_and_columns(self):
        df = _to_frame(SAMPLE_PAYLOAD)
        assert df is not None
        assert len(df) == 5
        assert list(df.columns) == ["open", "high", "low", "close", "volume"]
        assert df.index.is_monotonic_increasing

    def test_utc_date_is_trade_date(self):
        df = _to_frame(SAMPLE_PAYLOAD)
        # 1787284800000 ms = 2026-08-21 04:00 UTC -> trade date 2026-08-21.
        row = df.loc[pd.Timestamp("2026-08-21")]
        assert row["open"] == 92.71
        assert row["close"] == 90.07
        assert row["volume"] == 91192100

    def test_empty_payload(self):
        assert _to_frame({"data": {"timestamp": []}}) is None
        assert _to_frame({}) is None

    def test_ragged_columns_rejected(self):
        bad = {"data": {"timestamp": [1, 2], "open": [1.0], "high": [1.0, 2.0],
                        "low": [1.0, 2.0], "close": [1.0, 2.0], "volume": [1, 2]}}
        assert _to_frame(bad) is None


class TestToMs:
    def test_utc_midnight(self):
        import datetime

        expected = int(
            datetime.datetime(2026, 8, 21, tzinfo=datetime.timezone.utc).timestamp() * 1000
        )
        assert _to_ms("2026-08-21") == expected
        assert _to_ms("1970-01-01") == 0
        assert _to_ms("1970-01-02") == 86_400_000


class TestLoader:
    def test_unavailable_without_key(self, monkeypatch):
        monkeypatch.delenv("TICKFLOW_API_KEY", raising=False)
        assert DataLoader().is_available() is False

    def test_available_with_key(self, monkeypatch):
        monkeypatch.setenv("TICKFLOW_API_KEY", "tk_x")
        assert DataLoader().is_available() is True

    def test_non_daily_interval_returns_empty(self, monkeypatch):
        monkeypatch.setenv("TICKFLOW_API_KEY", "tk_x")
        out = DataLoader().fetch(["INTC.US"], "2026-06-01", "2026-08-25", interval="5m")
        assert out == {}

    def test_non_us_symbol_skipped(self, monkeypatch):
        monkeypatch.setenv("TICKFLOW_API_KEY", "tk_x")
        loader = DataLoader()
        assert loader._fetch_one("600036.SH", "2026-06-01", "2026-08-25") is None


class TestToUsSymbol:
    def test_suffixed_passthrough(self):
        from backtest.loaders.tickflow_loader import _to_us_symbol

        assert _to_us_symbol("INTC.US") == "INTC.US"
        assert _to_us_symbol("intc.us") == "INTC.US"

    def test_bare_ticker_normalized(self):
        from backtest.loaders.tickflow_loader import _to_us_symbol

        # attempt f9b0c0cdcded: bare "AVGO" must not no-op the loader.
        assert _to_us_symbol("AVGO") == "AVGO.US"
        assert _to_us_symbol("SPY") == "SPY.US"

    def test_non_us_rejected(self):
        from backtest.loaders.tickflow_loader import _to_us_symbol

        assert _to_us_symbol("600036.SH") is None
        assert _to_us_symbol("03690.HK") is None
        assert _to_us_symbol("GC=F") is None
        assert _to_us_symbol("^TNX") is None
        assert _to_us_symbol("BTC-USDT") is None
