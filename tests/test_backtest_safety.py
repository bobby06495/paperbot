"""Backtesting can never reach live order code or change production rules.

Run: python -m unittest discover -s tests -p test_backtest_safety.py -v
"""
import os

os.environ["PAPERBOT_NOTIFY"] = "off"

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

from paperbot.backtesting.data import DataError, IBKRProvider
from paperbot.backtesting.engine import SimIB
from paperbot.core import engine
from paperbot.core.risk import OrderIntent, StopUpdate
from paperbot.runtime import runstate


class IsolationTest(unittest.TestCase):
    def test_backtesting_never_imports_the_order_sending_module(self):
        """The simulator drives the strategy engine but never imports core/engine.py (where
        orders are placed) or the live runner."""
        code = ("import sys, paperbot.backtesting.cli, paperbot.backtesting.runner, "
                "paperbot.backtesting.report; "
                "print([m for m in ('paperbot.core.engine', 'paperbot.core.scheduled_pass', 'paperbot.cli') "
                "if m in sys.modules])")
        with tempfile.TemporaryDirectory() as elsewhere:
            out = subprocess.run([sys.executable, "-c", code], cwd=elsewhere, capture_output=True,
                                 text=True, timeout=60)
        self.assertEqual(out.stdout.strip(), "[]", out.stderr)

    def test_order_functions_refuse_in_a_backtest_process(self):
        ib = mock.MagicMock()
        with mock.patch.dict(os.environ, {runstate.BACKTEST_ENV: "1"}):
            with self.assertRaises(runstate.LiveTradingBlocked):
                engine.handle_intent(ib, mock.MagicMock(), {}, "DU1", OrderIntent("X", "BUY", 1, 10.0), 0.0, True)
            with self.assertRaises(runstate.LiveTradingBlocked):
                engine.handle_stop(ib, mock.MagicMock(), {}, "DU1", StopUpdate("X", 1, 9.0), True)
            with self.assertRaises(runstate.LiveTradingBlocked):
                engine.handle_cancel(ib, NS(order_id=1, symbol="X", reason=""), True)
        ib.placeOrder.assert_not_called()
        ib.cancelOrder.assert_not_called()

    def test_live_runners_refuse_to_start(self):
        env = {**os.environ, runstate.BACKTEST_ENV: "1"}
        for module in ("paperbot.core.engine", "paperbot.core.scheduled_pass", "paperbot.cli"):
            out = subprocess.run([sys.executable, "-c", f"import sys, {module} as m; sys.exit(m.main([]))"],
                                 env=env, capture_output=True, text=True, timeout=60)
            self.assertNotEqual(out.returncode, 0, module)
            self.assertIn("backtest", (out.stdout + out.stderr).lower(), module)

    def test_cli_marks_its_process(self):
        from paperbot.backtesting import runner
        saved = {k: os.environ.get(k) for k in (runstate.BACKTEST_ENV, "PAPERBOT_NOTIFY")}
        try:
            os.environ.pop(runstate.BACKTEST_ENV, None)
            runner.mark_process()
            self.assertEqual(os.environ[runstate.BACKTEST_ENV], "1")
            self.assertEqual(os.environ["PAPERBOT_NOTIFY"], "off")
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_sim_broker_has_no_working_order_methods(self):
        ib = SimIB(sim=None)
        for name in ("placeOrder", "cancelOrder", "reqGlobalCancel"):
            with self.assertRaises(RuntimeError):
                getattr(ib, name)()

    def test_ibkr_provider_refuses_live_port_before_connecting(self):
        with mock.patch("ib_async.IB.connect") as connect:
            with self.assertRaises(DataError):
                IBKRProvider({"host": "127.0.0.1", "port": 4001, "client_id": 21},
                             {"client_id_offset": 3, "request_pause_seconds": 0, "timeout_seconds": 1})
        connect.assert_not_called()

    def test_ibkr_provider_is_read_only_and_blocks_orders(self):
        fake = mock.MagicMock()
        fake.managedAccounts.return_value = ["DU123"]
        with mock.patch("ib_async.IB", return_value=fake):
            p = IBKRProvider({"host": "127.0.0.1", "port": 4002, "client_id": 21},
                             {"client_id_offset": 3, "request_pause_seconds": 0, "timeout_seconds": 1})
        kwargs = fake.connect.call_args.kwargs
        self.assertTrue(kwargs["readonly"])
        self.assertEqual(kwargs["clientId"], 24)
        for name in ("placeOrder", "cancelOrder", "reqGlobalCancel"):
            with self.assertRaises(RuntimeError):
                getattr(p.ib, name)()

    def test_ibkr_provider_refuses_live_account(self):
        fake = mock.MagicMock()
        fake.managedAccounts.return_value = ["U123"]
        with mock.patch("ib_async.IB", return_value=fake):
            with self.assertRaises(DataError):
                IBKRProvider({"host": "127.0.0.1", "port": 4002, "client_id": 21},
                             {"client_id_offset": 3, "request_pause_seconds": 0, "timeout_seconds": 1})



RACE = """
import sys, time
from pathlib import Path
from paperbot.backtesting import runs
folder, start = Path(sys.argv[1]), float(sys.argv[2])
while time.time() < start:
    pass
try:
    with runs.RunLock(folder):
        (folder / f"got-{sys.argv[3]}").write_text("x")
        time.sleep(4)
except runs.RunLocked:
    sys.exit(3)
"""


class RunLockTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_second_holder_refused_until_released(self):
        from paperbot.backtesting import runs
        with runs.RunLock(self.dir):
            with self.assertRaises(runs.RunLocked) as caught:
                runs.RunLock(self.dir).__enter__()
            self.assertIn(str(os.getpid()), str(caught.exception))
        with runs.RunLock(self.dir):  # released on exit
            pass
        self.assertFalse((self.dir / ".backtest.owner.json").exists())

    def test_old_lock_file_with_a_live_pid_does_not_block(self):
        """The OS lock decides, not the file's contents (no stale-pid takeover race)."""
        from paperbot.backtesting import runs
        (self.dir / ".backtest.lock").write_text('{"pid": %d}' % os.getpid())
        with runs.RunLock(self.dir):
            pass

    def test_simultaneous_starts_admit_exactly_one(self):
        import time
        start = time.time() + 6.0
        procs = [subprocess.Popen([sys.executable, "-c", RACE, str(self.dir), str(start), str(i)])
                 for i in range(4)]
        codes = sorted(p.wait(timeout=60) for p in procs)
        self.assertEqual(codes, [0, 3, 3, 3])
        self.assertEqual(len(list(self.dir.glob("got-*"))), 1)

    def test_lock_released_when_the_owner_dies(self):
        from paperbot.backtesting import runs
        code = ("import sys, os; from pathlib import Path; from paperbot.backtesting import runs; "
                "lock = runs.RunLock(Path(sys.argv[1])); lock.__enter__(); os._exit(0)")
        subprocess.run([sys.executable, "-c", code, str(self.dir)], check=True, timeout=60)
        with runs.RunLock(self.dir):  # the dead owner's lock is gone with its process
            pass



class ConfigValidationTest(unittest.TestCase):
    """Malformed backtest settings are refused by name before they change an assumption."""

    def refused(self, overrides, text):
        from paperbot.backtesting import config as bt_config
        with self.assertRaises(bt_config.BacktestConfigError) as caught:
            bt_config.load(Path(tempfile.mkdtemp()) / "none.json", overrides)
        self.assertIn(text, str(caught.exception))

    def test_booleans_are_not_numbers(self):
        self.refused({"starting_capital": True}, "starting_capital")
        self.refused({"execution": {"participation_rate": True}}, "execution.participation_rate")
        self.refused({"execution": {"reset_drawdown_breaker_next_day": 1}}, "true or false")

    def test_wrong_types_and_non_finite_values(self):
        self.refused({"execution": {"half_spread_bps": "2"}}, "execution.half_spread_bps")
        self.refused({"starting_capital": float("inf")}, "finite")
        self.refused({"validation": {"walk_forward_folds": 2.5}}, "whole number")
        self.refused({"symbols": "AAPL"}, "symbols")
        self.refused({"execution": 5}, "execution must be an object")

    def test_ranges_and_providers(self):
        self.refused({"ibkr": {"client_id_offset": 0}}, "client_id_offset")
        self.refused({"fallback_provider": "synthetic"}, "fallback_provider")
        self.refused({"validation": {"bootstrap_samples": 0}}, "bootstrap_samples")

    def test_valid_settings_accepted(self):
        from paperbot.backtesting import config as bt_config
        cfg = bt_config.load(None, {"starting_capital": 50000, "benchmark_symbol": None,
                                    "fallback_provider": None})
        self.assertEqual(cfg["starting_capital"], 50000)

    def test_cli_refuses_without_a_traceback(self):
        import json
        bad = Path(tempfile.mkdtemp()) / "bt.json"
        bad.write_text(json.dumps({"execution": {"participation_rate": True}}))
        out = subprocess.run([sys.executable, "-m", "paperbot.backtesting.cli", "--config", str(bad),
                              "strategies"], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 2, out.stderr)
        self.assertIn("execution.participation_rate", out.stderr)
        self.assertNotIn("Traceback", out.stderr)


if __name__ == "__main__":
    unittest.main()
