"""Aggregate bar records and large-print detection.

cvforge/Massive aggregates carry ``vwap`` and ``transactions`` alongside OHLCV,
and the transaction count is what makes trade-level work possible without a
tape. When ``transactions == 1`` the bar *is* a single print: open, high, low
and close all equal the traded price and ``volume`` is the exact contract count.
More generally ``volume / transactions`` separates block activity (few, huge
prints) from retail churn (many, small ones).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Literal

from .tenor import from_epoch_ms

PriceMode = Literal["print", "vwap", "close"]

# A bar whose average trade size clears this is treated as block-like.
BLOCK_AVG_SIZE = 50.0
# ...and one that also clears this total size is a large print by any measure.
LARGE_VOLUME = 250.0


@dataclass(frozen=True)
class Bar:
    """One aggregate bar for a single option contract."""

    ticker: str
    timestamp_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    vwap: float | None = None
    transactions: int | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Bar | None":
        """Build from a ``normalize_option_bars`` row, or a raw Polygon aggregate."""
        ticker = row.get("ticker")
        ts = row.get("timestamp_ms", row.get("t"))
        close = row.get("close", row.get("c"))
        if ticker is None or ts is None or close is None:
            return None
        try:
            return cls(
                ticker=str(ticker),
                timestamp_ms=int(ts),
                open=float(row.get("open", row.get("o", close))),
                high=float(row.get("high", row.get("h", close))),
                low=float(row.get("low", row.get("l", close))),
                close=float(close),
                volume=float(row.get("volume", row.get("v", 0.0)) or 0.0),
                vwap=_opt_float(row.get("vwap", row.get("vw"))),
                transactions=_opt_int(row.get("transactions", row.get("n"))),
            )
        except (TypeError, ValueError):
            return None

    @property
    def timestamp(self) -> datetime:
        return from_epoch_ms(self.timestamp_ms)

    @property
    def is_single_print(self) -> bool:
        """The bar contains exactly one trade, so its price is that trade's price."""
        return self.transactions == 1

    @property
    def avg_trade_size(self) -> float | None:
        if not self.transactions:
            return None
        return self.volume / self.transactions

    @property
    def is_flat(self) -> bool:
        """OHLC all equal -- consistent with a single print or one repeated level."""
        return self.high == self.low == self.open == self.close

    @property
    def print_quality(self) -> str:
        """How confidently a single trade price can be read off this bar.

        ``exact``  - one transaction; price and size are exact.
        ``block``  - few large trades; the bar is dominated by size.
        ``mixed``  - several trades of moderate size.
        ``retail`` - many small trades; no individual trade is recoverable.
        """
        if self.is_single_print:
            return "exact"
        avg = self.avg_trade_size
        if avg is None:
            return "unknown"
        if avg >= BLOCK_AVG_SIZE:
            return "block"
        if avg >= 10.0:
            return "mixed"
        return "retail"

    def price(self, mode: PriceMode = "print") -> float | None:
        """Representative price for a use case.

        ``print`` favours the traded level (close), which is what a trade-level
        signal needs. ``vwap`` is a better central/mid-like estimate and is what
        the parity fit should consume, since it is less contaminated by which
        side of the spread happened to trade last in the minute.
        """
        if mode == "close" or mode == "print":
            return self.close if math.isfinite(self.close) else None
        if mode == "vwap":
            if self.vwap is not None and math.isfinite(self.vwap) and self.vwap > 0:
                return self.vwap
            return self.close if math.isfinite(self.close) else None
        raise ValueError(f"unknown price mode {mode!r}")

    def is_large(self, *, min_volume: float = LARGE_VOLUME,
                 min_avg_size: float = BLOCK_AVG_SIZE) -> bool:
        """Whether this bar plausibly contains a single large trade."""
        if self.volume < min_volume:
            return False
        if self.is_single_print:
            return True
        avg = self.avg_trade_size
        return avg is not None and avg >= min_avg_size


def _opt_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _opt_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_bars(rows: Iterable[dict[str, Any]]) -> list[Bar]:
    """Parse and time-sort a sequence of normalized bar rows."""
    out = [bar for bar in (Bar.from_row(row) for row in rows) if bar is not None]
    out.sort(key=lambda b: b.timestamp_ms)
    return out


def index_by_timestamp(bars: Iterable[Bar]) -> dict[int, Bar]:
    """Map epoch-ms to bar, keeping the last bar on duplicate stamps."""
    return {bar.timestamp_ms: bar for bar in bars}
