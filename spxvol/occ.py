"""OCC option symbol encoding/decoding (Polygon/Massive ``O:`` flavour).

Format: ``O:<ROOT><YYMMDD><C|P><STRIKE*1000 zero-padded to 8>``
e.g. ``O:SPXW260630C07300000`` is SPXW 2026-06-30 call, strike 7300.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

_TAIL = 15  # 6 date + 1 right + 8 strike
_PATTERN = re.compile(r"^(?P<root>[A-Z0-9.]+)(?P<ymd>\d{6})(?P<right>[CP])(?P<strike>\d{8})$")

# Roots that settle on the opening print (AM settlement).
AM_SETTLED_ROOTS = frozenset({"SPX", "NDX", "RUT", "DJX", "XEO", "OEX"})


@dataclass(frozen=True)
class OptionContract:
    """A parsed option contract."""

    root: str
    expiration: date
    is_call: bool
    strike: float

    @property
    def right(self) -> str:
        return "C" if self.is_call else "P"

    @property
    def is_am_settled(self) -> bool:
        """True when settlement is struck from opening prices.

        SPX monthlies (root ``SPX``) are AM-settled; SPXW weeklies/dailies are
        PM-settled. Same-looking expiry date, materially different tenor
        intraday -- getting this wrong shows up as a fake term-structure kink.
        """
        return self.root in AM_SETTLED_ROOTS

    @property
    def ticker(self) -> str:
        return encode(self.root, self.expiration, self.is_call, self.strike)

    def counterpart(self) -> "OptionContract":
        """Same root/expiry/strike, opposite right (the parity partner)."""
        return OptionContract(self.root, self.expiration, not self.is_call, self.strike)


def encode(root: str, expiration: date, is_call: bool, strike: float) -> str:
    """Build an ``O:``-prefixed OCC ticker."""
    root = root.strip().upper()
    if not root:
        raise ValueError("root must be non-empty")
    if strike <= 0:
        raise ValueError(f"strike must be positive, got {strike}")
    # Decimal avoids the binary-float rounding that turns 7300.0 into 07299999.
    thousandths = int((Decimal(str(strike)) * 1000).to_integral_value())
    return f"O:{root}{expiration:%y%m%d}{'C' if is_call else 'P'}{thousandths:08d}"


def decode(ticker: str) -> OptionContract:
    """Parse an OCC ticker, with or without the ``O:`` prefix."""
    raw = ticker.strip().upper()
    if raw.startswith("O:"):
        raw = raw[2:]
    if len(raw) <= _TAIL:
        raise ValueError(f"ticker too short to be an OCC symbol: {ticker!r}")
    match = _PATTERN.match(raw)
    if match is None:
        raise ValueError(f"not a valid OCC option symbol: {ticker!r}")
    ymd = match.group("ymd")
    expiration = date(2000 + int(ymd[0:2]), int(ymd[2:4]), int(ymd[4:6]))
    strike = int(match.group("strike")) / 1000.0
    return OptionContract(
        root=match.group("root"),
        expiration=expiration,
        is_call=match.group("right") == "C",
        strike=strike,
    )
