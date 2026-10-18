"""Offline tests for cycle.py (no Gateway, no Task Scheduler).

Run: python -m unittest discover -s tests -p test_cycle.py -v
"""
import os

os.environ["PAPERBOT_NOTIFY"] = "off"  # tests must never send real notifications

import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from paperbot.runtime import cycle

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
            at(19, 16, 2): "run",  # after-close alerts and stop restores
            at(19, 16, 5): "closed",
            at(19, 21, 0): "closed",
        }
        for when, expected in cases.items():
            self.assertEqual(cycle.time_gate(when, FC), expected, when)

    def test_force_close_time_comes_from_rules(self):
        self.assertEqual(cycle.force_close_at(), FC)


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
        with mock.patch("paperbot.trading.bot.connect", side_effect=[SystemExit(1), None]) as connect, \
                mock.patch.object(cycle.time, "sleep") as sleep:
            cycle.connect_with_retry(ib, {"client_id": 21}, mock.Mock())
        self.assertEqual(connect.call_count, 2)
        sleep.assert_called_once_with(5)

    def test_second_failure_exits(self):
        with mock.patch("paperbot.trading.bot.connect", side_effect=SystemExit(1)) as connect, \
                mock.patch.object(cycle.time, "sleep"):
            with self.assertRaises(SystemExit):
                cycle.connect_with_retry(mock.Mock(), {"client_id": 21}, mock.Mock())
        self.assertEqual(connect.call_count, 2)


class RunGuardTest(unittest.TestCase):
    def setUp(self):
        self.patch = mock.patch("paperbot.trading.bot.setup_logging")  # keep test lines out of logs/bot.log
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
