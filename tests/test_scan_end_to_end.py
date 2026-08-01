"""End-to-end scan against a synthetic in-memory source (no network).

Builds a full session of bars from a known surface, hides one deliberately rich
large print inside it, and checks that ``scan_session`` finds that print and
measures the richness it was constructed with.
"""

from __future__ import annotations

import math
import unittest
from datetime import date, datetime, timedelta

from spxvol.black76 import price as bs_price
from spxvol.occ import OptionContract, decode
from spxvol.pipeline import ScanConfig, scan_session
from spxvol.tenor import tenor_years
from spxvol.trading_calendar import EASTERN

SESSION = date(2026, 7, 2)
EXPIRY = date(2026, 7, 31)
ROOT = "SPXW"
TRUE_FORWARD = 6000.0
TRUE_DISCOUNT = 0.9979
RICH_STRIKE = 5750.0
RICH_VOL_BUMP = 0.02          # 2 vol points over fair
RICH_MINUTE = 3
RICH_SIZE = 2500.0


def true_vol(strike: float) -> float:
    """A downward-sloping put skew, quadratic in log-moneyness."""
    x = math.log(strike / TRUE_FORWARD)
    return math.sqrt(max(0.0324 - 0.28 * x + 1.4 * x * x, 1e-4))


class SyntheticSource:
    """Duck-typed stand-in for CvForgeSource, serving generated bars."""

    def __init__(self, minutes: int = 6):
        self.minutes = minutes
        self.calls: list[str] = []
        self.stamps = [
            int(datetime(SESSION.year, SESSION.month, SESSION.day, 10, m,
                         tzinfo=EASTERN).timestamp() * 1000)
            for m in range(minutes)
        ]

    def option_bars(self, ticker: str, start: date, end: date, interval: str = "1m",
                    **_: object) -> list[dict[str, object]]:
        self.calls.append(ticker)
        contract = decode(ticker)
        rows = []
        for index, stamp in enumerate(self.stamps):
            now = datetime.fromtimestamp(stamp / 1000, tz=EASTERN)
            tenor = tenor_years(contract, now)
            if tenor <= 0:
                continue
            fair_vol = true_vol(contract.strike)
            fair = bs_price(TRUE_FORWARD, contract.strike, tenor, fair_vol,
                            contract.is_call, TRUE_DISCOUNT)
            if fair <= 0.01:
                continue  # below a real tick; the venue would not print it

            is_rich = (index == RICH_MINUTE
                       and not contract.is_call
                       and contract.strike == RICH_STRIKE)
            if is_rich:
                paid = bs_price(TRUE_FORWARD, contract.strike, tenor,
                                fair_vol + RICH_VOL_BUMP, False, TRUE_DISCOUNT)
                # vwap stays at fair: the minute's other trades were at the market.
                rows.append(self._row(ticker, stamp, paid, RICH_SIZE, 1, fair))
            else:
                rows.append(self._row(ticker, stamp, fair, 40.0, 20, fair))
        return rows

    @staticmethod
    def _row(ticker, stamp, close, volume, transactions, vwap):
        return {"ticker": ticker, "timestamp_ms": stamp, "open": close, "high": close,
                "low": close, "close": close, "volume": volume, "vwap": vwap,
                "transactions": transactions}


class TestScanEndToEnd(unittest.TestCase):
    def setUp(self):
        self.source = SyntheticSource()
        self.config = ScanConfig(root=ROOT, expiration=EXPIRY, session=SESSION,
                                 center=TRUE_FORWARD, band_pct=0.05, strike_step=25.0)

    def test_scan_finds_exactly_the_planted_print(self):
        scan = scan_session(self.source, self.config)
        self.assertEqual(len(scan.flags), 1, f"rejects={scan.rejects}")
        flag = scan.flags[0]
        self.assertEqual(flag.strike, RICH_STRIKE)
        self.assertFalse(flag.is_call)
        self.assertEqual(flag.contracts, RICH_SIZE)
        self.assertEqual(flag.print_quality, "exact")

    def test_measured_richness_matches_what_was_injected(self):
        flag = scan_session(self.source, self.config).flags[0]
        self.assertAlmostEqual(flag.residual_vol_pts, RICH_VOL_BUMP * 100, delta=0.25)
        self.assertEqual(flag.direction_hint, "buyer_paid_up")
        self.assertGreater(flag.dollars_over_curve, 0.0)

    def test_forward_and_discount_are_recovered_from_parity_alone(self):
        flag = scan_session(self.source, self.config).flags[0]
        self.assertAlmostEqual(flag.forward, TRUE_FORWARD, delta=0.05)
        self.assertAlmostEqual(flag.discount, TRUE_DISCOUNT, delta=1e-5)
        self.assertEqual(flag.forward_confidence, "high")

    def test_every_minute_produces_a_forward_and_a_smile(self):
        scan = scan_session(self.source, self.config)
        self.assertEqual(scan.minutes_examined, self.source.minutes)
        self.assertEqual(scan.minutes_with_forward, self.source.minutes)
        self.assertEqual(scan.minutes_with_smile, self.source.minutes)

    def test_it_fetches_both_rights_for_every_strike(self):
        scan_session(self.source, self.config)
        strikes = self.config.strikes()
        self.assertEqual(len(self.source.calls), 2 * len(strikes))
        self.assertEqual(len(set(self.source.calls)), 2 * len(strikes))

    def test_ordinary_retail_flow_is_not_flagged(self):
        # Same surface with the planted print removed: nothing should survive.
        source = SyntheticSource()
        source.option_bars = lambda ticker, start, end, interval="1m", **kw: [
            row for row in SyntheticSource.option_bars(source, ticker, start, end, interval)
            if row["transactions"] != 1
        ]
        self.assertEqual(scan_session(source, self.config).flags, [])

    def test_raising_the_size_threshold_filters_the_print_out(self):
        config = ScanConfig(root=ROOT, expiration=EXPIRY, session=SESSION,
                            center=TRUE_FORWARD, band_pct=0.05, strike_step=25.0,
                            min_volume=RICH_SIZE * 2)
        self.assertEqual(scan_session(self.source, config).flags, [])

    def test_a_dead_contract_does_not_abort_the_scan(self):
        source = SyntheticSource()
        original = source.option_bars
        dead = OptionContract(ROOT, EXPIRY, True, 6100.0).ticker

        def flaky(ticker, start, end, interval="1m", **kw):
            if ticker == dead:
                raise RuntimeError("upstream 502")
            return original(ticker, start, end, interval)

        source.option_bars = flaky
        scan = scan_session(source, self.config)
        self.assertEqual(len(scan.flags), 1)
        self.assertEqual(scan.contracts_fetched, 2 * len(self.config.strikes()) - 1)

    def test_flags_serialise(self):
        import json
        rows = [f.to_row() for f in scan_session(self.source, self.config).flags]
        self.assertEqual(json.loads(json.dumps(rows))[0]["right"], "P")


class TestScanWithHistory(unittest.TestCase):
    def test_layer_two_percentile_is_attached_when_history_exists(self):
        from spxvol.signal import SkewHistory
        from spxvol.smile import fit_smile, make_point
        from spxvol.black76 import vega

        now = datetime(SESSION.year, SESSION.month, SESSION.day, 10, 0, tzinfo=EASTERN)
        tenor = tenor_years(OptionContract(ROOT, EXPIRY, True, 6000), now)

        def fit_with(steepness: float):
            pts = []
            for k in [TRUE_FORWARD + 25 * i for i in range(-12, 13)]:
                vol = 0.18 - steepness * math.log(k / TRUE_FORWARD)
                pts.append(make_point(k, TRUE_FORWARD, vol,
                                      vega(TRUE_FORWARD, k, tenor, vol, TRUE_DISCOUNT),
                                      tag=f"K{k:g}"))
            return fit_smile(pts, tenor, TRUE_FORWARD)

        history = SkewHistory()
        for i in range(60):
            history.observe(SESSION - timedelta(days=90 - i), 20, fit_with(0.20))

        source = SyntheticSource()
        config = ScanConfig(root=ROOT, expiration=EXPIRY, session=SESSION,
                            center=TRUE_FORWARD, band_pct=0.05, strike_step=25.0)
        flags = scan_session(source, config, history=history).flags
        self.assertEqual(len(flags), 1)
        # The live surface is far steeper than the flat history, so the skew at
        # this delta should rank at the very top of its trailing distribution.
        self.assertGreater(flags[0].skew_sample, 0)
        self.assertGreater(flags[0].skew_percentile, 0.9)


if __name__ == "__main__":
    unittest.main()
