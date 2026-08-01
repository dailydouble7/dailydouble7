"""Orchestration: strike grids, minute alignment, session scans, history builds."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable, Iterable, Sequence

from .bars import Bar, parse_bars
from .black76 import implied_vol, vega
from .forward import ParityQuote, ForwardSolution, atm_strike_guess, solve_forward
from .occ import OptionContract
from .signal import (
    FlaggedTrade,
    ScoreWeights,
    SkewHistory,
    evaluate_print,
)
from .smile import SmilePoint, fit_smile, make_point
from .source import CvForgeSource
from .tenor import from_epoch_ms, sessions_to_expiry, tenor_years
from .trading_calendar import DEFAULT_CALENDAR, TradingCalendar

ProgressFn = Callable[[str], None]


def strike_grid(center: float, band_pct: float, step: float) -> list[float]:
    """Strikes spanning +/- ``band_pct`` around ``center``, snapped to ``step``."""
    if center <= 0 or step <= 0 or band_pct <= 0:
        raise ValueError("center, band_pct and step must be positive")
    lo = center * (1.0 - band_pct)
    hi = center * (1.0 + band_pct)
    first = math.ceil(lo / step) * step
    out = []
    k = first
    while k <= hi + 1e-9:
        out.append(round(k, 4))
        k += step
    return out


@dataclass
class ScanConfig:
    """Inputs for a single-session intraday scan."""

    root: str
    expiration: date
    session: date
    center: float
    band_pct: float = 0.05
    strike_step: float = 25.0
    interval: str = "1m"

    # Large-print thresholds.
    min_volume: float = 250.0
    min_avg_size: float = 50.0

    # Quality gates.
    min_parity_strikes: int = 4
    min_smile_points: int = 5
    max_stale_minutes: int = 1
    min_forward_confidence: str = "low"

    multiplier: float = 100.0
    weights: ScoreWeights = field(default_factory=ScoreWeights)
    calendar: TradingCalendar = field(default_factory=lambda: DEFAULT_CALENDAR)

    def strikes(self) -> list[float]:
        return strike_grid(self.center, self.band_pct, self.strike_step)

    def contracts(self) -> list[OptionContract]:
        return [OptionContract(self.root, self.expiration, is_call, k)
                for k in self.strikes() for is_call in (True, False)]


_CONFIDENCE_ORDER = {"none": 0, "low": 1, "medium": 2, "high": 3}


def fetch_contract_bars(source: CvForgeSource, contracts: Sequence[OptionContract],
                        start: date, end: date, interval: str,
                        progress: ProgressFn | None = None) -> dict[str, list[Bar]]:
    """Fetch and parse bars for every contract, skipping ones with no data."""
    out: dict[str, list[Bar]] = {}
    for index, contract in enumerate(contracts, start=1):
        ticker = contract.ticker
        if progress:
            progress(f"[{index}/{len(contracts)}] {ticker}")
        try:
            rows = source.option_bars(ticker, start, end, interval)
        except Exception as exc:  # noqa: BLE001 - one dead contract must not kill a scan
            if progress:
                progress(f"  ! {ticker}: {exc}")
            continue
        bars = parse_bars(rows)
        if bars:
            out[ticker] = bars
    return out


def align_bars(bars_by_ticker: dict[str, list[Bar]], max_stale_minutes: int = 1
               ) -> tuple[list[int], dict[int, dict[str, Bar]]]:
    """Build a per-timestamp view, optionally carrying a bar forward when stale.

    Sparse strikes do not print every minute. Allowing a small carry-forward
    keeps coverage usable, but staleness biases parity directly, so the window
    is deliberately tiny and configurable.
    """
    timestamps = sorted({bar.timestamp_ms for bars in bars_by_ticker.values() for bar in bars})
    tolerance_ms = max_stale_minutes * 60_000
    aligned: dict[int, dict[str, Bar]] = {ts: {} for ts in timestamps}

    for ticker, bars in bars_by_ticker.items():
        stamps = [b.timestamp_ms for b in bars]
        cursor = 0
        latest: Bar | None = None
        for ts in timestamps:
            while cursor < len(stamps) and stamps[cursor] <= ts:
                latest = bars[cursor]
                cursor += 1
            if latest is None:
                continue
            if ts - latest.timestamp_ms <= tolerance_ms:
                aligned[ts][ticker] = latest
    return timestamps, aligned


def build_parity_quotes(snapshot: dict[str, Bar], contracts_by_ticker: dict[str, OptionContract],
                        strikes: Iterable[float]) -> list[ParityQuote]:
    """Pair calls and puts at each strike using vwap (a mid-like central price)."""
    calls: dict[float, Bar] = {}
    puts: dict[float, Bar] = {}
    for ticker, bar in snapshot.items():
        contract = contracts_by_ticker.get(ticker)
        if contract is None:
            continue
        (calls if contract.is_call else puts)[contract.strike] = bar

    quotes = []
    for strike in strikes:
        call = calls.get(strike)
        put = puts.get(strike)
        if call is None or put is None:
            continue
        call_price = call.price("vwap")
        put_price = put.price("vwap")
        if call_price is None or put_price is None or call_price <= 0 or put_price <= 0:
            continue
        quotes.append(ParityQuote(strike, call_price, put_price))
    return quotes


def build_smile_points(snapshot: dict[str, Bar], contracts_by_ticker: dict[str, OptionContract],
                       forward: float, discount: float, tenor: float) -> list[SmilePoint]:
    """Invert every available contract to IV and vega-weight it for the fit."""
    points: list[SmilePoint] = []
    for ticker, bar in snapshot.items():
        contract = contracts_by_ticker.get(ticker)
        if contract is None:
            continue
        price = bar.price("vwap")
        if price is None or price <= 0:
            continue
        result = implied_vol(price, forward, contract.strike, tenor, contract.is_call, discount)
        if not result.ok or result.vol is None:
            continue
        # Out-of-the-money options carry the cleaner vol signal; ITM prices are
        # dominated by intrinsic and their inversion amplifies price noise.
        is_otm = (contract.strike >= forward) if contract.is_call else (contract.strike <= forward)
        if not is_otm:
            continue
        weight = vega(forward, contract.strike, tenor, result.vol, discount)
        if weight <= 0:
            continue
        points.append(make_point(contract.strike, forward, result.vol, weight, tag=ticker))
    return points


@dataclass
class SessionScan:
    """Result of scanning one session."""

    flags: list[FlaggedTrade] = field(default_factory=list)
    minutes_examined: int = 0
    minutes_with_forward: int = 0
    minutes_with_smile: int = 0
    contracts_fetched: int = 0
    rejects: dict[str, int] = field(default_factory=dict)

    def note_reject(self, reason: str) -> None:
        self.rejects[reason] = self.rejects.get(reason, 0) + 1


def scan_session(source: CvForgeSource, config: ScanConfig, *,
                 history: SkewHistory | None = None,
                 progress: ProgressFn | None = None) -> SessionScan:
    """Scan one session for large prints that paid above the prevailing smile."""
    contracts = config.contracts()
    contracts_by_ticker = {c.ticker: c for c in contracts}
    strikes = config.strikes()

    bars_by_ticker = fetch_contract_bars(source, contracts, config.session, config.session,
                                         config.interval, progress)
    scan = SessionScan(contracts_fetched=len(bars_by_ticker))
    if not bars_by_ticker:
        return scan

    timestamps, aligned = align_bars(bars_by_ticker, config.max_stale_minutes)
    scan.minutes_examined = len(timestamps)
    min_conf = _CONFIDENCE_ORDER.get(config.min_forward_confidence, 1)
    reference = contracts[0]

    for ts in timestamps:
        snapshot = aligned.get(ts, {})
        if not snapshot:
            continue

        quotes = build_parity_quotes(snapshot, contracts_by_ticker, strikes)
        if len(quotes) < config.min_parity_strikes:
            scan.note_reject("too_few_parity_strikes")
            continue

        solution: ForwardSolution = solve_forward(quotes)
        if not solution.ok or solution.forward is None or solution.discount is None:
            scan.note_reject(f"forward_{solution.reason}")
            continue
        if _CONFIDENCE_ORDER.get(solution.confidence, 0) < min_conf:
            scan.note_reject("low_forward_confidence")
            continue
        scan.minutes_with_forward += 1

        now = from_epoch_ms(ts)
        tenor = tenor_years(reference, now, config.calendar)
        if tenor <= 0.0:
            scan.note_reject("expired")
            continue
        sessions = sessions_to_expiry(reference, now, config.calendar)

        points = build_smile_points(snapshot, contracts_by_ticker,
                                    solution.forward, solution.discount, tenor)
        if len(points) < config.min_smile_points:
            scan.note_reject("too_few_smile_points")
            continue
        scan.minutes_with_smile += 1

        for ticker, bar in snapshot.items():
            # Only score a bar stamped at this minute; carried-forward bars were
            # already scored when they were fresh.
            if bar.timestamp_ms != ts:
                continue
            if not bar.is_large(min_volume=config.min_volume, min_avg_size=config.min_avg_size):
                continue
            contract = contracts_by_ticker.get(ticker)
            if contract is None:
                continue
            flag = evaluate_print(
                bar, contract,
                forward=solution.forward, discount=solution.discount,
                tenor=tenor, sessions=sessions, points=points,
                forward_confidence=solution.confidence,
                history=history, day=config.session,
                weights=config.weights, multiplier=config.multiplier,
            )
            if flag is None:
                scan.note_reject("uninvertible_print")
                continue
            scan.flags.append(flag)

    scan.flags.sort(key=lambda f: f.score, reverse=True)
    return scan


def build_history(source: CvForgeSource, root: str, expirations: Sequence[date],
                  center: float, start: date, end: date, *,
                  band_pct: float = 0.08, strike_step: float = 25.0,
                  calendar: TradingCalendar = DEFAULT_CALENDAR,
                  progress: ProgressFn | None = None) -> SkewHistory:
    """Build the Layer-2 trailing skew distribution from daily bars.

    Uses ``1d`` bars deliberately: one call per contract covers the whole
    lookback window, so a year of history costs roughly as many calls as a
    single intraday session. Minute data would be a hundred times the cost for
    a distribution that only needs one observation per day.
    """
    history = SkewHistory()
    strikes = strike_grid(center, band_pct, strike_step)

    for expiration in expirations:
        contracts = [OptionContract(root, expiration, is_call, k)
                     for k in strikes for is_call in (True, False)]
        contracts_by_ticker = {c.ticker: c for c in contracts}
        if progress:
            progress(f"history {root} {expiration}: {len(contracts)} contracts")
        bars_by_ticker = fetch_contract_bars(source, contracts, start, end, "1d", progress)
        if not bars_by_ticker:
            continue

        timestamps, aligned = align_bars(bars_by_ticker, max_stale_minutes=0)
        for ts in timestamps:
            snapshot = aligned.get(ts, {})
            quotes = build_parity_quotes(snapshot, contracts_by_ticker, strikes)
            if len(quotes) < 4:
                continue
            solution = solve_forward(quotes)
            if not solution.ok or solution.forward is None or solution.discount is None:
                continue
            now = from_epoch_ms(ts)
            reference = contracts[0]
            tenor = tenor_years(reference, now, calendar)
            if tenor <= 0.0:
                continue
            points = build_smile_points(snapshot, contracts_by_ticker,
                                        solution.forward, solution.discount, tenor)
            if len(points) < 5:
                continue
            fit = fit_smile(points, tenor, solution.forward)
            if not fit.ok:
                continue
            history.observe(now.date(), sessions_to_expiry(reference, now, calendar), fit)
    return history


def infer_center(source: CvForgeSource, root: str, expiration: date, session: date,
                 approx_center: float, *, band_pct: float = 0.10,
                 coarse_step: float = 100.0) -> float | None:
    """Locate the forward with a coarse grid, using ``C - P = 0`` at ``K = F``.

    A cheap way to centre a fine grid when the index level is not to hand: the
    synthetic crosses zero exactly at the forward, so the strike minimising
    ``|C - P|`` is adjacent to it.
    """
    strikes = strike_grid(approx_center, band_pct, coarse_step)
    contracts = [OptionContract(root, expiration, is_call, k)
                 for k in strikes for is_call in (True, False)]
    contracts_by_ticker = {c.ticker: c for c in contracts}
    bars_by_ticker = fetch_contract_bars(source, contracts, session, session, "1d")
    if not bars_by_ticker:
        return None
    timestamps, aligned = align_bars(bars_by_ticker, max_stale_minutes=0)
    if not timestamps:
        return None
    snapshot = aligned[timestamps[-1]]
    quotes = build_parity_quotes(snapshot, contracts_by_ticker, strikes)
    if len(quotes) >= 3:
        solution = solve_forward(quotes)
        if solution.ok and solution.forward is not None:
            return solution.forward
    return atm_strike_guess(quotes)


def session_range(end: date, sessions: int, calendar: TradingCalendar = DEFAULT_CALENDAR) -> date:
    """Date ``sessions`` trading days before ``end``."""
    day = end
    remaining = sessions
    while remaining > 0:
        day -= timedelta(days=1)
        if calendar.is_session(day):
            remaining -= 1
    return day
