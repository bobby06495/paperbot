"""Offline tests for runstate.py and the strategy's pure helpers.

Run: python -m unittest discover -s tests -p test_runstate.py -v
"""
import os

os.environ["PAPERBOT_NOTIFY"] = "off"  # tests must never send real notifications

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from paperbot.runtime import runstate
from paperbot.indicators import swing_low
from paperbot.strategies import rule_strategy as strategy


class StateFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_file_is_none(self):
        self.assertIsNone(runstate.read_json_state(self.path))

    def test_round_trip(self):
        runstate.write_json_atomic(self.path, {"a": 1})
        self.assertEqual(runstate.read_json_state(self.path), {"a": 1})

    def test_corrupt_or_wrong_type_fails_closed(self):
        for text in ("", "{trunc", "[1, 2]"):
            self.path.write_text(text)
            with self.assertRaises(runstate.StateCorrupt):
                runstate.read_json_state(self.path)

    def test_reset_moves_only_corrupt_files(self):
        good = Path(self.tmp.name) / "good.json"
        runstate.write_json_atomic(good, {"ok": True})
        self.path.write_text("{trunc")
        backups = runstate.reset_corrupt([self.path, good])
        self.assertEqual(len(backups), 1)
        self.assertFalse(self.path.exists())
        self.assertTrue(backups[0].exists() and good.exists())

    def test_strategy_state_fails_closed(self):
        self.path.write_text("{trunc")
        with mock.patch.object(strategy, "STATE", self.path):
            with self.assertRaises(runstate.StateCorrupt):
                strategy.load_state()


class PidFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = mock.patch.object(runstate, "PID_FILE", Path(self.tmp.name) / "bot.pid")
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_pid_alive(self):
        self.assertTrue(runstate.pid_alive(os.getpid()))
        self.assertFalse(runstate.pid_alive(2 ** 22 + 7))  # no such process

    def test_claim_and_release(self):
        self.assertIsNone(runstate.running_pid())
        runstate.claim_pid()
        self.assertEqual(runstate.running_pid(), os.getpid())
        runstate.release_pid()
        self.assertFalse(runstate.PID_FILE.exists())

    def test_stale_pid_file_is_not_running(self):
        runstate.PID_FILE.write_text(str(2 ** 22 + 7))
        self.assertIsNone(runstate.running_pid())
        runstate.claim_pid()  # a stale file does not block startup
        self.assertEqual(runstate.running_pid(), os.getpid())


class SwingLowTest(unittest.TestCase):
    def test_latest_pivot_low(self):
        lows = [10, 9, 8, 9, 10, 11, 10.5, 10.2, 10.6, 11]
        bars = [SimpleNamespace(low=x) for x in lows]
        self.assertEqual(swing_low(bars, 2, 2), 10.2)

    def test_no_pivot_yet(self):
        bars = [SimpleNamespace(low=x) for x in (10, 9, 8, 7)]
        self.assertIsNone(swing_low(bars, 2, 2))


if __name__ == "__main__":
    unittest.main()
