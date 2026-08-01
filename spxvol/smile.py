"""Per-timestamp smile fitting and residual measurement.

Total implied variance ``w = sigma^2 * T`` is fitted as a quadratic in log
forward-moneyness ``k = ln(K/F)``:

    w(k) = a + b*k + c*k^2

This is a practitioner smile, not an arbitrage-free parameterisation like SVI.
That is a deliberate choice: the model only ever *interpolates* between observed
strikes to ask "where should this contract have traded", so static-arbitrage
guarantees in the wings buy nothing, and a linear-in-parameters fit is stable
with the handful of strikes a single minute of data provides.

Points are vega-weighted, because implied vol read off a low-vega wing contract
is far noisier than one read near the money.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .black76 import forward_delta, norm_ppf

MIN_POINTS = 3


@dataclass(frozen=True)
class SmilePoint:
    """One observed implied vol for the fit."""

    strike: float
    log_moneyness: float
    iv: float
    weight: float = 1.0
    tag: str | None = None      # free-form, e.g. the source ticker


@dataclass(frozen=True)
class SmileFit:
    """A fitted quadratic-in-log-moneyness total-variance curve."""

    a: float
    b: float
    c: float
    tenor: float
    forward: float
    n_points: int
    rmse_vol: float
    reason: str = "ok"

    @property
    def ok(self) -> bool:
        return self.reason == "ok"

    def total_variance(self, log_moneyness: float) -> float:
        w = self.a + self.b * log_moneyness + self.c * log_moneyness * log_moneyness
        return max(w, 1e-12)

    def iv(self, log_moneyness: float) -> float:
        """Fitted implied vol at a log-moneyness."""
        if self.tenor <= 0.0:
            return 0.0
        return math.sqrt(self.total_variance(log_moneyness) / self.tenor)

    def iv_at_strike(self, strike: float) -> float:
        if strike <= 0.0 or self.forward <= 0.0:
            raise ValueError("strike and forward must be positive")
        return self.iv(math.log(strike / self.forward))

    @property
    def atm_iv(self) -> float:
        """Fitted vol at the forward (k = 0)."""
        return self.iv(0.0)

    def strike_at_delta(self, target_delta: float, is_call: bool, *,
                        max_iter: int = 40, tol: float = 1e-10) -> float:
        """Strike whose forward delta equals ``target_delta`` *on this smile*.

        Delta depends on vol and vol depends on strike, so this iterates:
        invert ``N(d1) = target`` for k at the current vol, re-read vol off the
        smile at that k, repeat. Converges in a handful of passes.
        """
        z = norm_ppf(target_delta if is_call else target_delta + 1.0)
        vol = max(self.atm_iv, 1e-6)
        k = 0.0
        for _ in range(max_iter):
            sqt = vol * math.sqrt(self.tenor)
            k_next = 0.5 * vol * vol * self.tenor - z * sqt
            vol_next = max(self.iv(k_next), 1e-6)
            if abs(k_next - k) < tol and abs(vol_next - vol) < tol:
                k, vol = k_next, vol_next
                break
            k, vol = k_next, vol_next
        return self.forward * math.exp(k)

    def iv_at_delta(self, target_delta: float, is_call: bool) -> float:
        """Fitted vol at a delta bucket -- the natural axis for comparing days."""
        return self.iv_at_strike(self.strike_at_delta(target_delta, is_call))

    def delta_at_strike(self, strike: float, is_call: bool) -> float:
        vol = self.iv_at_strike(strike)
        return forward_delta(self.forward, strike, self.tenor, vol, is_call)


def _solve_symmetric_3x3(m: list[list[float]], rhs: list[float]) -> list[float] | None:
    """Gaussian elimination with partial pivoting for the 3x3 normal equations."""
    aug = [row[:] + [rhs[i]] for i, row in enumerate(m)]
    n = 3
    for col in range(n):
        pivot_row = max(range(col, n), key=lambda r: abs(aug[r][col]))
        if abs(aug[pivot_row][col]) < 1e-14:
            return None
        aug[col], aug[pivot_row] = aug[pivot_row], aug[col]
        pivot = aug[col][col]
        for r in range(col + 1, n):
            factor = aug[r][col] / pivot
            for c in range(col, n + 1):
                aug[r][c] -= factor * aug[col][c]
    out = [0.0] * n
    for row in range(n - 1, -1, -1):
        acc = aug[row][n] - sum(aug[row][c] * out[c] for c in range(row + 1, n))
        out[row] = acc / aug[row][row]
    return out


def fit_smile(points: list[SmilePoint], tenor: float, forward: float, *,
              exclude_tags: frozenset[str] | None = None,
              min_points: int = MIN_POINTS) -> SmileFit:
    """Weighted least-squares fit of total variance against log-moneyness.

    ``exclude_tags`` drops named points before fitting. That is what makes the
    leave-one-out measurement in :mod:`spxvol.signal` honest: a large print must
    never contribute to the curve it is being scored against, or it drags the
    fit toward itself and its own richness disappears.
    """
    excluded = exclude_tags or frozenset()
    usable = [p for p in points
              if p.tag not in excluded and p.iv > 0.0 and math.isfinite(p.iv) and p.weight > 0.0]

    if tenor <= 0.0:
        return SmileFit(0.0, 0.0, 0.0, tenor, forward, len(usable), 0.0, "non_positive_tenor")
    if len(usable) < min_points:
        # Fall back to a flat (or sloped) fit rather than failing outright.
        if not usable:
            return SmileFit(0.0, 0.0, 0.0, tenor, forward, 0, 0.0, "no_points")
        mean_w = sum(p.iv * p.iv * tenor * p.weight for p in usable) / sum(p.weight for p in usable)
        return SmileFit(mean_w, 0.0, 0.0, tenor, forward, len(usable), 0.0, "degenerate_flat")

    xs = [p.log_moneyness for p in usable]
    ws = [p.weight for p in usable]
    ys = [p.iv * p.iv * tenor for p in usable]  # total variance

    # Normal equations for [1, k, k^2].
    def s(power: int) -> float:
        return sum(w * (x ** power) for w, x in zip(ws, xs))

    def t(power: int) -> float:
        return sum(w * y * (x ** power) for w, x, y in zip(ws, xs, ys))

    matrix = [[s(0), s(1), s(2)],
              [s(1), s(2), s(3)],
              [s(2), s(3), s(4)]]
    solution = _solve_symmetric_3x3(matrix, [t(0), t(1), t(2)])
    if solution is None:
        mean_w = sum(y * w for y, w in zip(ys, ws)) / sum(ws)
        return SmileFit(mean_w, 0.0, 0.0, tenor, forward, len(usable), 0.0, "singular_fit")

    a, b, c = solution
    fit = SmileFit(a, b, c, tenor, forward, len(usable), 0.0)

    sq_err = 0.0
    for p in usable:
        sq_err += (fit.iv(p.log_moneyness) - p.iv) ** 2
    rmse = math.sqrt(sq_err / len(usable))
    return SmileFit(a, b, c, tenor, forward, len(usable), rmse)


def make_point(strike: float, forward: float, iv: float, vega_value: float,
               tag: str | None = None) -> SmilePoint:
    """Build a vega-weighted smile point."""
    return SmilePoint(
        strike=strike,
        log_moneyness=math.log(strike / forward),
        iv=iv,
        weight=max(vega_value, 1e-9),
        tag=tag,
    )
