"""Two-layer scoring for large option prints.

The naive formulation of "paid above historical pricing for that delta" has a
failure mode worth designing around: without controlling for the prevailing vol
level, the model degenerates into a VIX detector and flags every trade on a
high-vol day. So the measurement is split in two:

**Layer 1 - aggression.** Was this print above the market *at that instant*?
Measured as the trade's implied vol minus a smile fitted to the same minute,
with the print's own contract left out of the fit. Timestamp-local, so it is
immune to the vol level entirely.

**Layer 2 - richness.** Was that delta expensive versus its own history?
Measured on the *normalised* skew (vol at the target delta minus ATM vol), so
the signal is about the shape of the surface rather than its height.

The two are independent and are reported separately. A trade that is high on
both is a buyer paying up for something that was already expensive.
"""

from __future__ import annotations

import math
from bisect import bisect_left
from dataclasses import dataclass, field
from datetime import date, datetime

from .bars import Bar
from .black76 import forward_delta, implied_vol, vega
from .occ import OptionContract
from .smile import SmileFit, SmilePoint, fit_smile

SPX_MULTIPLIER = 100.0

# Session buckets for grouping comparable tenors.
TENOR_BUCKETS: tuple[tuple[int, str], ...] = (
    (0, "0DTE"), (2, "1-2d"), (7, "3-7d"), (30, "8-30d"), (90, "31-90d"),
)
DELTA_BUCKETS: tuple[float, ...] = (0.05, 0.10, 0.15, 0.25, 0.35, 0.50)


def tenor_bucket(sessions: int) -> str:
    for limit, label in TENOR_BUCKETS:
        if sessions <= limit:
            return label
    return "90d+"


def delta_bucket(delta: float) -> float:
    """Nearest standard delta bucket, by absolute value."""
    target = abs(delta)
    return min(DELTA_BUCKETS, key=lambda b: abs(b - target))


def percentile_rank(sorted_values: list[float], value: float) -> float:
    """Fraction of ``sorted_values`` at or below ``value``, in [0, 1]."""
    if not sorted_values:
        return float("nan")
    return bisect_left(sorted_values, value) / len(sorted_values)


@dataclass
class SkewHistory:
    """Trailing distribution of normalised skew, keyed by tenor and delta bucket.

    Stores ``iv_at_delta - atm_iv`` rather than raw vol, which is what makes the
    percentile a statement about shape instead of level.
    """

    records: dict[tuple[str, float, bool], list[tuple[date, float]]] = field(default_factory=dict)
    atm_records: dict[str, list[tuple[date, float]]] = field(default_factory=dict)

    def observe(self, day: date, sessions: int, fit: SmileFit) -> None:
        """Record one day's smile shape across the standard delta buckets."""
        if not fit.ok or fit.tenor <= 0.0:
            return
        bucket = tenor_bucket(sessions)
        atm = fit.atm_iv
        self.atm_records.setdefault(bucket, []).append((day, atm))
        for target in DELTA_BUCKETS:
            for is_call in (True, False):
                try:
                    iv = fit.iv_at_delta(target if is_call else -target, is_call)
                except (ValueError, ZeroDivisionError):
                    continue
                if not math.isfinite(iv):
                    continue
                key = (bucket, target, is_call)
                self.records.setdefault(key, []).append((day, iv - atm))

    def _window(self, key: tuple[str, float, bool], before: date, lookback: int) -> list[float]:
        rows = self.records.get(key, [])
        values = [v for d, v in rows if d < before]
        return sorted(values[-lookback:]) if lookback else sorted(values)

    def rank_skew(self, sessions: int, delta: float, is_call: bool, skew_value: float,
                  before: date, *, lookback: int = 250) -> tuple[float, int]:
        """Percentile of a normalised skew reading versus its own trailing history.

        Returns ``(percentile, sample_size)``; percentile is NaN when the history
        is empty. Treat it as ordinal -- the history is rebuilt from traded
        prices, not quotes, so it is not a calibrated distribution.
        """
        key = (tenor_bucket(sessions), delta_bucket(delta), is_call)
        window = self._window(key, before, lookback)
        if not window:
            return float("nan"), 0
        return percentile_rank(window, skew_value), len(window)

    def rank_atm(self, sessions: int, atm_iv: float, before: date,
                 *, lookback: int = 250) -> tuple[float, int]:
        rows = self.atm_records.get(tenor_bucket(sessions), [])
        window = sorted(v for d, v in rows if d < before)[-lookback:] if rows else []
        if not window:
            return float("nan"), 0
        return percentile_rank(sorted(window), atm_iv), len(window)


@dataclass(frozen=True)
class ScoreWeights:
    """Composite weights. A starting point, not a calibrated model."""

    aggression: float = 0.5
    size: float = 0.3
    richness: float = 0.2


@dataclass(frozen=True)
class FlaggedTrade:
    """One large print, scored against the surface it traded into."""

    ticker: str
    timestamp: datetime
    root: str
    expiration: date
    strike: float
    is_call: bool

    contracts: float
    transactions: int | None
    avg_trade_size: float | None
    print_quality: str

    trade_price: float
    forward: float
    discount: float
    tenor: float
    sessions_to_expiry: int

    trade_iv: float
    fitted_iv: float
    delta: float

    residual_vol_pts: float
    vega_per_contract: float
    dollars_over_curve: float
    premium: float
    vega_notional: float

    smile_rmse_vol_pts: float
    smile_points: int
    forward_confidence: str

    skew_percentile: float = float("nan")
    skew_sample: int = 0
    atm_iv: float = float("nan")
    atm_percentile: float = float("nan")

    aggression_z: float = 0.0
    score: float = 0.0

    @property
    def direction_hint(self) -> str:
        """Inferred aggressor side.

        Bars give no trade direction, but a print above the fitted curve is
        consistent with a buyer lifting the offer and one below with a seller
        hitting the bid. This is a proxy: a large put printing "aggressively"
        may be one leg of a risk reversal or a hedge, not a directional bet.
        """
        if self.residual_vol_pts > 0:
            return "buyer_paid_up"
        if self.residual_vol_pts < 0:
            return "seller_hit_bid"
        return "flat"

    def to_row(self) -> dict[str, object]:
        return {
            "ticker": self.ticker,
            "timestamp": self.timestamp.isoformat(),
            "root": self.root,
            "expiration": self.expiration.isoformat(),
            "strike": self.strike,
            "right": "C" if self.is_call else "P",
            "contracts": self.contracts,
            "transactions": self.transactions,
            "avg_trade_size": self.avg_trade_size,
            "print_quality": self.print_quality,
            "trade_price": self.trade_price,
            "forward": round(self.forward, 4),
            "discount": round(self.discount, 8),
            "tenor_years": round(self.tenor, 8),
            "sessions_to_expiry": self.sessions_to_expiry,
            "trade_iv_pts": round(self.trade_iv * 100, 4),
            "fitted_iv_pts": round(self.fitted_iv * 100, 4),
            "delta": round(self.delta, 4),
            "residual_vol_pts": round(self.residual_vol_pts, 4),
            "dollars_over_curve": round(self.dollars_over_curve, 2),
            "premium": round(self.premium, 2),
            "vega_notional": round(self.vega_notional, 2),
            "smile_rmse_vol_pts": round(self.smile_rmse_vol_pts, 4),
            "smile_points": self.smile_points,
            "forward_confidence": self.forward_confidence,
            "skew_percentile": self.skew_percentile,
            "skew_sample": self.skew_sample,
            "atm_iv_pts": round(self.atm_iv * 100, 4) if math.isfinite(self.atm_iv) else None,
            "atm_percentile": self.atm_percentile,
            "aggression_z": round(self.aggression_z, 4),
            "score": round(self.score, 4),
            "direction_hint": self.direction_hint,
        }


def evaluate_print(bar: Bar, contract: OptionContract, *, forward: float, discount: float,
                   tenor: float, sessions: int, points: list[SmilePoint],
                   forward_confidence: str = "unknown",
                   history: SkewHistory | None = None,
                   day: date | None = None,
                   weights: ScoreWeights = ScoreWeights(),
                   multiplier: float = SPX_MULTIPLIER) -> FlaggedTrade | None:
    """Score a single large print against a leave-one-out smile.

    Returns ``None`` when the print's implied vol cannot be recovered (bound
    violation, stale leg, expired contract) -- those must not become fake
    observations.
    """
    trade_price = bar.price("print")
    if trade_price is None or tenor <= 0.0:
        return None

    inversion = implied_vol(trade_price, forward, contract.strike, tenor,
                            contract.is_call, discount)
    if not inversion.ok:
        return None
    trade_iv = inversion.vol
    assert trade_iv is not None

    # Leave-one-out: the print must not shape the curve that judges it.
    fit = fit_smile(points, tenor, forward, exclude_tags=frozenset({bar.ticker}))
    if not fit.ok and fit.reason not in ("degenerate_flat",):
        return None

    log_moneyness = math.log(contract.strike / forward)
    fitted_iv = fit.iv(log_moneyness)
    if not math.isfinite(fitted_iv) or fitted_iv <= 0.0:
        return None

    residual_pts = (trade_iv - fitted_iv) * 100.0
    delta = forward_delta(forward, contract.strike, tenor, trade_iv, contract.is_call)
    vega_contract = vega(forward, contract.strike, tenor, trade_iv, discount)

    dollars_over = (trade_iv - fitted_iv) * vega_contract * bar.volume * multiplier
    premium = trade_price * bar.volume * multiplier
    vega_notional = vega_contract * 0.01 * bar.volume * multiplier

    skew_pct, skew_n = float("nan"), 0
    atm_pct = float("nan")
    atm_iv = fit.atm_iv
    if history is not None and day is not None and math.isfinite(atm_iv):
        skew_pct, skew_n = history.rank_skew(sessions, delta, contract.is_call,
                                             fitted_iv - atm_iv, day)
        atm_pct, _ = history.rank_atm(sessions, atm_iv, day)

    # Normalise aggression by the smile's own fit noise: a 1-vol-point miss means
    # something different on a curve that fits to 0.1 than on one that fits to 2.
    noise_floor = max(fit.rmse_vol * 100.0, 0.10)
    aggression_z = residual_pts / noise_floor

    size_component = math.log10(max(vega_notional, 1.0)) / 5.0  # ~1.0 at $100k vega
    richness_component = skew_pct if math.isfinite(skew_pct) else 0.5
    score = (weights.aggression * max(aggression_z, 0.0)
             + weights.size * size_component
             + weights.richness * richness_component)

    return FlaggedTrade(
        ticker=bar.ticker,
        timestamp=bar.timestamp,
        root=contract.root,
        expiration=contract.expiration,
        strike=contract.strike,
        is_call=contract.is_call,
        contracts=bar.volume,
        transactions=bar.transactions,
        avg_trade_size=bar.avg_trade_size,
        print_quality=bar.print_quality,
        trade_price=trade_price,
        forward=forward,
        discount=discount,
        tenor=tenor,
        sessions_to_expiry=sessions,
        trade_iv=trade_iv,
        fitted_iv=fitted_iv,
        delta=delta,
        residual_vol_pts=residual_pts,
        vega_per_contract=vega_contract,
        dollars_over_curve=dollars_over,
        premium=premium,
        vega_notional=vega_notional,
        smile_rmse_vol_pts=fit.rmse_vol * 100.0,
        smile_points=fit.n_points,
        forward_confidence=forward_confidence,
        skew_percentile=skew_pct,
        skew_sample=skew_n,
        atm_iv=atm_iv,
        atm_percentile=atm_pct,
        aggression_z=aggression_z,
        score=score,
    )
