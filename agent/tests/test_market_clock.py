"""The prompt's "now" is Beijing + US Eastern time, never the container clock.

The engine container runs on UTC; a bare ``datetime.now()`` turned 07:30
Beijing time into the previous day. The status bar and the swarm worker
prompt render the same explicit lines from :mod:`src.core.market_clock`.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

import src.core.market_clock as clock_mod
from src.core.market_clock import clock_lines, market_session, to_eastern


def _utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def test_early_beijing_morning_is_already_the_next_day() -> None:
    # 2026-09-23 23:30 UTC == 2026-09-24 07:30 Beijing (Thursday).
    now_line, markets = clock_lines(_utc(2026, 9, 23, 23, 30))
    assert now_line.startswith("Now: 2026-09-24 07:30 Beijing time (UTC+8, Thursday)")
    assert "US Eastern 2026-09-23 19:30 (EDT, Wednesday)" in now_line
    assert "A-shares pre-open, latest weekday session 2026-09-23 (Wed)" in markets
    assert "HK pre-open, latest weekday session 2026-09-23 (Wed)" in markets
    assert "US closed for the day, latest weekday session 2026-09-23 (Wed)" in markets
    assert "holidays are not checked" in markets
    assert "(local)" not in now_line


@pytest.mark.parametrize(
    ("utc", "expected"),
    [
        (_utc(2026, 9, 24, 2, 0), "A-shares open"),               # 10:00 Beijing
        (_utc(2026, 9, 24, 4, 0), "A-shares midday break"),       # 12:00
        (_utc(2026, 9, 24, 8, 0), "A-shares closed for the day"), # 16:00
        (_utc(2026, 9, 26, 2, 0), "A-shares closed (weekend)"),   # Saturday
    ],
)
def test_a_share_session_states(utc: datetime, expected: str) -> None:
    assert expected in clock_lines(utc)[1]


def test_monday_pre_open_points_at_friday() -> None:
    state, latest = market_session(
        datetime(2026, 9, 28, 8, 0), ((clock_mod.time(9, 30), clock_mod.time(15, 0)),)
    )
    assert state == "pre-open"
    assert latest.isoformat() == "2026-09-25"


@pytest.mark.parametrize(
    ("utc", "offset_h"),
    [
        (_utc(2026, 3, 8, 6, 59), -5),   # just before DST starts
        (_utc(2026, 3, 8, 7, 0), -4),    # DST starts 02:00 EST
        (_utc(2026, 11, 1, 5, 59), -4),  # just before DST ends
        (_utc(2026, 11, 1, 6, 0), -5),   # DST ends 02:00 EDT
        (_utc(2026, 1, 15, 12, 0), -5),
        (_utc(2026, 7, 15, 12, 0), -4),
    ],
)
def test_eastern_rule_fallback_matches_dst(monkeypatch, utc: datetime, offset_h: int) -> None:
    monkeypatch.setattr(clock_mod, "_eastern_zone", lambda: None)
    assert to_eastern(utc).utcoffset().total_seconds() == offset_h * 3600


def test_status_bar_and_worker_prompt_use_the_same_clock(monkeypatch) -> None:
    from src.agent.loop import _build_status_message
    from src.swarm.models import SwarmAgentSpec
    from src.swarm.worker import build_worker_prompt

    fixed = ["Now: FIXED-CLOCK", "Markets: FIXED"]
    monkeypatch.setattr("src.agent.loop.clock_lines", lambda: list(fixed))
    monkeypatch.setattr("src.swarm.worker.clock_lines", lambda: list(fixed))

    status = _build_status_message("(empty state)", [])["content"]
    assert "Now: FIXED-CLOCK\nMarkets: FIXED" in status

    spec = SwarmAgentSpec(id="a", role="r", system_prompt="s", tools=[], skills=[])
    prompt = build_worker_prompt(spec, {}, "")
    assert prompt.endswith("## Current Date & Time\n\nNow: FIXED-CLOCK\nMarkets: FIXED")
