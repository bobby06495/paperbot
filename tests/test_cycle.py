"""Offline tests for cycle.py (no Gateway, no Task Scheduler).

Run: python -m unittest discover -s tests -p test_cycle.py -v
"""
import os

os.environ["PAPERBOT_NOTIFY"] = "off"  # tests must never send real notifications

import datetime as dt
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

from paperbot.core import scheduled_pass as cycle

ET = cycle.ET
FC = dt.time(15, 51)


def at(day: int, hour: int, minute: int) -> dt.datetime:
    return dt.datetime(2026, 10, day, hour, minute, tzinfo=ET)  # 19 = Monday


class TimeGateTest(unittest.TestCase):
    def test_states(self):
        cases = {
            at(17, 11, 0): "weekend",  # Saturday
            at(18, 11, 0): "weekend",  # Sunday
            at(19, 9, 29): "closed",
            at(19, 9, 30): "run",
            at(19, 10, 4): "run",  # strategy.decide manages exits only until 10:05
            at(19, 15, 50): "run",
            at(19, 15, 51): "force_close",
            at(19, 15, 59): "force_close",
            at(19, 16, 2): "post_close",  # confirm DAY orders dead, then restore stops
            at(19, 16, 5): "post_close",  # second protect-only pass
            at(19, 16, 10): "closed",
            at(19, 21, 0): "closed",
        }
        for when, expected in cases.items():
            self.assertEqual(cycle.time_gate(when, FC), expected, when)

    def test_force_close_time_comes_from_rules(self):
        self.assertEqual(cycle.force_close_at(), FC)


class EtBoundaryTest(unittest.TestCase):
    """Exact-second ET boundaries, other clock zones and the end of daylight saving."""

    def test_second_boundaries(self):
        def t(hour, minute, second):
            return at(19, hour, minute).replace(second=second)
        cases = [(t(9, 29, 59), "closed"), (t(9, 30, 0), "run"),
                 (t(15, 50, 59), "run"), (t(15, 51, 0), "force_close"),
                 (t(15, 59, 59), "force_close"), (t(16, 0, 0), "post_close"),
                 (t(16, 9, 59), "post_close"), (t(16, 10, 0), "closed")]
        for when, expected in cases:
            self.assertEqual(cycle.time_gate(when, FC), expected, when)

    def test_overnight_strategy_never_force_closes(self):
        self.assertEqual(cycle.time_gate(at(19, 15, 55), None), "run")
        self.assertEqual(cycle.seconds_to_force_close(at(19, 15, 50), None), 0.0)

    def test_friday_night_to_saturday(self):
        friday = dt.datetime(2026, 10, 23, 23, 59, 59, tzinfo=ET)
        self.assertEqual(cycle.time_gate(friday, FC), "closed")
        self.assertEqual(cycle.time_gate(friday + dt.timedelta(seconds=1), FC), "weekend")

    def test_weekday_is_the_et_weekday_not_the_pc_zone(self):
        from paperbot.core import engine
        singapore = dt.timezone(dt.timedelta(hours=8))
        # Saturday 04:02 in Singapore is Friday 16:02 ET: post-close still runs
        saturday_sgt = dt.datetime(2026, 10, 24, 4, 2, tzinfo=singapore)
        self.assertTrue(engine.post_close(saturday_sgt))
        self.assertEqual(cycle.time_gate(saturday_sgt.astimezone(ET), FC), "post_close")
        # Monday 04:02 in Singapore is Sunday 16:02 ET: nothing runs
        self.assertFalse(engine.post_close(dt.datetime(2026, 10, 26, 4, 2, tzinfo=singapore)))

    def test_end_of_daylight_saving(self):
        # US clocks fall back on Sunday 2026-11-01: 20:51 UTC is 16:51 ET before, 15:51 ET after
        before = dt.datetime(2026, 10, 30, 20, 51, tzinfo=dt.timezone.utc).astimezone(ET)
        after = dt.datetime(2026, 11, 2, 20, 51, tzinfo=dt.timezone.utc).astimezone(ET)
        self.assertEqual(cycle.time_gate(before, FC), "closed")
        self.assertEqual(cycle.time_gate(after, FC), "force_close")
        self.assertEqual(after.time(), dt.time(15, 51))

    def test_hand_over_to_force_close(self):
        def t(minute, second=0):
            return at(19, 15, minute).replace(second=second)
        self.assertEqual(cycle.seconds_to_force_close(t(45), FC), 0.0)  # the 15:50 run takes it
        self.assertEqual(cycle.seconds_to_force_close(t(50, 30), FC), 30.0)
        self.assertEqual(cycle.seconds_to_force_close(t(51), FC), 0.0)  # already force close


class MainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [mock.patch.object(cycle, "ERROR_LOG", Path(self.tmp.name) / "err.log"),
                        mock.patch("paperbot.runtime.runstate.HEARTBEAT_FILE", Path(self.tmp.name) / "hb.json")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def main_at(self, when, run=None):
        fake_dt = mock.Mock(wraps=dt.datetime)
        fake_dt.now = lambda tz=None: when
        with mock.patch.object(cycle.dt, "datetime", fake_dt), \
                mock.patch.object(cycle, "run", run or mock.Mock(return_value=0)) as run_mock:
            return cycle.main([]), run_mock

    def test_off_hours_exit_without_running(self):
        for when in (at(17, 11, 0), at(19, 8, 0), at(19, 20, 0)):
            code, run = self.main_at(when)
            self.assertEqual(code, 0)
            run.assert_not_called()

    def test_market_hours_run_with_gate(self):
        code, run = self.main_at(at(19, 15, 55))
        run.assert_called_once_with("force_close", False)

    def test_uncaught_error_logged_exit_1(self):
        code, _ = self.main_at(at(19, 11, 0), mock.Mock(side_effect=RuntimeError("boom")))
        self.assertEqual(code, 1)
        text = cycle.ERROR_LOG.read_text()
        self.assertIn("RuntimeError: boom", text)
        self.assertIn("Traceback", text)
        from paperbot.runtime import runstate
        self.assertIn('"crashed"', runstate.HEARTBEAT_FILE.read_text())  # dashboard shows it

    def test_deliberate_exit_logged(self):
        code, _ = self.main_at(at(19, 11, 0), mock.Mock(side_effect=SystemExit("not paper")))
        self.assertEqual(code, 1)
        self.assertIn("exit: not paper", cycle.ERROR_LOG.read_text())

    def test_print_task_installs_nothing(self):
        with mock.patch("builtins.print") as printed:
            self.assertEqual(cycle.main(["--print-task"]), 0)
        text = " ".join(str(c.args[0]) for c in printed.call_args_list if c.args)
        self.assertIn("Register-ScheduledTask -TaskName 'paperbot cycle'", text)
        self.assertIn("schtasks /Create", text)
        self.assertIn("-m paperbot.runtime.cycle", text)
        self.assertIn("--submit", text)

    def test_powershell_command_has_no_native_quote_escapes(self):
        # Windows PowerShell 5.1 strips \" when calling native programs (the original bug)
        self.assertNotIn('\\"', cycle.powershell_task_command())
        self.assertIn("-MultipleInstances IgnoreNew", cycle.powershell_task_command())

    def test_cmd_command_quotes_only_paths_with_spaces(self):
        with mock.patch.object(cycle.sys, "executable", r"C:\Program Files\Py\python.exe"):
            self.assertIn(r'\"C:\Program Files\Py\python.exe\"', cycle.cmd_task_command())
        self.assertNotIn('\\"', cycle.cmd_task_command())  # this PC: no spaces, no escapes


class RetryTest(unittest.TestCase):
    def test_one_retry_after_5_seconds(self):
        ib = mock.Mock()
        with mock.patch("paperbot.core.engine.connect", side_effect=[SystemExit(1), None]) as connect, \
                mock.patch.object(cycle.time, "sleep") as sleep:
            cycle.connect_with_retry(ib, {"client_id": 21}, mock.Mock())
        self.assertEqual(connect.call_count, 2)
        sleep.assert_called_once_with(5)

    def test_second_failure_exits(self):
        with mock.patch("paperbot.core.engine.connect", side_effect=SystemExit(1)) as connect, \
                mock.patch.object(cycle.time, "sleep"):
            with self.assertRaises(SystemExit):
                cycle.connect_with_retry(mock.Mock(), {"client_id": 21}, mock.Mock())
        self.assertEqual(connect.call_count, 2)


class RunGuardTest(unittest.TestCase):
    def setUp(self):
        self.patch = mock.patch("paperbot.core.engine.setup_logging")  # keep test lines out of logs/bot.log
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def test_skips_when_bot_running(self):
        with mock.patch("paperbot.runtime.runstate.stop_requested", return_value=False), \
                mock.patch("paperbot.runtime.runstate.running_pid", return_value=1234), \
                mock.patch("ib_async.IB") as ib_cls:
            self.assertEqual(cycle.run("run", True), 0)
        ib_cls.assert_not_called()

    def test_skips_when_stop_file(self):
        with mock.patch("paperbot.runtime.runstate.stop_requested", return_value=True), \
                mock.patch("ib_async.IB") as ib_cls:
            self.assertEqual(cycle.run("run", True), 0)
        ib_cls.assert_not_called()


class ForceCloseTimingTest(unittest.TestCase):
    def test_last_run_before_force_close_waits_for_it(self):
        self.assertEqual(cycle.seconds_to_force_close(at(19, 15, 50).replace(second=3), FC), 57)
        self.assertEqual(cycle.seconds_to_force_close(at(19, 15, 47), FC), 240)
        self.assertEqual(cycle.seconds_to_force_close(at(19, 15, 45), FC), 0)  # 15:50 run follows
        self.assertEqual(cycle.seconds_to_force_close(at(19, 15, 51), FC), 0)
        self.assertEqual(cycle.seconds_to_force_close(at(19, 15, 55), FC), 0)

    def test_sleep_until_stops_on_stop_file(self):
        ib = mock.Mock()
        with mock.patch("paperbot.runtime.runstate.stop_requested", side_effect=[False, True]):
            self.assertFalse(cycle.sleep_until(ib, cycle.time.monotonic() + 60))
        ib.sleep.assert_called_once_with(5.0)


class RunFlowTest(unittest.TestCase):
    """cycle.run() with every IBKR / file dependency mocked: only the control flow is real."""

    def run_at(self, when, holds=True, gate=None, confirmed=True):
        from paperbot.strategies import rule_strategy as strategy
        fake_dt = mock.Mock(wraps=dt.datetime)
        fake_dt.now = lambda tz=None: when
        ib = mock.MagicMock()
        ib.reqPnL.return_value = NS(dailyPnL=0.0)
        patches = [
            mock.patch.object(cycle.dt, "datetime", fake_dt),
            mock.patch("paperbot.core.engine.setup_logging"),
            mock.patch("paperbot.runtime.runstate.stop_requested", return_value=False),
            mock.patch("paperbot.runtime.runstate.running_pid", return_value=None),
            mock.patch("paperbot.runtime.runstate.claim_pid"),
            mock.patch("paperbot.runtime.runstate.release_pid"),
            mock.patch("paperbot.core.risk.RiskGuard"),
            mock.patch("paperbot.core.engine.preflight"),  # tested in test_runtime
            mock.patch("paperbot.core.broker.verify_account", return_value="DU1"),
            mock.patch("paperbot.strategies.rule_strategy.load_state", return_value={"positions": {}}),
            mock.patch("paperbot.core.selector.unattended_key", return_value="trend_join_long"),
            mock.patch.object(strategy, "_profile", None),
            mock.patch.object(strategy, "SCAN_BUDGET_SECONDS", strategy.SCAN_BUDGET_SECONDS),
            mock.patch("ib_async.IB", return_value=ib),
            mock.patch.object(cycle, "connect_with_retry"),
            mock.patch.object(cycle, "save_orphans"),
            mock.patch.object(cycle, "load_orphans", return_value={}),
            mock.patch.object(cycle, "holds_bot_positions", side_effect=[holds, False]),
        ]
        self.one_pass = mock.Mock(return_value=1)
        self.sleep_until = mock.Mock(return_value=True)
        self.await_done = mock.Mock(return_value=confirmed)
        patches += [mock.patch.object(cycle, "one_pass", self.one_pass),
                    mock.patch.object(cycle, "sleep_until", self.sleep_until),
                    mock.patch("paperbot.core.engine.await_day_orders_done", self.await_done)]
        for p in patches:
            p.start()
        try:
            return cycle.run(gate or cycle.time_gate(when, FC), True)
        finally:
            for p in reversed(patches):
                p.stop()

    def test_1550_run_holding_positions_starts_force_close_at_1551(self):
        self.run_at(at(19, 15, 50).replace(second=4))
        deadline = self.sleep_until.call_args.args[1] - cycle.time.monotonic()
        self.assertAlmostEqual(deadline, 56, delta=1)
        self.assertEqual(self.one_pass.call_count, 2)  # normal pass + first force-close pass

    def test_1550_run_when_flat_does_not_wait(self):
        self.run_at(at(19, 15, 50).replace(second=4), holds=False)
        self.sleep_until.assert_not_called()
        self.assertEqual(self.one_pass.call_count, 1)

    def test_post_close_run_confirms_day_orders_before_the_pass(self):
        self.run_at(at(19, 16, 0).replace(second=3))
        self.await_done.assert_called_once()
        self.assertTrue(self.await_done.call_args.args[1])  # submit
        self.one_pass.assert_called_once()

    def test_force_close_run_still_holding_at_the_close_protects_itself(self):
        # this run holds the pid file, so the 16:00 run is skipped
        self.run_at(at(19, 16, 0).replace(second=5), gate="force_close")
        self.await_done.assert_called_once()
        self.assertEqual(self.one_pass.call_count, 2)

    def test_regular_run_skips_post_close_check(self):
        self.run_at(at(19, 11, 0))
        self.await_done.assert_not_called()
        self.sleep_until.assert_not_called()

    def test_post_close_pass_runs_even_if_day_orders_are_not_confirmed(self):
        # the reconcile then still counts the unconfirmed order and puts no stop beside it
        self.run_at(at(19, 16, 0).replace(second=3), confirmed=False)
        self.await_done.assert_called_once()
        self.one_pass.assert_called_once()

    def test_force_close_run_ends_once_flat(self):
        self.run_at(at(19, 15, 52), holds=False, gate="force_close")
        self.one_pass.assert_called_once()  # no 30 s re-pricing loop, no post-close pass
        self.await_done.assert_not_called()


class OrphanStateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = mock.patch.object(cycle, "CYCLE_STATE", Path(self.tmp.name) / "c.json")
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_round_trip_between_runs(self):
        cycle.save_orphans({"AAPL": 1}, at(19, 11, 0))
        self.assertEqual(cycle.load_orphans(at(19, 11, 5)), {"AAPL": 1})

    def test_stale_or_corrupt_state_is_ignored(self):
        cycle.save_orphans({"AAPL": 1}, at(19, 11, 0))
        self.assertEqual(cycle.load_orphans(at(19, 12, 0)), {})
        cycle.CYCLE_STATE.write_text("{trunc")
        self.assertEqual(cycle.load_orphans(at(19, 11, 5)), {})


if __name__ == "__main__":
    unittest.main()
