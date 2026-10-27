"""Offline tests for market/market_calendar.py: sessions, early closes, coverage, IBKR hours.

Expected dates are NYSE's published 2026-2027 schedule
(https://www.nyse.com/markets/hours-calendars, checked 2026-10-27).
Run: python -m unittest discover -s tests -p test_market_calendar.py -v
"""
import os

os.environ["PAPERBOT_NOTIFY"] = "off"  # tests must never send real notifications

import asyncio
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock
from zoneinfo import ZoneInfo

from paperbot.market import market_calendar as mc

ET = mc.ET
SGT = ZoneInfo("Asia/Singapore")
NYSE_HOLIDAYS = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19",
    "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18",
    "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
}
NYSE_EARLY_CLOSES = {"2026-11-27", "2026-12-24", "2027-11-26"}


def d(text: str) -> dt.date:
    return dt.date.fromisoformat(text)


def at(day: str, hour: int, minute: int, second: int = 0, tz=ET) -> dt.datetime:
    return dt.datetime.combine(d(day), dt.time(hour, minute, second), tz)


class CalendarFileMixin:
    """A temp calendar file; IBKR overrides cleared before and after each test."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "market_calendar.json"
        self.raw = json.loads(mc.CALENDAR_FILE.read_text(encoding="utf-8"))
        mc._ibkr.clear()

    def tearDown(self):
        mc._ibkr.clear()
        self.tmp.cleanup()

    def write(self, **changes):
        self.path.write_text(json.dumps({**self.raw, **changes}), encoding="utf-8")
        return self.path


class ShippedCalendarTest(unittest.TestCase):
    """config/market_calendar.json as shipped, against NYSE's published schedule."""

    def setUp(self):
        mc._ibkr.clear()

    def test_file_is_valid_and_matches_nyse(self):
        cal = mc.load()
        self.assertEqual({str(x) for x in cal["holidays"]}, NYSE_HOLIDAYS)
        self.assertEqual({str(x) for x in cal["early_closes"]}, NYSE_EARLY_CLOSES)
        self.assertEqual(cal["years"], {2026, 2027})

    def test_every_holiday_is_closed(self):
        for text in sorted(NYSE_HOLIDAYS):
            with self.subTest(day=text):
                day = mc.session(d(text))
                self.assertEqual(day.kind, "holiday")
                self.assertFalse(day.trading)
                self.assertFalse(day.is_open(at(text, 12, 0)))

    def test_every_early_close_ends_at_1300(self):
        for text in sorted(NYSE_EARLY_CLOSES):
            with self.subTest(day=text):
                day = mc.session(d(text))
                self.assertEqual((day.kind, day.open, day.close),
                                 ("early_close", dt.time(9, 30), dt.time(13, 0)))
                self.assertTrue(day.is_open(at(text, 12, 59, 59)))
                self.assertFalse(day.is_open(at(text, 13, 0)))

    def test_normal_weekdays(self):
        for text in ("2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30"):
            with self.subTest(day=text):
                self.assertEqual(mc.session(d(text)).kind, "regular")

    def test_weekend(self):
        for text in ("2026-10-31", "2026-11-01", "2028-01-01"):  # also outside coverage
            with self.subTest(day=text):
                self.assertEqual(mc.session(d(text)).kind, "weekend")

    def test_regular_boundaries_to_the_second(self):
        day = mc.session(d("2026-10-27"))
        self.assertFalse(day.is_open(at("2026-10-27", 9, 29, 59)))
        self.assertTrue(day.is_open(at("2026-10-27", 9, 30)))
        self.assertTrue(day.is_open(at("2026-10-27", 15, 59, 59)))
        self.assertFalse(day.is_open(at("2026-10-27", 16, 0)))

    def test_new_years_eve_2027_trades_normally(self):
        """1 Jan 2028 is a Saturday: NYSE does not close on Friday 31 Dec 2027."""
        self.assertEqual(mc.session(d("2027-12-31")).kind, "regular")

    def test_date_outside_the_covered_years_is_unknown(self):
        for text in ("2025-12-31", "2028-01-03"):
            with self.subTest(day=text):
                day = mc.session(d(text))
                self.assertEqual(day.kind, "unknown")
                self.assertFalse(day.is_open(at(text, 11, 0)))
                self.assertIn("covered_years", day.reason)

    def test_daylight_saving_changes(self):
        """The ET date and wall time come from zoneinfo on both sides of a switch."""
        # US DST ends Sun 1 Nov 2026 and starts Sun 14 Mar 2027
        before = dt.datetime(2026, 10, 30, 13, 30, tzinfo=dt.timezone.utc)  # 09:30 EDT
        after = dt.datetime(2026, 11, 2, 14, 30, tzinfo=dt.timezone.utc)  # 09:30 EST
        spring = dt.datetime(2027, 3, 15, 13, 30, tzinfo=dt.timezone.utc)  # 09:30 EDT
        for moment in (before, after, spring):
            with self.subTest(moment=moment):
                self.assertTrue(mc.session_at(moment).is_open(moment))
                self.assertFalse(mc.session_at(moment).is_open(moment - dt.timedelta(seconds=1)))
        self.assertFalse(mc.session_at(after).is_open(dt.datetime(2026, 11, 2, 13, 30,
                                                                  tzinfo=dt.timezone.utc)))

    def test_singapore_clock_uses_the_new_york_date(self):
        # Friday 27 Nov 2026 00:30 SGT is Thursday 26 Nov 11:30 ET: Thanksgiving, closed
        sg = dt.datetime(2026, 11, 27, 0, 30, tzinfo=SGT)
        self.assertEqual(mc.session_at(sg).kind, "holiday")
        # Saturday 28 Nov 01:00 SGT is Friday 27 Nov 12:00 ET: early close, open
        sg = dt.datetime(2026, 11, 28, 1, 0, tzinfo=SGT)
        self.assertEqual(mc.session_at(sg).kind, "early_close")
        self.assertTrue(mc.session_at(sg).is_open(sg))
        # Saturday 28 Nov 02:30 SGT is 13:30 ET: after the early close
        sg = dt.datetime(2026, 11, 28, 2, 30, tzinfo=SGT)
        self.assertFalse(mc.session_at(sg).is_open(sg))
        # Monday 21:30 SGT (UTC+8) is 08:30 ET in winter: before the open
        sg = dt.datetime(2026, 11, 30, 21, 30, tzinfo=SGT)
        self.assertFalse(mc.session_at(sg).is_open(sg))

    def test_next_trading_day_skips_the_long_weekend(self):
        self.assertEqual(mc.next_trading_day(d("2026-11-25")).day, d("2026-11-27"))
        self.assertEqual(mc.next_trading_day(d("2026-12-24")).day, d("2026-12-28"))


class ShiftTest(unittest.TestCase):
    def test_times_after_an_early_close_keep_their_distance_to_it(self):
        early = mc.session(d("2026-11-27"))
        self.assertEqual(mc.shift(dt.time(15, 51), early), dt.time(12, 51))
        self.assertEqual(mc.shift(dt.time(15, 15), early), dt.time(12, 15))
        self.assertEqual(mc.shift(dt.time(13, 0), early), dt.time(10, 0))
        self.assertEqual(mc.shift(dt.time(9, 45), early), dt.time(9, 45))  # before the close
        self.assertEqual(mc.shift(dt.time(12, 59), early), dt.time(12, 59))
        self.assertIsNone(mc.shift(None, early))

    def test_regular_and_closed_days_unchanged(self):
        for text in ("2026-10-27", "2026-11-26", "2026-10-31"):
            self.assertEqual(mc.shift(dt.time(15, 51), mc.session(d(text))), dt.time(15, 51))


class CalendarFileTest(CalendarFileMixin, unittest.TestCase):
    def test_missing_file_fails_closed(self):
        day = mc.session(d("2026-10-27"), Path(self.tmp.name) / "nope.json")
        self.assertEqual(day.kind, "unknown")
        self.assertIn("calendar unavailable", day.reason)

    def test_corrupt_file_fails_closed(self):
        self.path.write_text("{trunc", encoding="utf-8")
        self.assertEqual(mc.session(d("2026-10-27"), self.path).kind, "unknown")

    def test_invalid_contents_fail_closed(self):
        bad = [
            {"holidays": {"2026-10-31": "a Saturday"}},
            {"holidays": {"2028-01-17": "outside covered_years"}},
            {"holidays": {"2026-13-01": "no such date"}},
            {"early_closes": {"2026-11-26": "also a holiday"}},
            {"covered_years": []},
            {"early_close": "17:00"},
            {"unscheduled_early_closes": {"2026-10-27": {"close": "nope"}}},
        ]
        for change in bad:
            with self.subTest(change=change):
                path = self.write(**change)
                with self.assertRaises(mc.CalendarError):
                    mc.load(path)
                self.assertEqual(mc.session(d("2026-10-27"), path).kind, "unknown")
        self.raw.pop("holidays")
        self.assertEqual(mc.session(d("2026-10-27"), self.write()).kind, "unknown")

    def test_unscheduled_closure_and_early_close(self):
        path = self.write(unscheduled_closures={"2026-10-28": "National Day of Mourning"},
                          unscheduled_early_closes={"2026-10-29": {"close": "14:00",
                                                                   "reason": "test"}})
        self.assertEqual(mc.session(d("2026-10-28"), path).kind, "holiday")
        day = mc.session(d("2026-10-29"), path)
        self.assertEqual((day.kind, day.close), ("early_close", dt.time(14, 0)))
        self.assertEqual(mc.shift(dt.time(15, 51), day), dt.time(13, 51))

    def test_coverage_warning_30_days_before_the_end(self):
        path = self.write()
        self.assertIsNone(mc.coverage_warning(d("2027-11-30"), path))
        self.assertIn("2027-12-31", mc.coverage_warning(d("2027-12-01"), path))
        self.assertIn("2027-12-31", mc.coverage_warning(d("2028-01-03"), path))
        self.path.write_text("{trunc", encoding="utf-8")
        self.assertIn("unusable", mc.coverage_warning(d("2026-10-27"), path))


class IbkrHoursTest(CalendarFileMixin, unittest.TestCase):
    HOURS = ("20261126:CLOSED;20261127:0930-20261127:1300;"
             "20261130:0930-20261130:1600;20261201:0930-20261201:1600")

    def test_parse_liquid_hours(self):
        self.assertIsNone(mc.parse_liquid_hours(self.HOURS, d("2026-11-26")))
        self.assertEqual(mc.parse_liquid_hours(self.HOURS, d("2026-11-27")),
                         (dt.time(9, 30), dt.time(13, 0)))
        self.assertEqual(mc.parse_liquid_hours(self.HOURS, d("2026-12-02")), "missing")
        self.assertEqual(mc.parse_liquid_hours("", d("2026-12-02")), "missing")

    def test_agreement_is_no_alert(self):
        for text in ("2026-11-26", "2026-11-27", "2026-11-30"):
            day = mc.session(d(text))
            self.assertIsNone(mc.compare(day, mc.parse_liquid_hours(self.HOURS, d(text))))

    def test_ibkr_closure_wins(self):
        day = d("2026-10-28")
        self.assertIn("CLOSED", mc.compare(mc.session(day), None))
        mc.apply_ibkr_hours(day, None)
        self.assertEqual(mc.session(day).kind, "holiday")

    def test_ibkr_earlier_close_wins(self):
        day = d("2026-10-28")
        hours = (dt.time(9, 30), dt.time(14, 0))
        self.assertIn("early close", mc.compare(mc.session(day), hours))
        mc.apply_ibkr_hours(day, hours)
        self.assertEqual((mc.session(day).kind, mc.session(day).close),
                         ("early_close", dt.time(14, 0)))

    def test_ibkr_open_on_a_calendar_holiday_stays_closed(self):
        day = d("2026-11-26")
        text = mc.compare(mc.session(day), (dt.time(9, 30), dt.time(16, 0)))
        self.assertIn("staying closed", text)
        mc.apply_ibkr_hours(day, (dt.time(9, 30), dt.time(16, 0)))
        self.assertEqual(mc.session(day).kind, "holiday")

    def test_ibkr_later_close_never_extends_the_session(self):
        day = d("2026-11-27")
        mc.apply_ibkr_hours(day, (dt.time(9, 30), dt.time(16, 0)))
        self.assertEqual(mc.session(day).close, dt.time(13, 0))

    def test_cross_check_never_raises(self):
        class BrokenIB:
            def run(self, coro):
                return asyncio.run(coro)

            def reqContractDetailsAsync(self, contract):
                async def gone():
                    raise ConnectionError("gateway gone")
                return gone()

        with self.assertLogs("paperbot", "WARNING"):
            self.assertIsNone(mc.cross_check_with_ibkr(BrokenIB(), at("2026-10-28", 9, 0)))
        self.assertEqual(mc._ibkr, {})

    def test_cross_check_applies_ibkr_hours(self):
        detail = NS(liquidHours="20261028:CLOSED;20261029:0930-20261029:1600",
                    timeZoneId="US/Eastern")

        class FakeIB:
            def run(self, coro):
                return asyncio.run(coro)

            def reqContractDetailsAsync(self, contract):
                async def nothing():
                    return [detail]
                return nothing()

        text = mc.cross_check_with_ibkr(FakeIB(), at("2026-10-28", 9, 0))
        self.assertIn("CLOSED", text)
        self.assertEqual(mc.session(d("2026-10-28")).kind, "holiday")


class IbkrHoursHardeningTest(CalendarFileMixin, unittest.TestCase):
    """Review fixes: malformed IBKR hours never raise or loosen; a later open is applied."""
    DAY = d("2026-10-28")

    def test_malformed_hours_are_reported_not_raised(self):
        for text in ("20261028:9x30-20261028:1600", "20261028:2599-20261028:1600",
                     "20261028:0961-20261028:1600", "20261028:0930-20261028",
                     "20261028:1600-20261028:0930", "20261028:0930-20261027:1600",
                     "20261028:0930", "20261028:closed", "20261028"):
            with self.subTest(text=text):
                self.assertEqual(mc.parse_liquid_hours(text, self.DAY), "malformed")
        self.assertEqual(mc.parse_liquid_hours(None, self.DAY), "missing")
        self.assertEqual(mc.parse_liquid_hours("20261029:0930-20261029:1600", self.DAY), "missing")

    def test_malformed_hours_change_nothing(self):
        self.assertIsNone(mc.compare(mc.session(self.DAY), "malformed"))
        mc.apply_ibkr_hours(self.DAY, "malformed")
        self.assertEqual(mc._ibkr, {})
        self.assertEqual(mc.session(self.DAY).kind, "regular")

    def fake_ib(self, hours):
        detail = NS(liquidHours=hours, timeZoneId="US/Eastern")

        class FakeIB:
            def run(self, coro):
                return asyncio.run(coro)

            def reqContractDetailsAsync(self, contract):
                async def answer():
                    return [detail]
                return answer()
        return FakeIB()

    def test_cross_check_with_malformed_hours_keeps_the_calendar(self):
        for text in ("20261028:2599-20261028:1600", "20261028:9x30-20261028:1600"):
            with self.subTest(text=text), self.assertLogs("paperbot", "WARNING") as logs:
                self.assertIsNone(mc.cross_check_with_ibkr(self.fake_ib(text),
                                                           at("2026-10-28", 8, 0)))
            self.assertTrue(any("malformed" in line for line in logs.output))
            self.assertEqual(mc._ibkr, {})
            self.assertEqual(mc.session(self.DAY).describe(), "2026-10-28 regular 09:30-16:00 ET")

    def test_later_open_delays_the_session(self):
        hours = (dt.time(11, 0), dt.time(16, 0))
        self.assertIn("late open at 11:00", mc.compare(mc.session(self.DAY), hours))
        mc.apply_ibkr_hours(self.DAY, hours)
        day = mc.session(self.DAY)
        self.assertEqual((day.kind, day.open, day.close), ("regular", dt.time(11, 0), dt.time(16, 0)))
        self.assertFalse(day.is_open(at("2026-10-28", 10, 59, 59)))
        self.assertTrue(day.is_open(at("2026-10-28", 11, 0)))
        self.assertEqual(mc.shift(dt.time(15, 51), day), dt.time(15, 51))  # rules unchanged

    def test_later_open_and_earlier_close_both_apply(self):
        mc.apply_ibkr_hours(self.DAY, (dt.time(10, 0), dt.time(14, 0)))
        day = mc.session(self.DAY)
        self.assertEqual((day.kind, day.open, day.close),
                         ("early_close", dt.time(10, 0), dt.time(14, 0)))

    def test_earlier_ibkr_open_never_extends_the_session(self):
        mc.apply_ibkr_hours(self.DAY, (dt.time(4, 0), dt.time(16, 0)))
        self.assertEqual(mc.session(self.DAY).open, dt.time(9, 30))

    def test_ibkr_hours_after_the_early_close_leave_no_session(self):
        day = d("2026-11-27")
        hours = (dt.time(13, 30), dt.time(16, 0))
        self.assertIn("treating today as closed", mc.compare(mc.session(day), hours))
        mc.apply_ibkr_hours(day, hours)
        self.assertEqual(mc.session(day).kind, "holiday")

    def test_valid_closure_still_wins_through_the_cross_check(self):
        text = mc.cross_check_with_ibkr(self.fake_ib("20261028:CLOSED"), at("2026-10-28", 8, 0))
        self.assertIn("CLOSED", text)
        self.assertEqual(mc.session(self.DAY).kind, "holiday")


class WeekdaySessionTest(unittest.TestCase):
    """The backtester's sessions: every weekday is regular, as before the calendar."""

    def test_holidays_are_regular_for_the_backtester(self):
        self.assertEqual(mc.weekday_session(at("2026-11-26", 10, 0)).kind, "regular")
        self.assertEqual(mc.weekday_session(at("2025-06-02", 10, 0)).close, dt.time(16, 0))
        self.assertEqual(mc.weekday_session(at("2026-10-31", 10, 0)).kind, "weekend")


if __name__ == "__main__":
    unittest.main()
