"""Offline tests: the market calendar at every trading boundary.

decide() (protect-only on holidays / unknown days, force close at the early close),
the scheduled cycle's gate, the built-in scheduler's prefilter slots, the
post-close window and the early-close strategy times. No IBKR.
Run: python -m unittest discover -s tests -p test_session_guards.py -v
"""
import os

os.environ["PAPERBOT_NOTIFY"] = "off"  # tests must never send real notifications

import datetime as dt
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

from paperbot import paths
from paperbot.core import clock
from paperbot.core import engine
from paperbot.core import profile as profiles
from paperbot.core import scheduled_pass as cycle
from paperbot.core.risk import REF_STOP, OrderIntent
from paperbot.market import market_calendar as mc
from paperbot.strategies import rule_strategy as strategy

from test_strategy import FakeIB, StrategyTestBase, trade

ET = mc.ET
FC = dt.time(15, 51)
LIMITS = {"limits": {"allowed_symbols": ["*"], "limit_orders_only": True}}


def at(day: str, hour: int, minute: int, second: int = 0) -> dt.datetime:
    return dt.datetime.combine(dt.date.fromisoformat(day), dt.time(hour, minute, second), ET)


def sells(reqs) -> list:
    return [r for r in reqs if isinstance(r, OrderIntent) and r.action == "SELL"]


class DecideSessionTest(StrategyTestBase):
    """Holding AAPL with its stop at IBKR, at the force-close time of different days."""

    def setUp(self):
        super().setUp()
        mc._ibkr.clear()
        self.addCleanup(mc._ibkr.clear)

    def held(self, now):
        self.own()
        return FakeIB(9, [trade(REF_STOP, 5, 9, 95.0, oca="g1")], 101.0, now,
                      quote=(100.50, 100.60))

    def test_regular_day_force_closes_at_1551(self):
        now = at("2026-10-19", 15, 51, 2)
        self.assertEqual(len(sells(self.decide(self.held(now), now))), 1)

    def test_holiday_sends_no_orders(self):
        now = at("2026-11-26", 15, 51, 2)  # Thanksgiving
        self.assertEqual(self.decide(self.held(now), now), [])

    def test_early_close_force_closes_at_1251(self):
        now = at("2026-11-27", 12, 51, 2)
        sell, = sells(self.decide(self.held(now), now))
        self.assertEqual((sell.quantity, sell.oca_group), (9, "g1"))
        before = at("2026-11-27", 12, 50, 0)
        self.assertEqual(sells(self.decide(self.held(before), before)), [])

    def test_nothing_after_the_early_close(self):
        now = at("2026-11-27", 15, 51, 2)  # the market closed at 13:00
        self.assertEqual(sells(self.decide(self.held(now), now)), [])

    def test_unknown_day_is_protect_only_with_alert(self):
        now = at("2028-01-03", 15, 51, 2)  # outside covered_years
        with self.assertLogs("paperbot", "ERROR") as logs:
            self.assertEqual(self.decide(self.held(now), now), [])
        self.assertTrue(any("ALERT market calendar" in line for line in logs.output))

    def test_ibkr_reported_closure_is_protect_only(self):
        now = at("2026-10-28", 15, 51, 2)
        mc.apply_ibkr_hours(now.date(), None)
        self.assertEqual(self.decide(self.held(now), now), [])

    def test_ibkr_late_open_delays_trading(self):
        """Before IBKR's later open the pass is protect-only; from it, normal."""
        mc.apply_ibkr_hours(dt.date(2026, 10, 28), (dt.time(11, 0), dt.time(16, 0)))
        before = at("2026-10-28", 10, 30)
        with mock.patch.object(strategy, "_find_entries", side_effect=AssertionError("no scan")):
            self.assertEqual(self.decide(self.held(before), before), [])
        after = at("2026-10-28", 15, 51, 2)
        self.assertEqual(len(sells(self.decide(self.held(after), after))), 1)

    def test_no_entries_after_the_shifted_entry_window(self):
        base = strategy.load_profile()
        profile = profiles.for_session(base, mc.session(dt.date(2026, 11, 27)))
        latest = profiles.hhmm(base.time_filter.latest_entry_et)  # e.g. 15:15 -> 12:15
        last = at("2026-11-27", latest.hour - 3, latest.minute)
        self.assertTrue(profile.time_filter.in_entry_window(last))
        self.assertFalse(profile.time_filter.in_entry_window(last + dt.timedelta(minutes=1)))
        self.assertEqual(profile.time_filter.entry_cutoff(at("2026-11-27", 10, 0)),
                         last + dt.timedelta(minutes=1))  # unfilled entries die a minute later

    def test_check_only_reports_a_closed_market(self):
        now = at("2026-11-26", 11, 0)
        with mock.patch.object(strategy, "_now", lambda: now):
            result = strategy.check_symbol(FakeIB(0, [], 101.0, now), "DU1", "MSFT")
        self.assertFalse(result["pass"])
        self.assertIn("market closed", result["reasons"][-1])


class ProfileForSessionTest(unittest.TestCase):
    """Early-close times; nothing else in the profile changes."""

    def setUp(self):
        self.profiles = profiles.load_all(config=LIMITS)
        self.early = mc.session(dt.date(2026, 11, 27))

    def test_trend_join_long(self):
        base = self.profiles["trend_join_long"]
        day = profiles.for_session(base, self.early)
        tf = day.time_filter
        self.assertEqual((tf.earliest_entry_et, tf.latest_entry_et, tf.force_close_et),
                         (base.time_filter.earliest_entry_et,
                          profiles.for_session(base, self.early).time_filter.latest_entry_et,
                          "12:51"))
        self.assertEqual(tf.latest_entry_et,
                         mc.shift(profiles.hhmm(base.time_filter.latest_entry_et), self.early)
                         .strftime("%H:%M"))
        for field in ("entry", "stop", "manage", "limits", "universe", "timeframe", "holding",
                      "order_ref", "key"):
            self.assertEqual(getattr(day, field), getattr(base, field), field)

    def test_macro_swing_etf(self):
        base = self.profiles["macro_swing_etf"]
        day = profiles.for_session(base, self.early)
        self.assertEqual((day.time_filter.earliest_entry_et, day.time_filter.latest_entry_et),
                         ("12:45", "12:55"))
        self.assertEqual([b.at_et for b in day.exit], ["12:50", "12:50"])
        self.assertEqual([b.at_et for b in base.exit], ["15:50", "15:50"])  # original untouched
        self.assertEqual([b.type for b in day.exit], [b.type for b in base.exit])
        self.assertEqual(day.limits, base.limits)

    def test_regular_day_returns_the_same_profile(self):
        base = self.profiles["trend_join_long"]
        self.assertIs(profiles.for_session(base, mc.session(dt.date(2026, 10, 27))), base)


class CycleGateTest(unittest.TestCase):
    def setUp(self):
        mc._ibkr.clear()

    def test_holiday_and_weekend(self):
        self.assertEqual(cycle.time_gate(at("2026-11-26", 11, 0), FC), "holiday")
        self.assertEqual(cycle.time_gate(at("2026-12-25", 15, 51), FC), "holiday")
        self.assertEqual(cycle.time_gate(at("2026-11-28", 11, 0), FC), "weekend")

    def test_early_close_windows(self):
        self.assertEqual(cycle.time_gate(at("2026-11-27", 12, 50, 59), FC), "run")
        self.assertEqual(cycle.time_gate(at("2026-11-27", 12, 51), FC), "force_close")
        self.assertEqual(cycle.time_gate(at("2026-11-27", 13, 0), FC), "post_close")
        self.assertEqual(cycle.time_gate(at("2026-11-27", 13, 9, 59), FC), "post_close")
        self.assertEqual(cycle.time_gate(at("2026-11-27", 13, 10), FC), "closed")
        self.assertEqual(cycle.time_gate(at("2026-11-27", 15, 51), FC), "closed")

    def test_overnight_strategy_on_an_early_close(self):
        self.assertEqual(cycle.time_gate(at("2026-11-27", 12, 55), None), "run")
        self.assertEqual(cycle.time_gate(at("2026-11-27", 13, 5), None), "post_close")

    def test_regular_day_unchanged(self):
        self.assertEqual(cycle.time_gate(at("2026-10-27", 9, 29, 59), FC), "closed")
        self.assertEqual(cycle.time_gate(at("2026-10-27", 9, 30), FC), "run")
        self.assertEqual(cycle.time_gate(at("2026-10-27", 15, 51), FC), "force_close")
        self.assertEqual(cycle.time_gate(at("2026-10-27", 16, 0), FC), "post_close")
        self.assertEqual(cycle.time_gate(at("2026-10-27", 16, 10), FC), "closed")

    def test_unknown_day_inside_the_usual_window_only(self):
        self.assertEqual(cycle.time_gate(at("2028-01-03", 11, 0), FC), "unknown")
        self.assertEqual(cycle.time_gate(at("2028-01-03", 8, 0), FC), "closed")

    def test_ibkr_late_open_gates_the_cycle(self):
        mc.apply_ibkr_hours(dt.date(2026, 10, 28), (dt.time(11, 0), dt.time(16, 0)))
        try:
            self.assertEqual(cycle.time_gate(at("2026-10-28", 10, 30), FC), "closed")
            self.assertEqual(cycle.time_gate(at("2026-10-28", 11, 0), FC), "run")
        finally:
            mc._ibkr.clear()

    def test_singapore_clock(self):
        from zoneinfo import ZoneInfo
        sg = dt.datetime(2026, 11, 27, 0, 30, tzinfo=ZoneInfo("Asia/Singapore"))
        self.assertEqual(cycle.time_gate(sg, FC), "holiday")  # 26 Nov 11:30 ET

    def test_holiday_run_exits_without_running(self):
        fake = mock.Mock(wraps=dt.datetime)
        fake.now = lambda tz=None: at("2026-11-26", 11, 0)
        with mock.patch.object(cycle.dt, "datetime", fake), \
                mock.patch.object(cycle, "run") as run:
            self.assertEqual(cycle.main(["--submit"]), 0)
        run.assert_not_called()

    def test_force_close_and_session_end_follow_the_early_close(self):
        with mock.patch.object(cycle, "force_close_at", return_value=FC):
            self.assertEqual(cycle.force_close_today(at("2026-11-27", 9, 30)), dt.time(12, 51))
            self.assertEqual(cycle.force_close_today(at("2026-10-27", 9, 30)), FC)
        self.assertEqual(cycle.session_end(at("2026-11-27", 9, 30)), dt.time(13, 0))
        self.assertEqual(cycle.session_end(at("2026-10-27", 9, 30)), dt.time(16, 0))


class PostCloseTest(unittest.TestCase):
    def test_post_close_window_follows_the_close(self):
        self.assertTrue(engine.post_close(at("2026-10-27", 16, 0)))
        self.assertFalse(engine.post_close(at("2026-10-27", 16, 10)))
        self.assertTrue(engine.post_close(at("2026-11-27", 13, 0)))
        self.assertTrue(engine.post_close(at("2026-11-27", 13, 9, 59)))
        self.assertFalse(engine.post_close(at("2026-11-27", 16, 0)))
        self.assertFalse(engine.post_close(at("2026-11-26", 16, 0)))  # holiday


class ClockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        profile = NS(universe={"source": "watchlist"},
                     time_filter=NS(latest_entry_et="15:15"))
        self.jobs = clock.default_jobs(profile, rotate=lambda: None)
        self.clock = clock.Clock(self.jobs, Path(self.tmp.name) / "s.json",
                                 popen=mock.Mock(return_value=NS(pid=1, poll=lambda: None)))

    def due(self, now) -> list[str]:
        return [job.name for job, _ in self.clock.due(now)]

    def prefilter_due(self, now) -> bool:
        return "prefilter" in self.due(now)

    def test_prefilter_runs_on_a_trading_day(self):
        self.assertTrue(self.prefilter_due(at("2026-10-27", 9, 56)))

    def test_no_prefilter_on_a_holiday_but_maintenance_runs(self):
        self.assertFalse(self.prefilter_due(at("2026-11-26", 9, 56)))
        self.assertIn("rotate_logs", self.due(at("2026-11-26", 9, 26)))
        self.assertIn("daily_summary", self.due(at("2026-11-26", 16, 6)))

    def test_no_prefilter_on_an_unknown_day(self):
        self.assertFalse(self.prefilter_due(at("2028-01-03", 9, 56)))

    def test_early_close_drops_slots_after_the_last_entry(self):
        self.assertTrue(self.prefilter_due(at("2026-11-27", 11, 56)))
        # last entry 15:15 -> 12:15 on the early close: the 12:25 slot is not run
        self.assertFalse(self.prefilter_due(at("2026-11-27", 12, 26)))
        # ... while the 11:55 slot is still within its 25-minute grace it may run late
        self.assertTrue(self.prefilter_due(at("2026-11-27", 12, 10)))
        # the same slots run on a regular day
        self.assertTrue(self.prefilter_due(at("2026-10-27", 12, 26)))


class ScheduleTest(unittest.TestCase):
    def test_cycle_window_is_weekdays_0930_to_1610_et(self):
        from paperbot.ops import setup_schedule as setup
        commands = {name: cmd for name, cmd, _ in setup.build_commands(
            dt.date(2026, 10, 27), convert=lambda m: m.astimezone(ET))}
        cmd = commands["HT_Cycle"]
        self.assertEqual(cmd[cmd.index("/ST") + 1], "09:30")
        self.assertEqual(cmd[cmd.index("/D") + 1], "MON,TUE,WED,THU,FRI")
        self.assertEqual((cmd[cmd.index("/RI") + 1], cmd[cmd.index("/DU") + 1]), ("5", "06:40"))


class BacktestUnaffectedTest(unittest.TestCase):
    def test_backtester_pins_weekday_sessions(self):
        """Backtest results must not change with the live calendar."""
        source = (paths.PROJECT_ROOT / "src/paperbot/backtesting/engine.py").read_text()
        self.assertIn('mock.patch.object(rule_strategy, "session_for", '
                      'market_calendar.weekday_session)', source)
        self.assertIs(strategy.session_for, mc.session_at)


if __name__ == "__main__":
    unittest.main()
