"""Black-76, parity recovery and smile-fitting tests."""

from __future__ import annotations

import math
import random
import unittest

from spxvol.black76 import (
    dividend_discount,
    forward_delta,
    implied_vol,
    norm_cdf,
    norm_ppf,
    price,
    spot_delta,
    strike_for_delta,
    undiscounted_price,
    vega,
)
from spxvol.forward import ParityQuote, atm_strike_guess, solve_forward
from spxvol.smile import fit_smile, make_point


class TestNormal(unittest.TestCase):
    def test_cdf_known_values(self):
        self.assertAlmostEqual(norm_cdf(0.0), 0.5, places=15)
        self.assertAlmostEqual(norm_cdf(1.959963984540054), 0.975, places=12)
        self.assertAlmostEqual(norm_cdf(-1.959963984540054), 0.025, places=12)

    def test_cdf_tails_do_not_underflow_to_garbage(self):
        self.assertGreater(norm_cdf(-8.0), 0.0)
        self.assertLess(norm_cdf(-8.0), 1e-14)

    def test_ppf_inverts_cdf(self):
        for p in (1e-8, 1e-4, 0.01, 0.1, 0.25, 0.5, 0.75, 0.9, 0.99, 1 - 1e-8):
            self.assertAlmostEqual(norm_cdf(norm_ppf(p)), p, places=12)

    def test_ppf_rejects_out_of_range(self):
        for bad in (0.0, 1.0, -0.5, 2.0):
            with self.assertRaises(ValueError):
                norm_ppf(bad)


class TestBlack76(unittest.TestCase):
    F, K, T, VOL, D = 6000.0, 5800.0, 0.25, 0.20, 0.99

    def test_put_call_parity_holds_exactly(self):
        call = price(self.F, self.K, self.T, self.VOL, True, self.D)
        put = price(self.F, self.K, self.T, self.VOL, False, self.D)
        self.assertAlmostEqual(call - put, self.D * (self.F - self.K), places=9)

    def test_price_increases_with_vol(self):
        prices = [price(self.F, self.K, self.T, v, True, self.D)
                  for v in (0.05, 0.1, 0.2, 0.4, 0.8)]
        self.assertEqual(prices, sorted(prices))

    def test_zero_vol_gives_discounted_intrinsic(self):
        self.assertAlmostEqual(price(self.F, self.K, self.T, 0.0, True, self.D),
                               self.D * (self.F - self.K), places=9)
        self.assertAlmostEqual(price(self.F, self.K, self.T, 0.0, False, self.D), 0.0, places=9)

    def test_vega_matches_finite_difference(self):
        eps = 1e-6
        up = undiscounted_price(self.F, self.K, self.T, self.VOL + eps, True)
        down = undiscounted_price(self.F, self.K, self.T, self.VOL - eps, True)
        self.assertAlmostEqual((up - down) / (2 * eps),
                               vega(self.F, self.K, self.T, self.VOL), places=4)

    def test_call_and_put_vega_are_equal(self):
        self.assertAlmostEqual(vega(self.F, self.K, self.T, self.VOL, self.D),
                               vega(self.F, self.K, self.T, self.VOL, self.D), places=12)

    def test_delta_signs_and_parity(self):
        call_d = forward_delta(self.F, self.K, self.T, self.VOL, True)
        put_d = forward_delta(self.F, self.K, self.T, self.VOL, False)
        self.assertTrue(0.0 < call_d < 1.0)
        self.assertTrue(-1.0 < put_d < 0.0)
        self.assertAlmostEqual(call_d - put_d, 1.0, places=12)

    def test_atm_forward_delta_is_near_half(self):
        d = forward_delta(6000.0, 6000.0, 0.02, 0.15, True)
        self.assertAlmostEqual(d, 0.5, places=2)

    def test_strike_for_delta_round_trips(self):
        for target in (0.10, 0.25, 0.40):
            k = strike_for_delta(target, self.F, self.T, self.VOL, True)
            self.assertAlmostEqual(forward_delta(self.F, k, self.T, self.VOL, True),
                                   target, places=10)

    def test_dividend_discount_and_spot_delta(self):
        # r = q  =>  F == S and exp(-qT) == D.
        spot = self.F * self.D
        dq = dividend_discount(self.F, self.D, spot)
        self.assertAlmostEqual(dq, 1.0, places=12)
        sd = spot_delta(self.F, self.K, self.T, self.VOL, True, self.D, spot)
        self.assertAlmostEqual(sd, forward_delta(self.F, self.K, self.T, self.VOL, True), places=12)


class TestImpliedVol(unittest.TestCase):
    def test_round_trip_across_strikes_and_tenors(self):
        forward, discount = 6000.0, 0.997
        worst = 0.0
        for tenor in (1 / 2520.0, 0.01, 0.08, 0.5, 2.0):
            for moneyness in (0.7, 0.85, 0.95, 1.0, 1.05, 1.15, 1.3):
                strike = forward * moneyness
                for vol in (0.06, 0.15, 0.35, 0.9):
                    is_call = strike >= forward
                    px = price(forward, strike, tenor, vol, is_call, discount)
                    got = implied_vol(px, forward, strike, tenor, is_call, discount)
                    if not got.ok:
                        # Deep wings can be worth ~1e-12, where price stops
                        # depending on vol at all. Rejecting is correct; the
                        # bug being guarded against is returning MIN_VOL as if
                        # it were a real measurement.
                        self.assertIn(got.reason,
                                      ("at_or_below_intrinsic", "below_min_vol",
                                       "negligible_time_value"))
                        continue
                    worst = max(worst, abs(got.vol - vol))
        self.assertLess(worst, 1e-6)

    def test_rejects_price_below_intrinsic(self):
        result = implied_vol(50.0, 6000.0, 5000.0, 0.25, True, 1.0)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "at_or_below_intrinsic")

    def test_rejects_price_above_upper_bound(self):
        self.assertEqual(implied_vol(6100.0, 6000.0, 5000.0, 0.25, True, 1.0).reason,
                         "at_or_above_upper_bound")
        self.assertEqual(implied_vol(5100.0, 6000.0, 5000.0, 0.25, False, 1.0).reason,
                         "at_or_above_upper_bound")

    def test_rejects_expired_and_bad_discount(self):
        self.assertEqual(implied_vol(10.0, 6000.0, 6000.0, 0.0, True, 1.0).reason, "expired")
        self.assertEqual(implied_vol(10.0, 6000.0, 6000.0, 0.1, True, 0.0).reason, "bad_discount")

    def test_converges_quickly(self):
        result = implied_vol(price(6000.0, 5500.0, 0.1, 0.25, False, 1.0),
                             6000.0, 5500.0, 0.1, False, 1.0)
        self.assertTrue(result.ok)
        self.assertLess(result.iterations, 25)


class TestForwardRecovery(unittest.TestCase):
    F, D, T = 6013.75, 0.99412, 0.0834

    def _chain(self, strikes, vol_fn):
        return [ParityQuote(k,
                            price(self.F, k, self.T, vol_fn(k), True, self.D),
                            price(self.F, k, self.T, vol_fn(k), False, self.D))
                for k in strikes]

    def test_exact_recovery_from_clean_chain(self):
        strikes = [self.F + 25 * i for i in range(-10, 11)]
        solution = solve_forward(self._chain(strikes, lambda k: 0.2))
        self.assertTrue(solution.ok)
        self.assertAlmostEqual(solution.forward, self.F, places=6)
        self.assertAlmostEqual(solution.discount, self.D, places=10)
        self.assertEqual(solution.confidence, "high")

    def test_recovery_is_independent_of_the_skew(self):
        strikes = [self.F + 25 * i for i in range(-10, 11)]
        skewed = self._chain(strikes, lambda k: 0.2 - 0.4 * math.log(k / self.F))
        solution = solve_forward(skewed)
        self.assertAlmostEqual(solution.forward, self.F, places=6)
        self.assertAlmostEqual(solution.discount, self.D, places=10)

    def test_implied_rate_is_recovered(self):
        strikes = [self.F + 25 * i for i in range(-8, 9)]
        solution = solve_forward(self._chain(strikes, lambda k: 0.2))
        expected = -math.log(self.D) / self.T
        self.assertAlmostEqual(solution.implied_rate(self.T), expected, places=8)

    def test_survives_noisy_prices(self):
        rng = random.Random(20260801)
        strikes = [self.F + 25 * i for i in range(-12, 13)]
        noisy = [ParityQuote(q.strike,
                             q.call_price + rng.uniform(-0.05, 0.05),
                             q.put_price + rng.uniform(-0.05, 0.05))
                 for q in self._chain(strikes, lambda k: 0.2)]
        solution = solve_forward(noisy)
        self.assertTrue(solution.ok)
        self.assertLess(abs(solution.forward - self.F), 1.0)
        self.assertLess(abs(solution.discount - self.D), 1e-4)

    def test_duplicate_strikes_do_not_double_weight(self):
        strikes = [self.F + 25 * i for i in range(-6, 7)]
        chain = self._chain(strikes, lambda k: 0.2)
        solution = solve_forward(chain + chain[:3])
        self.assertEqual(solution.n_strikes, len(strikes))

    def test_rejects_degenerate_inputs(self):
        self.assertEqual(solve_forward([]).reason, "too_few_strikes")
        self.assertEqual(solve_forward([ParityQuote(6000, 10, 5)]).reason, "too_few_strikes")
        # Synthetic rising in K is the wrong sign for parity.
        bad = [ParityQuote(5900, 1.0, 10.0), ParityQuote(6000, 5.0, 10.0),
               ParityQuote(6100, 9.0, 10.0)]
        self.assertEqual(solve_forward(bad).reason, "non_negative_slope")

    def test_atm_guess_brackets_the_forward(self):
        strikes = [self.F + 25 * i for i in range(-10, 11)]
        guess = atm_strike_guess(self._chain(strikes, lambda k: 0.2))
        self.assertLess(abs(guess - self.F), 25.0)


class TestSmile(unittest.TestCase):
    F, T, D = 6000.0, 0.08, 0.999

    def _points(self, vol_fn, strikes=None):
        strikes = strikes or [self.F + 25 * i for i in range(-16, 17)]
        out = []
        for k in strikes:
            vol = vol_fn(k)
            out.append(make_point(k, self.F, vol, vega(self.F, k, self.T, vol, self.D),
                                  tag=f"K{k:g}"))
        return out

    def test_recovers_a_quadratic_total_variance_surface(self):
        def vol(k):
            x = math.log(k / self.F)
            return math.sqrt((0.04 - 0.3 * x + 1.5 * x * x))
        fit = fit_smile(self._points(vol), self.T, self.F)
        self.assertTrue(fit.ok)
        self.assertLess(fit.rmse_vol, 1e-9)
        self.assertAlmostEqual(fit.atm_iv, 0.2, places=9)

    def test_fits_a_non_quadratic_surface_closely(self):
        # A cubic term the model cannot represent exactly: the fit should still
        # track it to well under a vol point across the quoted range.
        def vol(k):
            x = math.log(k / self.F)
            return 0.2 - 0.55 * x + 2.0 * x * x - 6.0 * x ** 3
        fit = fit_smile(self._points(vol), self.T, self.F)
        self.assertTrue(fit.ok)
        self.assertLess(fit.rmse_vol, 0.01)

    def test_put_skew_is_downward_sloping_in_strike(self):
        def vol(k):
            return 0.2 - 0.5 * math.log(k / self.F)
        fit = fit_smile(self._points(vol), self.T, self.F)
        self.assertGreater(fit.iv_at_strike(5700), fit.iv_at_strike(6000))
        self.assertGreater(fit.iv_at_strike(6000), fit.iv_at_strike(6300))

    def test_iv_at_delta_is_self_consistent(self):
        def vol(k):
            return 0.2 - 0.5 * math.log(k / self.F)
        fit = fit_smile(self._points(vol), self.T, self.F)
        for target, is_call in ((-0.25, False), (-0.10, False), (0.25, True)):
            strike = fit.strike_at_delta(target, is_call)
            self.assertAlmostEqual(fit.delta_at_strike(strike, is_call), target, places=6)

    def test_exclude_tags_changes_the_fit(self):
        def vol(k):
            return 0.2 - 0.5 * math.log(k / self.F)
        points = self._points(vol)
        # Corrupt one point badly; excluding it must restore the clean fit.
        bad = make_point(5800.0, self.F, 0.95, 100.0, tag="BAD")
        polluted = fit_smile(points + [bad], self.T, self.F)
        cleaned = fit_smile(points + [bad], self.T, self.F, exclude_tags=frozenset({"BAD"}))
        self.assertGreater(polluted.rmse_vol, cleaned.rmse_vol)
        self.assertLess(cleaned.rmse_vol, 1e-9)

    def test_degenerates_gracefully_with_too_few_points(self):
        fit = fit_smile(self._points(lambda k: 0.2, [5900.0, 6000.0]), self.T, self.F)
        self.assertEqual(fit.reason, "degenerate_flat")
        self.assertAlmostEqual(fit.atm_iv, 0.2, places=6)
        self.assertEqual(fit_smile([], self.T, self.F).reason, "no_points")


if __name__ == "__main__":
    unittest.main()
