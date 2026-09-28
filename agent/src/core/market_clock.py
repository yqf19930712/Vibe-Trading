"""Explicit "now" for prompts: Beijing time, US Eastern time, market sessions.

The engine container runs on UTC and its users are in mainland China: a bare
``datetime.now()`` renders 07:30 Beijing time as the previous day's 23:30, and
"today" in a prompt is then wrong for eight hours of every day. The model is
therefore told the time in the zones that matter — Beijing (UTC+8, also Hong
Kong's zone) and US Eastern — plus where each market's regular session stands
and which weekday session is the latest one.

Deliberately NOT a process-wide ``TZ`` change: data loaders and caches key on
``date.today()`` and must keep their current meaning.

Session state is computed from regular hours and weekdays only; exchange
holidays are not checked, and the rendered line says so, so the model verifies
the latest trading day against real data (``get_market_data``) instead of
trusting it blindly. US Eastern uses the system tz database when present and
falls back to the US daylight-saving rule (second Sunday of March to first
Sunday of November, 02:00 local) otherwise.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Optional

BEIJING = timezone(timedelta(hours=8), "UTC+8")
_EST = timezone(timedelta(hours=-5), "EST")
_EDT = timezone(timedelta(hours=-4), "EDT")

# (label, zone key, regular sessions in local time). Zone key "beijing"
# covers Hong Kong as well (both UTC+8, no daylight saving).
_MARKETS: tuple[tuple[str, str, tuple[tuple[time, time], ...]], ...] = (
    ("A-shares", "beijing", ((time(9, 30), time(11, 30)), (time(13, 0), time(15, 0)))),
    ("HK", "beijing", ((time(9, 30), time(12, 0)), (time(13, 0), time(16, 0)))),
    ("US", "eastern", ((time(9, 30), time(16, 0)),)),
)


def _nth_sunday(year: int, month: int, n: int) -> date:
    first = date(year, month, 1)
    offset = (6 - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _eastern_by_rule(now_utc: datetime) -> datetime:
    """US Eastern time without a tz database (post-2007 DST rule)."""
    year = now_utc.year
    dst_start = datetime.combine(_nth_sunday(year, 3, 2), time(7, 0), timezone.utc)
    dst_end = datetime.combine(_nth_sunday(year, 11, 1), time(6, 0), timezone.utc)
    zone = _EDT if dst_start <= now_utc < dst_end else _EST
    return now_utc.astimezone(zone)


def _eastern_zone() -> Optional[tzinfo]:
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo("America/New_York")
    except Exception:  # noqa: BLE001 - no tz database in the image
        return None


def to_eastern(now_utc: datetime) -> datetime:
    """Convert an aware UTC datetime to US Eastern time."""
    zone = _eastern_zone()
    if zone is None:
        return _eastern_by_rule(now_utc)
    return now_utc.astimezone(zone)


def _previous_weekday(day: date) -> date:
    day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def market_session(local_now: datetime, sessions: tuple[tuple[time, time], ...]) -> tuple[str, date]:
    """Session state and latest weekday session date at ``local_now``.

    Args:
        local_now: The current time in the market's own zone.
        sessions: Regular sessions as ``(open, close)`` local times.

    Returns:
        ``(state, latest_session_date)``; ``state`` is one of ``open``,
        ``midday break``, ``pre-open``, ``closed for the day``,
        ``closed (weekend)``. The date is today once today's session has
        opened, else the previous weekday.
    """
    today = local_now.date()
    if local_now.weekday() >= 5:
        return "closed (weekend)", _previous_weekday(today)
    now_t = local_now.time()
    if now_t < sessions[0][0]:
        return "pre-open", _previous_weekday(today)
    if any(start <= now_t < end for start, end in sessions):
        return "open", today
    if now_t >= sessions[-1][1]:
        return "closed for the day", today
    return "midday break", today


def clock_lines(now: Optional[datetime] = None) -> list[str]:
    """Two prompt lines: the current time, and each market's session state.

    Args:
        now: Instant to render (aware or naive-UTC); defaults to now.

    Returns:
        ``["Now: …", "Markets …"]``.
    """
    if now is None:
        now_utc = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now_utc = now.replace(tzinfo=timezone.utc)
    else:
        now_utc = now.astimezone(timezone.utc)
    beijing = now_utc.astimezone(BEIJING)
    eastern = to_eastern(now_utc)
    eastern_label = "EDT" if eastern.utcoffset() == timedelta(hours=-4) else "EST"
    local = {"beijing": beijing, "eastern": eastern}
    markets = []
    for label, zone_key, sessions in _MARKETS:
        state, latest = market_session(local[zone_key], sessions)
        markets.append(f"{label} {state}, latest weekday session {latest:%Y-%m-%d (%a)}")
    return [
        f"Now: {beijing:%Y-%m-%d %H:%M} Beijing time (UTC+8, {beijing:%A}) | "
        f"US Eastern {eastern:%Y-%m-%d %H:%M} ({eastern_label}, {eastern:%A})",
        "Markets (regular hours only; exchange holidays are not checked — "
        "confirm the latest trading day from data): " + "; ".join(markets),
    ]
