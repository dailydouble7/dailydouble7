"""Black-76 pricing, implied-vol inversion, and greeks for European index options.

SPX options are European and cash-settled, so Black-76 on the forward applies
directly -- no early-exercise numerics. Everything here is expressed in terms of
the forward ``F`` and discount factor ``D``, which :mod:`spxvol.forward` recovers
from put-call parity. That keeps the whole stack free of any dependency on an
index spot feed, a rate curve, or a dividend forecast.

Pure stdlib: no numpy required.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

SQRT_2PI = math.sqrt(2.0 * math.pi)

# Inversion search bracket, in decimal vol (1.0 == 100 vol points).
MIN_VOL = 1e-6
MAX_VOL = 10.0


def norm_cdf(x: float) -> float:
    """Standard normal CDF via erfc (full double precision in both tails)."""
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def norm_pdf(x: float) -> float:
    """Standard normal PDF."""
    return math.exp(-0.5 * x * x) / SQRT_2PI


# Acklam's rational approximation to the inverse normal CDF (|err| < 1.15e-9),
# refined by one Halley step against erfc so it is accurate to ~machine epsilon.
_A = (-3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02,
      1.383577518672690e02, -3.066479806614716e01, 2.506628277459239e00)
_B = (-5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02,
      6.680131188771972e01, -1.328068155288572e01)
_C = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00,
      -2.549732539343734e00, 4.374664141464968e00, 2.938163982698783e00)
_D = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00,
      3.754408661907416e00)


def norm_ppf(p: float) -> float:
    """Inverse standard normal CDF. Raises ValueError outside (0, 1)."""
    if not 0.0 < p < 1.0:
        raise ValueError(f"norm_ppf requires 0 < p < 1, got {p!r}")
    p_low, p_high = 0.02425, 1.0 - 0.02425
    if p < p_low:
        q = math.sqrt(-2.0 * math.log(p))
        x = (((((_C[0] * q + _C[1]) * q + _C[2]) * q + _C[3]) * q + _C[4]) * q + _C[5]) / (
            (((_D[0] * q + _D[1]) * q + _D[2]) * q + _D[3]) * q + 1.0)
    elif p > p_high:
        q = math.sqrt(-2.0 * math.log(1.0 - p))
        x = -(((((_C[0] * q + _C[1]) * q + _C[2]) * q + _C[3]) * q + _C[4]) * q + _C[5]) / (
            (((_D[0] * q + _D[1]) * q + _D[2]) * q + _D[3]) * q + 1.0)
    else:
        q = p - 0.5
        r = q * q
        x = (((((_A[0] * r + _A[1]) * r + _A[2]) * r + _A[3]) * r + _A[4]) * r + _A[5]) * q / (
            ((((_B[0] * r + _B[1]) * r + _B[2]) * r + _B[3]) * r + _B[4]) * r + 1.0)
    # One Halley refinement.
    e = 0.5 * math.erfc(-x / math.sqrt(2.0)) - p
    u = e * SQRT_2PI * math.exp(0.5 * x * x)
    return x - u / (1.0 + 0.5 * x * u)


def _d1_d2(forward: float, strike: float, tenor: float, vol: float) -> tuple[float, float]:
    sqt = vol * math.sqrt(tenor)
    d1 = (math.log(forward / strike) + 0.5 * vol * vol * tenor) / sqt
    return d1, d1 - sqt


def undiscounted_price(forward: float, strike: float, tenor: float, vol: float, is_call: bool) -> float:
    """Black-76 price *in forward value terms* (i.e. before multiplying by D)."""
    if forward <= 0.0 or strike <= 0.0:
        raise ValueError("forward and strike must be positive")
    if tenor <= 0.0 or vol <= 0.0:
        return max(forward - strike, 0.0) if is_call else max(strike - forward, 0.0)
    d1, d2 = _d1_d2(forward, strike, tenor, vol)
    if is_call:
        return forward * norm_cdf(d1) - strike * norm_cdf(d2)
    return strike * norm_cdf(-d2) - forward * norm_cdf(-d1)


def price(forward: float, strike: float, tenor: float, vol: float, is_call: bool,
          discount: float = 1.0) -> float:
    """Black-76 present value."""
    return discount * undiscounted_price(forward, strike, tenor, vol, is_call)


def vega(forward: float, strike: float, tenor: float, vol: float, discount: float = 1.0) -> float:
    """d(price)/d(vol), with vol in decimals. Same for calls and puts.

    Multiply by 0.01 for "per vol point", and by the contract multiplier (100 for
    SPX) to get dollars.
    """
    if tenor <= 0.0 or vol <= 0.0 or forward <= 0.0 or strike <= 0.0:
        return 0.0
    d1, _ = _d1_d2(forward, strike, tenor, vol)
    return discount * forward * norm_pdf(d1) * math.sqrt(tenor)


def forward_delta(forward: float, strike: float, tenor: float, vol: float, is_call: bool) -> float:
    """Undiscounted delta with respect to the forward: N(d1) for calls.

    This is the delta convention used for bucketing throughout the package. It is
    recoverable from parity data alone -- spot delta additionally requires the
    index level (see :func:`spot_delta`), which cvforge may not expose.
    """
    if tenor <= 0.0 or vol <= 0.0:
        intrinsic = forward > strike
        if is_call:
            return 1.0 if intrinsic else 0.0
        return 0.0 if intrinsic else -1.0
    d1, _ = _d1_d2(forward, strike, tenor, vol)
    return norm_cdf(d1) if is_call else norm_cdf(d1) - 1.0


def dividend_discount(forward: float, discount: float, spot: float) -> float:
    """Recover ``exp(-qT)`` from the parity outputs plus an index level.

    ``F = S * exp((r-q)T) = S * exp(-qT) / D``, hence ``exp(-qT) = F * D / S``.
    Only needed for spot-delta conventions; forward delta needs no spot.
    """
    if spot <= 0.0 or forward <= 0.0 or discount <= 0.0:
        raise ValueError("spot, forward and discount must be positive")
    return forward * discount / spot


def spot_delta(forward: float, strike: float, tenor: float, vol: float, is_call: bool,
               discount: float, spot: float) -> float:
    """Spot delta. Requires an index level to split D into rate vs dividend."""
    dq = dividend_discount(forward, discount, spot)
    return dq * forward_delta(forward, strike, tenor, vol, is_call)


@dataclass(frozen=True)
class InversionResult:
    """Outcome of an implied-vol solve."""

    vol: float | None
    reason: str = "ok"
    iterations: int = 0

    @property
    def ok(self) -> bool:
        return self.vol is not None


def implied_vol(observed_price: float, forward: float, strike: float, tenor: float,
                is_call: bool, discount: float = 1.0, *, tol: float = 1e-10,
                max_iter: int = 100, min_time_value: float = 1e-6) -> InversionResult:
    """Invert Black-76 for volatility using safeguarded Newton.

    The observed price is de-discounted first so the solve happens in forward
    value space, where the no-arbitrage bounds are exactly ``[intrinsic, F]`` for
    calls and ``[intrinsic, K]`` for puts. Prices at or outside those bounds are
    rejected with a reason rather than silently clamped -- with trade prints (as
    opposed to mids) genuine bound violations happen, usually from a stale or
    mismatched-timestamp leg, and they must not become fake IV observations.

    ``min_time_value`` guards the other degenerate end. Once an option's time
    value shrinks to near nothing its price stops depending on volatility, so no
    vol is identifiable: an absolute price tolerance would be met at the very
    first trial vol and the solver would "converge" on the bracket floor. Such
    prices are rejected rather than reported as near-zero vol. The default is
    far below any real tick (SPX trades in 0.05 increments) and is expressed in
    the same units as the price.
    """
    if tenor <= 0.0:
        return InversionResult(None, "expired")
    if discount <= 0.0:
        return InversionResult(None, "bad_discount")
    if forward <= 0.0 or strike <= 0.0:
        return InversionResult(None, "bad_forward_or_strike")

    target = observed_price / discount
    intrinsic = max(forward - strike, 0.0) if is_call else max(strike - forward, 0.0)
    upper = forward if is_call else strike

    if target <= intrinsic + 1e-12:
        return InversionResult(None, "at_or_below_intrinsic")
    if target >= upper - 1e-12:
        return InversionResult(None, "at_or_above_upper_bound")
    if target - intrinsic < min_time_value:
        return InversionResult(None, "negligible_time_value")

    lo, hi = MIN_VOL, MAX_VOL
    f_lo = undiscounted_price(forward, strike, tenor, lo, is_call) - target
    f_hi = undiscounted_price(forward, strike, tenor, hi, is_call) - target
    if f_lo > 0.0:
        return InversionResult(None, "below_min_vol")
    if f_hi < 0.0:
        return InversionResult(None, "above_max_vol")

    # Start from the Brenner-Subrahmanyam ATM approximation, clamped into bracket.
    guess = math.sqrt(2.0 * math.pi / tenor) * target / forward
    vol = min(max(guess, lo), hi)

    for i in range(1, max_iter + 1):
        diff = undiscounted_price(forward, strike, tenor, vol, is_call) - target
        if diff > 0.0:
            hi = vol
        else:
            lo = vol
        if abs(diff) < tol:
            return _finish(vol, i)
        v = vega(forward, strike, tenor, vol)
        if v > 1e-12:
            step = diff / v
            candidate = vol - step
        else:
            candidate = float("nan")
        # Fall back to bisection whenever Newton leaves the bracket or misbehaves.
        if not (lo < candidate < hi) or math.isnan(candidate):
            candidate = 0.5 * (lo + hi)
        if abs(candidate - vol) < 1e-14:
            return _finish(candidate, i)
        vol = candidate

    return InversionResult(vol, "max_iter", max_iter)


def _finish(vol: float, iterations: int) -> InversionResult:
    """Reject solutions pinned against the search bracket.

    A vol sitting on the floor or ceiling means the bracket, not the data,
    determined the answer -- report that rather than a spurious number.
    """
    if vol <= MIN_VOL * (1.0 + 1e-6):
        return InversionResult(None, "below_min_vol", iterations)
    if vol >= MAX_VOL * (1.0 - 1e-6):
        return InversionResult(None, "above_max_vol", iterations)
    return InversionResult(vol, "ok", iterations)


def strike_for_delta(target_delta: float, forward: float, tenor: float, vol: float,
                     is_call: bool) -> float:
    """Strike whose forward delta equals ``target_delta`` at a flat ``vol``.

    Inverting ``N(d1) = target`` for k = ln(K/F) gives
    ``k = 0.5*vol^2*T - z*vol*sqrt(T)`` with ``z = N^-1(target)``.
    """
    z = norm_ppf(target_delta if is_call else target_delta + 1.0)
    k = 0.5 * vol * vol * tenor - z * vol * math.sqrt(tenor)
    return forward * math.exp(k)
