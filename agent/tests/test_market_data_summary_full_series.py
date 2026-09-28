"""get_market_data's summary describes the whole period, not the sample.

A series longer than ``max_rows`` is thinned to every Nth bar; the period
high / low used to be taken from those sampled bars, so an extreme that fell
between two sampled bars simply vanished while the summary still presented
itself as the range high / low.
"""

from __future__ import annotations

import pandas as pd

from src.market_data import cap_rows, fetch_market_data, to_table


def _series(n: int = 243, spikes: dict[int, tuple[float, float]] | None = None) -> pd.DataFrame:
    idx = pd.bdate_range("2025-01-02", periods=n)
    close = [100.0] * n
    high = [101.0] * n
    low = [99.0] * n
    for i, (h, l) in (spikes or {}).items():
        high[i], low[i] = h, l
    df = pd.DataFrame({"open": close, "high": high, "low": low, "close": close, "volume": 1.0}, index=idx)
    df.index.name = "trade_date"
    return df


def _loader(df: pd.DataFrame):
    class _L:
        def fetch(self, codes, start, end, interval="1D"):
            return {codes[0]: df}

    return _L


def test_extremes_between_sampled_bars_are_reported() -> None:
    # 243 bars, stride 3: bars 100 and 200 are not sampled (100 % 3 == 1).
    df = _series(spikes={100: (151.0, 99.0), 200: (101.0, 69.0)})

    out = fetch_market_data(
        codes=["X.US"], start_date="2025-01-01", end_date="2026-01-01",
        source="yfinance", loader_resolver=lambda _src: _loader(df),
    )

    table = out["X.US"]
    assert table["truncated"] is True
    summary = table["summary"]
    assert summary["high"] == 151.0
    assert summary["low"] == 69.0
    # rows = what was returned; total_rows = the full series.
    assert summary["rows"] == len(table["rows"]) < 243
    assert summary["total_rows"] == 243
    # the spikes really are absent from the sampled rows
    hi = table["columns"].index("high")
    assert max(r[hi] for r in table["rows"]) == 101.0


def test_uncapped_series_summary_is_unchanged() -> None:
    records = [
        {"trade_date": f"2025-01-0{i}", "high": h, "low": l, "close": c}
        for i, (h, l, c) in enumerate([(11, 9, 10), (13, 8, 12), (12, 10, 11)], start=1)
    ]
    summary = to_table(cap_rows(records, 120), full=records)["summary"]
    assert summary == {
        "rows": 3, "start": "2025-01-01", "end": "2025-01-03",
        "first_close": 10.0, "last_close": 11.0, "change_pct": 10.0,
        "high": 13.0, "low": 8.0,
    }


def test_to_table_without_full_keeps_the_legacy_behaviour() -> None:
    # stride 4 samples bars 0, 4, 8 and the pinned last bar 9 — not the spike at 5
    records = [{"d": i, "close": 9.0 if i == 5 else 1.0} for i in range(10)]
    capped = cap_rows(records, 3)
    assert to_table(capped)["summary"]["high"] == 1.0
    assert to_table(capped, full=records)["summary"]["high"] == 9.0
