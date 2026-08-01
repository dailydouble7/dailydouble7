"""Recover the forward and discount factor from put-call parity.

For a European, cash-settled index option, parity is exact:

    C - P = D * (F - K)

which is *linear in the strike*. Regressing ``C - P`` on ``K`` across several
strikes at one timestamp yields both unknowns at once: the slope is ``-D`` and
the intercept is ``D * F``.

This matters more than it looks. The alternative -- assuming a rate curve and a
dividend forecast -- puts a systematic tilt across the whole SPX smile, which is
precisely the quantity a skew-richness model is trying to measure. Parity needs
no rate, no dividend yield, and no index print. It also self-corrects for clock
drift, because both legs are sampled from the same minute.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

MIN_STRIKES = 3          # 2 determines a line; 3 is the minimum that can be checked
DEFAULT_ATM_BAND = 0.06  # gaussian weight width, as a fraction of the forward


@dataclass(frozen=True)
class ParityQuote:
    """A call and put sampled at the same strike, expiry and instant."""

    strike: float
    call_price: float
    put_price: float

    @property
    def synthetic(self) -> float:
        """``C - P``, the regression's dependent variable."""
        return self.call_price - self.put_price


@dataclass(frozen=True)
class ForwardSolution:
    """Forward and discount recovered from a parity fit, plus fit diagnostics."""

    forward: float | None
    discount: float | None
    n_strikes: int
    r_squared: float
    residual_std: float
    strikes_used: tuple[float, ...] = field(default_factory=tuple)
    reason: str = "ok"

    @property
    def ok(self) -> bool:
        return self.forward is not None and self.discount is not None

    def implied_rate(self, tenor: float) -> float | None:
        """Continuously-compounded rate implied by the discount factor."""
        if self.discount is None or tenor <= 0.0 or self.discount <= 0.0:
            return None
        return -math.log(self.discount) / tenor

    @property
    def confidence(self) -> str:
        """Coarse quality label for downstream filtering."""
        if not self.ok:
            return "none"
        if self.n_strikes < MIN_STRIKES:
            return "low"
        if self.r_squared >= 0.9999 and self.n_strikes >= 5:
            return "high"
        if self.r_squared >= 0.999:
            return "medium"
        return "low"


def _weighted_line(xs: list[float], ys: list[float], ws: list[float]) -> tuple[float, float] | None:
    """Weighted least squares fit of ``y = a + b*x``; returns (a, b)."""
    sw = sum(ws)
    if sw <= 0.0:
        return None
    swx = sum(w * x for w, x in zip(ws, xs))
    swy = sum(w * y for w, y in zip(ws, ys))
    swxx = sum(w * x * x for w, x in zip(ws, xs))
    swxy = sum(w * x * y for w, x, y in zip(ws, xs, ys))
    denom = sw * swxx - swx * swx
    if abs(denom) < 1e-12:
        return None
    b = (sw * swxy - swx * swy) / denom
    a = (swy - b * swx) / sw
    return a, b


def solve_forward(quotes: list[ParityQuote], *, atm_band: float = DEFAULT_ATM_BAND,
                  refine_passes: int = 2, min_strikes: int = MIN_STRIKES,
                  max_discount: float = 1.05) -> ForwardSolution:
    """Fit ``C - P = D*(F - K)`` across strikes to recover ``(F, D)``.

    The first pass is unweighted. Subsequent passes weight strikes by a gaussian
    centred on the running forward estimate, because near-the-money synthetics
    are the tightest and most reliably two-sided; deep wings are where a stale
    leg does the most damage to the slope.
    """
    usable = [q for q in quotes
              if q.strike > 0 and math.isfinite(q.call_price) and math.isfinite(q.put_price)]
    # Duplicate strikes would silently over-weight one point.
    by_strike = {q.strike: q for q in usable}
    usable = sorted(by_strike.values(), key=lambda q: q.strike)

    if len(usable) < 2:
        return ForwardSolution(None, None, len(usable), 0.0, 0.0, reason="too_few_strikes")

    xs = [q.strike for q in usable]
    ys = [q.synthetic for q in usable]
    ws = [1.0] * len(usable)

    forward = discount = None
    for _ in range(max(1, refine_passes)):
        fit = _weighted_line(xs, ys, ws)
        if fit is None:
            return ForwardSolution(None, None, len(usable), 0.0, 0.0, reason="singular_fit")
        intercept, slope = fit
        if slope >= 0.0:
            # Slope must be -D < 0; a non-negative slope means the data are not parity-shaped.
            return ForwardSolution(None, None, len(usable), 0.0, 0.0, reason="non_negative_slope")
        discount = -slope
        forward = intercept / discount
        if forward <= 0.0:
            return ForwardSolution(None, None, len(usable), 0.0, 0.0, reason="non_positive_forward")
        width = atm_band * forward
        ws = [math.exp(-0.5 * ((x - forward) / width) ** 2) for x in xs]
        if sum(ws) <= 1e-12:
            ws = [1.0] * len(usable)

    assert forward is not None and discount is not None

    if not 0.0 < discount <= max_discount:
        return ForwardSolution(None, None, len(usable), 0.0, 0.0, reason="implausible_discount")

    predicted = [discount * (forward - x) for x in xs]
    resid = [y - p for y, p in zip(ys, predicted)]
    n = len(usable)
    dof = max(n - 2, 1)
    residual_std = math.sqrt(sum(r * r for r in resid) / dof)
    mean_y = sum(ys) / n
    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    r_squared = 1.0 - sum(r * r for r in resid) / ss_tot if ss_tot > 1e-12 else 1.0

    reason = "ok" if n >= min_strikes else "few_strikes"
    return ForwardSolution(forward, discount, n, r_squared, residual_std, tuple(xs), reason)


def forward_from_single_strike(quote: ParityQuote, discount: float) -> float:
    """``F = K + (C - P)/D`` -- usable when only one strike pair is available.

    Requires an externally supplied discount factor, so prefer
    :func:`solve_forward` whenever three or more strikes exist.
    """
    if discount <= 0.0:
        raise ValueError("discount must be positive")
    return quote.strike + quote.synthetic / discount


def atm_strike_guess(quotes: list[ParityQuote]) -> float | None:
    """Strike where ``|C - P|`` is smallest -- a fast, model-free ATM locator.

    Useful for centring a strike grid before any fitting has happened, since
    ``C - P`` crosses zero exactly at ``K = F``.
    """
    if not quotes:
        return None
    return min(quotes, key=lambda q: abs(q.synthetic)).strike
