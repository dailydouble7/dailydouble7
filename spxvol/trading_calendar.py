"""NYSE trading calendar and trading-time year fractions.

Calendar-day tenor makes 0DTE implied vol explode into the close, which
contaminates exactly the short-dated SPX trades this package is built to
measure. Everything here works in *trading minutes* instead.

Holiday rules are the standard NYSE set and are approximate for early closes;
override via :class:`TradingCalendar` if you need exchange-exact behaviour.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")

REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)

# Standard trading-time normalisation: 252 sessions x 390 minutes.
MINUTES_PER_SESSION = 390
SESSIONS_PER_YEAR = 252
MINUTES_PER_YEAR = MINUTES_PER_SESSION * SESSIONS_PER_YEAR


def easter(year: int) -> date:
    """Gregorian Easter Sunday (anonymous algorithm)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month, day = divmod(h + ell - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """n-th ``weekday`` (Mon=0) of a month; n=-1 means the last one."""
    if n > 0:
        first = date(year, month, 1)
        offset = (weekday - first.weekday()) % 7
        return first + timedelta(days=offset + 7 * (n - 1))
    last_day = (date(year, month, 28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    offset = (last_day.weekday() - weekday) % 7
    return last_day - timedelta(days=offset)


def _observed(day: date) -> date | None:
    """NYSE observation rule: Sunday shifts to Monday, Saturday is not observed."""
    if day.weekday() == 5:
        return None
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


@lru_cache(maxsize=256)
def holidays(year: int) -> frozenset[date]:
    """NYSE full-day closures for a calendar year."""
    out: set[date] = set()
    for candidate in (
        _observed(date(year, 1, 1)),
        _nth_weekday(year, 1, 0, 3) if year >= 1998 else None,   # MLK
        _nth_weekday(year, 2, 0, 3),                             # Presidents
        easter(year) - timedelta(days=2),                        # Good Friday
        _nth_weekday(year, 5, 0, -1),                            # Memorial
        _observed(date(year, 6, 19)) if year >= 2022 else None,  # Juneteenth
        _observed(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),                             # Labor
        _nth_weekday(year, 11, 3, 4),                            # Thanksgiving
        _observed(date(year, 12, 25)),
    ):
        if candidate is not None:
            out.add(candidate)
    return frozenset(out)


@lru_cache(maxsize=256)
def early_closes(year: int) -> frozenset[date]:
    """Sessions that close at 13:00 ET (1pm)."""
    out: set[date] = set()
    hol = holidays(year)

    july4 = date(year, 7, 4)
    if july4.weekday() < 5:
        eve = july4 - timedelta(days=1)
        if eve.weekday() < 5 and eve not in hol:
            out.add(eve)

    out.add(_nth_weekday(year, 11, 3, 4) + timedelta(days=1))  # Friday after Thanksgiving

    xmas_eve = date(year, 12, 24)
    if xmas_eve.weekday() < 5 and xmas_eve not in hol:
        out.add(xmas_eve)

    return frozenset(d for d in out if d.weekday() < 5 and d not in hol)


class TradingCalendar:
    """NYSE sessions, with hooks for overriding holidays and early closes."""

    def __init__(self, extra_holidays: frozenset[date] | None = None,
                 extra_early_closes: frozenset[date] | None = None):
        self.extra_holidays = extra_holidays or frozenset()
        self.extra_early_closes = extra_early_closes or frozenset()

    def is_session(self, day: date) -> bool:
        if day.weekday() >= 5:
            return False
        return day not in holidays(day.year) and day not in self.extra_holidays

    def close_time(self, day: date) -> time:
        if day in early_closes(day.year) or day in self.extra_early_closes:
            return EARLY_CLOSE
        return REGULAR_CLOSE

    def session_bounds(self, day: date) -> tuple[datetime, datetime] | None:
        """Timezone-aware open/close for a session, or None if not a session."""
        if not self.is_session(day):
            return None
        return (
            datetime.combine(day, REGULAR_OPEN, tzinfo=EASTERN),
            datetime.combine(day, self.close_time(day), tzinfo=EASTERN),
        )

    def session_minutes(self, day: date) -> int:
        bounds = self.session_bounds(day)
        if bounds is None:
            return 0
        return int((bounds[1] - bounds[0]).total_seconds() // 60)

    def trading_minutes_between(self, start: datetime, end: datetime) -> float:
        """Trading minutes in ``[start, end]``, counting partial sessions.

        Both bounds must be timezone-aware; they are converted to Eastern.
        """
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("start and end must be timezone-aware")
        start = start.astimezone(EASTERN)
        end = end.astimezone(EASTERN)
        if end <= start:
            return 0.0

        total = 0.0
        day = start.date()
        last = end.date()
        while day <= last:
            bounds = self.session_bounds(day)
            if bounds is not None:
                lo = max(bounds[0], start)
                hi = min(bounds[1], end)
                if hi > lo:
                    total += (hi - lo).total_seconds() / 60.0
            day += timedelta(days=1)
        return total

    def year_fraction(self, start: datetime, end: datetime) -> float:
        """Trading-time year fraction between two instants."""
        return self.trading_minutes_between(start, end) / MINUTES_PER_YEAR


DEFAULT_CALENDAR = TradingCalendar()
