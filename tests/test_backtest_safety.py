"""Backtesting can never reach live order code or change production rules.

Run: python -m unittest discover -s tests -p test_backtest_safety.py -v
"""
import os

os.environ["PAPERBOT_NOTIFY"] = "off"

import subprocess
import sys
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
