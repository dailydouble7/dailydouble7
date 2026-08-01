"""Trading-calendar, tenor and OCC-symbol tests."""

from __future__ import annotations

import unittest
from datetime import date, datetime

from spxvol.occ import OptionContract, decode, encode
from spxvol.tenor import (
    calendar_tenor_years,
    expiry_moment,
    from_epoch_ms,
    sessions_to_expiry,
    tenor_years,
)
from spxvol.trading_calendar import (
    DEFAULT_CALENDAR as CAL,
    EASTERN,
    MINUTES_PER_YEAR,
    early_closes,
    easter,
    holidays,
)


class TestCalendar(unittest.TestCase):
    def test_easter_matches_known_dates(self):
        self.assertEqual(easter(2025), date(2025, 4, 20))
        self.assertEqual(easter(2026), date(2026, 4, 5))
        self.assertEqual(easter(2027), date(2027, 3, 28))

    def test_2026_holiday_set(self):
        expected = {
            date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
            date(2026, 5, 25), date(2026, 6, 19), date(2026, 9, 7), date(2026, 11, 26),
            date(2026, 12, 25),
        }
        self.assertEqual(set(holidays(2026)), expected)

    def test_saturday_independence_day_is_not_observed(self):
        # NYSE does not grant a Friday holiday when July 4 falls on a Saturday.
        self.assertEqual(date(2026, 7, 4).weekday(), 5)
        self.assertNotIn(date(2026, 7, 3), holidays(2026))
        self.assertTrue(CAL.is_session(date(2026, 7, 3)))

    def test_sunday_holiday_rolls_to_monday(self):
        self.assertEqual(date(2027, 12, 25).weekday(), 5)   # Saturday -> not observed
        self.assertNotIn(date(2027, 12, 24), holidays(2027))
        self.assertEqual(date(2022, 12, 25).weekday(), 6)   # Sunday -> Monday
        self.assertIn(date(2022, 12, 26), holidays(2022))

    def test_early_closes_are_half_sessions(self):
        self.assertIn(date(2026, 11, 27), early_closes(2026))  # day after Thanksgiving
        self.assertIn(date(2026, 12, 24), early_closes(2026))
        self.assertEqual(CAL.session_minutes(date(2026, 11, 27)), 210)

    def test_regular_session_is_390_minutes(self):
        self.assertEqual(CAL.session_minutes(date(2026, 6, 30)), 390)

    def test_weekends_and_holidays_are_not_sessions(self):
        self.assertFalse(CAL.is_session(date(2026, 6, 27)))  # Saturday
        self.assertFalse(CAL.is_session(date(2026, 12, 25)))

    def test_one_session_is_one_252nd_of_a_year(self):
        start = datetime(2026, 6, 30, 9, 30, tzinfo=EASTERN)
        end = datetime(2026, 6, 30, 16, 0, tzinfo=EASTERN)
        self.assertAlmostEqual(CAL.year_fraction(start, end), 1 / 252.0, places=12)

    def test_overnight_gap_contributes_no_trading_time(self):
        after_close = datetime(2026, 6, 30, 16, 0, tzinfo=EASTERN)
        before_open = datetime(2026, 7, 1, 9, 30, tzinfo=EASTERN)
        self.assertEqual(CAL.trading_minutes_between(after_close, before_open), 0.0)

    def test_minutes_span_a_holiday_correctly(self):
        # Thu 2026-12-24 is a half day, Fri 12-25 is a holiday, Mon 12-28 is full.
        start = datetime(2026, 12, 24, 9, 30, tzinfo=EASTERN)
        end = datetime(2026, 12, 28, 16, 0, tzinfo=EASTERN)
        self.assertEqual(CAL.trading_minutes_between(start, end), 210 + 390)

    def test_naive_datetimes_are_rejected(self):
        with self.assertRaises(ValueError):
            CAL.trading_minutes_between(datetime(2026, 6, 30, 10), datetime(2026, 6, 30, 11))


class TestTenor(unittest.TestCase):
    def test_am_settled_spx_expires_at_the_open(self):
        contract = OptionContract("SPX", date(2026, 6, 19), True, 6000)
        self.assertTrue(contract.is_am_settled)
        self.assertEqual(expiry_moment(contract).hour, 9)
        self.assertEqual(expiry_moment(contract).minute, 30)

    def test_pm_settled_spxw_expires_at_the_close(self):
        contract = OptionContract("SPXW", date(2026, 6, 30), True, 6000)
        self.assertFalse(contract.is_am_settled)
        self.assertEqual(expiry_moment(contract).hour, 16)

    def test_am_and_pm_tenors_differ_by_one_session_on_expiry_eve(self):
        # 2026-07-17 is the July monthly expiry. (June's would be 06-19, which is
        # Juneteenth -- a holiday, so no time accrues and the two conventions
        # would coincide.)
        expiry = date(2026, 7, 17)
        self.assertTrue(CAL.is_session(expiry))
        now = datetime(2026, 7, 16, 15, 0, tzinfo=EASTERN)
        am = tenor_years(OptionContract("SPX", expiry, True, 6000), now)
        pm = tenor_years(OptionContract("SPXW", expiry, True, 6000), now)
        self.assertGreater(pm, am)
        self.assertAlmostEqual((pm - am) * MINUTES_PER_YEAR, 390.0, places=6)
        self.assertAlmostEqual(am * MINUTES_PER_YEAR, 60.0, places=6)

    def test_expired_contract_has_zero_tenor(self):
        contract = OptionContract("SPXW", date(2026, 6, 30), True, 6000)
        self.assertEqual(tenor_years(contract, datetime(2026, 7, 1, 10, tzinfo=EASTERN)), 0.0)

    def test_zero_dte_tenor_is_small_but_positive(self):
        contract = OptionContract("SPXW", date(2026, 6, 30), True, 6000)
        now = datetime(2026, 6, 30, 15, 59, tzinfo=EASTERN)
        tenor = tenor_years(contract, now)
        self.assertGreater(tenor, 0.0)
        self.assertAlmostEqual(tenor * MINUTES_PER_YEAR, 1.0, places=9)

    def test_weekend_gap_accrues_calendar_time_but_no_trading_time(self):
        # The whole reason for trading time: a weekend burns 2.8 calendar days
        # and 90 trading minutes. Note the two clocks do NOT order consistently
        # over long spans -- July 2026 packs 23 sessions into 31 days, so trading
        # time actually runs faster there than ACT/365.
        contract = OptionContract("SPXW", date(2026, 7, 6), True, 6000)  # Monday
        now = datetime(2026, 7, 2, 15, 0, tzinfo=EASTERN)                # Thursday
        trading_minutes = tenor_years(contract, now) * MINUTES_PER_YEAR
        self.assertAlmostEqual(trading_minutes, 60 + 390 + 390, places=6)
        self.assertGreater(calendar_tenor_years(contract, now), tenor_years(contract, now))

    def test_sessions_to_expiry_counts_trading_days(self):
        contract = OptionContract("SPXW", date(2026, 7, 6), True, 6000)
        now = datetime(2026, 7, 2, 10, tzinfo=EASTERN)  # Thu; Fri 3rd is a session
        self.assertEqual(sessions_to_expiry(contract, now), 2)
        same_day = OptionContract("SPXW", date(2026, 7, 2), True, 6000)
        self.assertEqual(sessions_to_expiry(same_day, now), 0)

    def test_epoch_conversion_lands_in_eastern(self):
        stamp = datetime(2026, 6, 30, 13, 30, tzinfo=EASTERN)
        self.assertEqual(from_epoch_ms(int(stamp.timestamp() * 1000)), stamp)


class TestOCC(unittest.TestCase):
    def test_encode_matches_the_documented_example(self):
        self.assertEqual(encode("SPXW", date(2026, 6, 30), True, 7300), "O:SPXW260630C07300000")
        self.assertEqual(encode("SPY", date(2026, 6, 30), True, 725), "O:SPY260630C00725000")

    def test_decode_round_trips(self):
        for ticker in ("O:SPXW260630C07300000", "O:SPY260630P00725000", "O:SPX261218C06000000"):
            self.assertEqual(decode(ticker).ticker, ticker)

    def test_decode_without_prefix(self):
        self.assertEqual(decode("SPXW260630C07300000").strike, 7300.0)

    def test_fractional_strikes_survive_the_round_trip(self):
        # Binary float rounding would turn 7300.0 into 07299999 without Decimal.
        for strike in (7300.0, 6002.5, 725.25, 0.125):
            contract = OptionContract("SPXW", date(2026, 6, 30), True, strike)
            self.assertEqual(decode(contract.ticker).strike, strike)

    def test_counterpart_flips_only_the_right(self):
        call = OptionContract("SPXW", date(2026, 6, 30), True, 6000)
        put = call.counterpart()
        self.assertFalse(put.is_call)
        self.assertEqual((put.root, put.expiration, put.strike),
                         (call.root, call.expiration, call.strike))

    def test_am_settlement_is_root_sensitive(self):
        self.assertTrue(OptionContract("SPX", date(2026, 6, 19), True, 6000).is_am_settled)
        self.assertFalse(OptionContract("SPXW", date(2026, 6, 19), True, 6000).is_am_settled)

    def test_invalid_symbols_raise(self):
        for bad in ("", "O:", "SPXW", "O:SPXW26063XC07300000", "O:260630C07300000"):
            with self.assertRaises(ValueError):
                decode(bad)

    def test_encode_rejects_bad_inputs(self):
        with self.assertRaises(ValueError):
            encode("", date(2026, 6, 30), True, 6000)
        with self.assertRaises(ValueError):
            encode("SPXW", date(2026, 6, 30), True, 0)


if __name__ == "__main__":
    unittest.main()
