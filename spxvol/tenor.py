"""Contract tenor in trading time, respecting AM vs PM settlement."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .occ import OptionContract
from .trading_calendar import (
    DEFAULT_CALENDAR,
    EASTERN,
    REGULAR_OPEN,
    TradingCalendar,
)


def expiry_moment(contract: OptionContract, calendar: TradingCalendar = DEFAULT_CALENDAR) -> datetime:
    """The instant a contract stops accruing time value.

    AM-settled contracts (SPX monthlies) settle from Friday's opening prints, so
    their tenor ends at 09:30 ET on expiration day -- not at the close.
    """
    if contract.is_am_settled:
        return datetime.combine(contract.expiration, REGULAR_OPEN, tzinfo=EASTERN)
    close = calendar.close_time(contract.expiration)
    return datetime.combine(contract.expiration, close, tzinfo=EASTERN)


def from_epoch_ms(timestamp_ms: int) -> datetime:
    """Convert a bar's epoch-millisecond stamp to an Eastern-time datetime."""
    return datetime.fromtimestamp(timestamp_ms / 1000.0, tz=timezone.utc).astimezone(EASTERN)


def tenor_years(contract: OptionContract, now: datetime,
                calendar: TradingCalendar = DEFAULT_CALENDAR) -> float:
    """Trading-time year fraction from ``now`` to expiry. Zero once expired."""
    return calendar.year_fraction(now, expiry_moment(contract, calendar))


def calendar_tenor_years(contract: OptionContract, now: datetime,
                         calendar: TradingCalendar = DEFAULT_CALENDAR) -> float:
    """Calendar-time (ACT/365) tenor, for comparison against vendor conventions."""
    delta = expiry_moment(contract, calendar) - now.astimezone(EASTERN)
    return max(delta.total_seconds(), 0.0) / (365.0 * 86400.0)


def sessions_to_expiry(contract: OptionContract, now: datetime,
                       calendar: TradingCalendar = DEFAULT_CALENDAR) -> int:
    """Whole sessions remaining, for tenor bucketing. 0 means same-session (0DTE)."""
    end = expiry_moment(contract, calendar).date()
    day = now.astimezone(EASTERN).date()
    count = 0
    while day < end:
        day += timedelta(days=1)
        if calendar.is_session(day):
            count += 1
    return count
