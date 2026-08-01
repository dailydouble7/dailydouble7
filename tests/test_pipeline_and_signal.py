"""Bar handling, scoring, alignment and source-layer tests."""

from __future__ import annotations

import math
import unittest
from datetime import date, datetime, timedelta

from spxvol.bars import Bar, parse_bars
from spxvol.black76 import price as bs_price, vega
from spxvol.occ import OptionContract
from spxvol.pipeline import (
    align_bars,
    build_parity_quotes,
    build_smile_points,
    strike_grid,
)
from spxvol.signal import (
    SkewHistory,
    delta_bucket,
    evaluate_print,
    percentile_rank,
    tenor_bucket,
)
from spxvol.smile import fit_smile, make_point
from spxvol.source import chunk_ranges, normalize_bars
from spxvol.tenor import tenor_years
from spxvol.trading_calendar import EASTERN

FORWARD, DISCOUNT, TENOR = 6000.0, 0.999, 0.08


def synth_vol(strike: float) -> float:
    x = math.log(strike / FORWARD)
    return math.sqrt(max(0.04 - 0.30 * x + 1.5 * x * x, 1e-4))


def make_bar(ticker: str, ts: int, close: float, volume: float,
             transactions: int | None, vwap: float | None = None) -> Bar:
    return Bar(ticker=ticker, timestamp_ms=ts, open=close, high=close, low=close,
               close=close, volume=volume, vwap=vwap if vwap is not None else close,
               transactions=transactions)


class TestBars(unittest.TestCase):
    def test_single_print_is_detected(self):
        bar = make_bar("O:SPXW260630C06000000", 0, 12.5, 2500, 1)
        self.assertTrue(bar.is_single_print)
        self.assertEqual(bar.print_quality, "exact")
        self.assertEqual(bar.avg_trade_size, 2500.0)

    def test_print_quality_classes(self):
        self.assertEqual(make_bar("t", 0, 1.0, 5000, 3).print_quality, "block")
        self.assertEqual(make_bar("t", 0, 1.0, 500, 25).print_quality, "mixed")
        self.assertEqual(make_bar("t", 0, 1.0, 800, 400).print_quality, "retail")
        self.assertEqual(make_bar("t", 0, 1.0, 800, None).print_quality, "unknown")

    def test_is_large_requires_size_and_concentration(self):
        self.assertTrue(make_bar("t", 0, 1.0, 2500, 1).is_large())
        self.assertTrue(make_bar("t", 0, 1.0, 2500, 5).is_large())
        self.assertFalse(make_bar("t", 0, 1.0, 100, 1).is_large())    # too small
        self.assertFalse(make_bar("t", 0, 1.0, 2500, 900).is_large())  # retail churn

    def test_price_modes(self):
        bar = make_bar("t", 0, 10.0, 100, 5, vwap=9.5)
        self.assertEqual(bar.price("print"), 10.0)
        self.assertEqual(bar.price("vwap"), 9.5)
        # vwap falls back to close when absent or nonsensical.
        self.assertEqual(make_bar("t", 0, 10.0, 100, 5, vwap=0.0).price("vwap"), 10.0)
        with self.assertRaises(ValueError):
            bar.price("bogus")

    def test_parse_bars_sorts_and_drops_malformed_rows(self):
        rows = [
            {"ticker": "A", "timestamp_ms": 200, "close": 2.0, "volume": 1},
            {"ticker": "A", "timestamp_ms": 100, "close": 1.0, "volume": 1},
            {"ticker": None, "timestamp_ms": 300, "close": 3.0},
            {"ticker": "A", "close": 4.0},
        ]
        bars = parse_bars(rows)
        self.assertEqual([b.timestamp_ms for b in bars], [100, 200])

    def test_parse_accepts_raw_polygon_keys(self):
        bars = parse_bars([{"ticker": "A", "t": 1, "o": 1, "h": 2, "l": 0.5,
                            "c": 1.5, "v": 10, "vw": 1.4, "n": 3}])
        self.assertEqual(bars[0].close, 1.5)
        self.assertEqual(bars[0].vwap, 1.4)
        self.assertEqual(bars[0].transactions, 3)


class TestSourceHelpers(unittest.TestCase):
    def test_chunk_ranges_covers_the_span_without_overlap(self):
        chunks = chunk_ranges(date(2026, 1, 1), date(2026, 1, 12), 5)
        self.assertEqual(chunks[0], (date(2026, 1, 1), date(2026, 1, 5)))
        self.assertEqual(chunks[-1][1], date(2026, 1, 12))
        for earlier, later in zip(chunks, chunks[1:]):
            self.assertEqual(later[0] - earlier[1], timedelta(days=1))

    def test_chunk_ranges_single_day(self):
        self.assertEqual(chunk_ranges(date(2026, 1, 1), date(2026, 1, 1), 5),
                         [(date(2026, 1, 1), date(2026, 1, 1))])

    def test_chunk_ranges_validates(self):
        with self.assertRaises(ValueError):
            chunk_ranges(date(2026, 1, 5), date(2026, 1, 1), 5)
        with self.assertRaises(ValueError):
            chunk_ranges(date(2026, 1, 1), date(2026, 1, 5), 0)

    def test_normalize_bars_maps_polygon_shape(self):
        rows = normalize_bars({"ticker": "O:X", "results": [
            {"t": 1, "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 9, "vw": 1.4, "n": 2}]})
        self.assertEqual(rows[0]["ticker"], "O:X")
        self.assertEqual(rows[0]["transactions"], 2)

    def test_normalize_bars_tolerates_junk(self):
        self.assertEqual(normalize_bars(None), [])
        self.assertEqual(normalize_bars({"ticker": "X"}), [])
        self.assertEqual(normalize_bars({"ticker": "X", "results": "nope"}), [])


class TestAlignment(unittest.TestCase):
    def test_strike_grid_spans_the_band(self):
        grid = strike_grid(6000.0, 0.05, 25.0)
        self.assertGreaterEqual(min(grid), 5700.0)
        self.assertLessEqual(max(grid), 6300.0)
        self.assertEqual(grid[1] - grid[0], 25.0)

    def test_strike_grid_validates(self):
        for args in ((0, 0.05, 25), (6000, 0, 25), (6000, 0.05, 0)):
            with self.assertRaises(ValueError):
                strike_grid(*args)

    def test_align_carries_forward_within_tolerance(self):
        minute = 60_000
        bars = {"A": [make_bar("A", 0, 1.0, 10, 1), make_bar("A", 2 * minute, 1.2, 10, 1)],
                "B": [make_bar("B", 0, 2.0, 10, 1)]}
        stamps, aligned = align_bars(bars, max_stale_minutes=1)
        self.assertEqual(stamps, [0, 2 * minute])
        # B is 2 minutes stale at t=2m, beyond a 1-minute tolerance.
        self.assertIn("B", aligned[0])
        self.assertNotIn("B", aligned[2 * minute])

    def test_strict_alignment_admits_only_exact_stamps(self):
        minute = 60_000
        bars = {"A": [make_bar("A", 0, 1.0, 10, 1)],
                "B": [make_bar("B", minute, 2.0, 10, 1)]}
        _, aligned = align_bars(bars, max_stale_minutes=0)
        self.assertEqual(set(aligned[0]), {"A"})
        self.assertEqual(set(aligned[minute]), {"B"})

    def test_parity_quotes_pair_calls_with_puts(self):
        expiry = date(2026, 6, 30)
        contracts = {}
        snapshot = {}
        for strike in (5900.0, 6000.0, 6100.0):
            for is_call in (True, False):
                contract = OptionContract("SPXW", expiry, is_call, strike)
                contracts[contract.ticker] = contract
                px = bs_price(FORWARD, strike, TENOR, synth_vol(strike), is_call, DISCOUNT)
                snapshot[contract.ticker] = make_bar(contract.ticker, 0, px, 10, 1)
        quotes = build_parity_quotes(snapshot, contracts, [5900.0, 6000.0, 6100.0])
        self.assertEqual(len(quotes), 3)

    def test_parity_quotes_skip_unpaired_strikes(self):
        expiry = date(2026, 6, 30)
        call = OptionContract("SPXW", expiry, True, 6000.0)
        snapshot = {call.ticker: make_bar(call.ticker, 0, 50.0, 10, 1)}
        self.assertEqual(build_parity_quotes(snapshot, {call.ticker: call}, [6000.0]), [])

    def test_smile_points_keep_only_otm_contracts(self):
        expiry = date(2026, 6, 30)
        contracts, snapshot = {}, {}
        for strike in (5700.0, 5900.0, 6100.0, 6300.0):
            for is_call in (True, False):
                contract = OptionContract("SPXW", expiry, is_call, strike)
                contracts[contract.ticker] = contract
                px = bs_price(FORWARD, strike, TENOR, synth_vol(strike), is_call, DISCOUNT)
                snapshot[contract.ticker] = make_bar(contract.ticker, 0, px, 10, 1)
        points = build_smile_points(snapshot, contracts, FORWARD, DISCOUNT, TENOR)
        self.assertEqual(len(points), 4)  # one side per strike
        for point in points:
            contract = contracts[point.tag]
            is_otm = (contract.strike >= FORWARD) if contract.is_call else (contract.strike <= FORWARD)
            self.assertTrue(is_otm)
            self.assertAlmostEqual(point.iv, synth_vol(contract.strike), places=6)


class TestBucketsAndHistory(unittest.TestCase):
    def test_tenor_buckets(self):
        self.assertEqual(tenor_bucket(0), "0DTE")
        self.assertEqual(tenor_bucket(2), "1-2d")
        self.assertEqual(tenor_bucket(5), "3-7d")
        self.assertEqual(tenor_bucket(200), "90d+")

    def test_delta_buckets_snap_by_absolute_value(self):
        self.assertEqual(delta_bucket(-0.24), 0.25)
        self.assertEqual(delta_bucket(0.48), 0.50)
        self.assertEqual(delta_bucket(-0.06), 0.05)

    def test_percentile_rank(self):
        values = [1.0, 2.0, 3.0, 4.0]
        self.assertEqual(percentile_rank(values, 0.5), 0.0)
        self.assertEqual(percentile_rank(values, 3.0), 0.5)
        self.assertEqual(percentile_rank(values, 9.0), 1.0)
        self.assertTrue(math.isnan(percentile_rank([], 1.0)))

    def _fit_for(self, steepness: float):
        points = [make_point(k, FORWARD,
                             0.2 - steepness * math.log(k / FORWARD),
                             vega(FORWARD, k, TENOR,
                                  0.2 - steepness * math.log(k / FORWARD), DISCOUNT),
                             tag=f"K{k:g}")
                  for k in [FORWARD + 25 * i for i in range(-14, 15)]]
        return fit_smile(points, TENOR, FORWARD)

    def test_history_ranks_a_steeper_skew_as_richer(self):
        history = SkewHistory()
        base = date(2026, 1, 5)
        for i in range(40):
            history.observe(base + timedelta(days=i), 20, self._fit_for(0.40))
        fit = self._fit_for(0.80)
        skew = fit.iv_at_delta(-0.25, False) - fit.atm_iv
        pct, n = history.rank_skew(20, -0.25, False, skew, base + timedelta(days=60))
        self.assertEqual(n, 40)
        self.assertGreater(pct, 0.9)

    def test_history_is_empty_before_any_observation(self):
        pct, n = SkewHistory().rank_skew(20, -0.25, False, 0.05, date(2026, 1, 1))
        self.assertTrue(math.isnan(pct))
        self.assertEqual(n, 0)

    def test_history_excludes_same_day_and_future_observations(self):
        history = SkewHistory()
        day = date(2026, 3, 2)
        history.observe(day, 20, self._fit_for(0.40))
        _, n = history.rank_skew(20, -0.25, False, 0.05, day)
        self.assertEqual(n, 0)


class TestEvaluatePrint(unittest.TestCase):
    def setUp(self):
        self.expiry = date(2026, 6, 30)
        self.now = datetime(2026, 6, 1, 11, 0, tzinfo=EASTERN)
        self.tenor = tenor_years(OptionContract("SPXW", self.expiry, True, 6000), self.now)
        self.points = [
            make_point(k, FORWARD, synth_vol(k),
                       vega(FORWARD, k, self.tenor, synth_vol(k), DISCOUNT), tag=f"K{k:g}")
            for k in [FORWARD + 25 * i for i in range(-14, 15)]
        ]

    def _print_at(self, strike: float, vol_offset: float, size: float = 2000,
                  is_call: bool = False):
        contract = OptionContract("SPXW", self.expiry, is_call, strike)
        paid = synth_vol(strike) + vol_offset
        px = bs_price(FORWARD, strike, self.tenor, paid, is_call, DISCOUNT)
        bar = make_bar(contract.ticker, int(self.now.timestamp() * 1000), px, size, 1)
        points = self.points + [make_point(strike, FORWARD, paid,
                                           vega(FORWARD, strike, self.tenor, paid, DISCOUNT),
                                           tag=contract.ticker)]
        return evaluate_print(bar, contract, forward=FORWARD, discount=DISCOUNT,
                              tenor=self.tenor, sessions=20, points=points,
                              forward_confidence="high")

    def test_recovers_the_injected_richness(self):
        flag = self._print_at(5700.0, 0.02)
        self.assertIsNotNone(flag)
        self.assertAlmostEqual(flag.residual_vol_pts, 2.0, places=1)
        self.assertEqual(flag.direction_hint, "buyer_paid_up")

    def test_a_fairly_priced_print_shows_no_residual(self):
        flag = self._print_at(5700.0, 0.0)
        self.assertLess(abs(flag.residual_vol_pts), 0.05)

    def test_a_cheap_print_reads_as_seller_initiated(self):
        flag = self._print_at(5700.0, -0.02)
        self.assertLess(flag.residual_vol_pts, -1.0)
        self.assertEqual(flag.direction_hint, "seller_hit_bid")

    def test_leave_one_out_matters(self):
        # With the print included in its own fit the measured richness collapses.
        strike = 5700.0
        contract = OptionContract("SPXW", self.expiry, False, strike)
        paid = synth_vol(strike) + 0.02
        polluted = self.points + [make_point(strike, FORWARD, paid, 1e6, tag=contract.ticker)]
        contaminated = fit_smile(polluted, self.tenor, FORWARD)
        clean = fit_smile(polluted, self.tenor, FORWARD,
                          exclude_tags=frozenset({contract.ticker}))
        k = math.log(strike / FORWARD)
        self.assertLess(abs(contaminated.iv(k) - paid), abs(clean.iv(k) - paid))

    def test_dollars_over_curve_scales_with_size(self):
        small = self._print_at(5700.0, 0.02, size=1000)
        large = self._print_at(5700.0, 0.02, size=4000)
        self.assertAlmostEqual(large.dollars_over_curve / small.dollars_over_curve, 4.0, places=6)
        self.assertGreater(large.score, small.score)

    def test_delta_is_in_the_expected_range_for_a_downside_put(self):
        flag = self._print_at(5700.0, 0.02)
        self.assertLess(flag.delta, 0.0)
        self.assertGreater(flag.delta, -0.5)

    def test_unpriceable_print_returns_none(self):
        contract = OptionContract("SPXW", self.expiry, False, 5700.0)
        # A price above the strike is impossible for a put.
        bar = make_bar(contract.ticker, 0, 99999.0, 1000, 1)
        self.assertIsNone(evaluate_print(bar, contract, forward=FORWARD, discount=DISCOUNT,
                                         tenor=self.tenor, sessions=20, points=self.points))

    def test_expired_contract_returns_none(self):
        contract = OptionContract("SPXW", self.expiry, False, 5700.0)
        bar = make_bar(contract.ticker, 0, 10.0, 1000, 1)
        self.assertIsNone(evaluate_print(bar, contract, forward=FORWARD, discount=DISCOUNT,
                                         tenor=0.0, sessions=0, points=self.points))

    def test_row_serialises_to_plain_json_types(self):
        import json
        row = self._print_at(5700.0, 0.02).to_row()
        json.dumps(row)  # must not raise
        self.assertEqual(row["right"], "P")
        self.assertEqual(row["print_quality"], "exact")


if __name__ == "__main__":
    unittest.main()
