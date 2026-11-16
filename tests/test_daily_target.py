"""Offline tests for the $5,000 capital cap and the daily profit target (rules.json risk:
capital_usd, daily_profit_target_usd), and for daily P&L in USD.

Run: python -m unittest discover -s tests -p test_daily_target.py -v
"""
import os

os.environ["PAPERBOT_NOTIFY"] = "off"  # tests must never send real notifications

import datetime as dt
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

from paperbot.core import account
from paperbot.core import engine as bot
from paperbot.core.common import ET
from paperbot.core.risk import (AccountState, ConfigError, Limits, OrderIntent, RiskGuard,
                                capital_view, deployed_usd, load_limits)
from paperbot.runtime.runstate import StateCorrupt, write_json_atomic
from paperbot.strategies import rule_strategy as strategy

from test_strategy import REF_STOP, FakeIB, StrategyTestBase, trade

LIMITS = Limits(allowed_symbols=["*"], max_quantity_per_order=500, max_order_value=1250.0,
                max_orders_per_day=8, max_open_orders=3, max_daily_loss=50.0,
                max_risk_per_trade_pct=0.5, max_position_size_pct_of_portfolio=25.0,
                max_concurrent_positions=4, max_commission_pct_of_risk=10.0,
                max_drawdown_usd=500.0, capital_usd=5000.0, daily_profit_target_usd=100.0)
DAY = dt.date(2026, 11, 17)


class GuardTargetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.json"
        self.today = DAY

    def tearDown(self):
        self.tmp.cleanup()

    def guard(self, limits=LIMITS):
        return RiskGuard(limits, self.path, today=lambda: self.today)

    def test_target_latches_for_the_day_and_halts_entries(self):
        guard = self.guard()
        guard.update_daily_pnl(99.99)
        self.assertFalse(guard.target_hit)
        guard.update_daily_pnl(100.0)
        self.assertTrue(guard.target_hit)
        self.assertTrue(guard.halted)
        self.assertIn("daily profit target 100.00 reached", guard.halt_reason)
        guard.update_daily_pnl(40.0)  # a later dip does not undo it
        self.assertTrue(guard.target_hit)
        buy = OrderIntent("AAPL", "BUY", 1, 100.0, stop_price=97.0)
        self.assertTrue(any("trading halted" in p for p in guard.check(buy, 0, 40.0)))

    def test_exits_are_never_blocked_by_the_target(self):
        guard = self.guard()
        guard.update_daily_pnl(150.0)
        sell = OrderIntent("AAPL", "SELL", 5, 99.0, closing=True)
        self.assertEqual(guard.check(sell, 0, 150.0, AccountState(5000.0, {"AAPL": 5}, set())), [])

    def test_survives_a_restart_and_resets_on_the_next_et_day(self):
        self.guard().update_daily_pnl(120.0)
        self.assertTrue(self.guard().target_hit)  # new process, same day
        self.today = DAY + dt.timedelta(days=1)
        guard = self.guard()
        self.assertFalse(guard.target_hit)
        self.assertFalse(guard.halted)

    def test_no_target_configured_never_triggers(self):
        guard = self.guard(replace(LIMITS, daily_profit_target_usd=None))
        guard.update_daily_pnl(10_000.0)
        self.assertFalse(guard.target_hit)
        self.assertFalse(guard.halted)

    def test_unknown_pnl_never_triggers(self):
        guard = self.guard()
        guard.update_daily_pnl(None)
        self.assertFalse(guard.target_hit)

    def test_target_after_a_loss_halt_keeps_the_loss_reason(self):
        guard = self.guard()
        guard.update_daily_pnl(-60.0)
        guard.update_daily_pnl(110.0)
        self.assertTrue(guard.target_hit)
        self.assertIn("daily loss", guard.halt_reason)

    def test_state_file_from_before_the_target_still_loads(self):
        write_json_atomic(self.path, {"date": DAY.isoformat(), "orders_today": 2,
                                      "halted": False, "halt_reason": ""})
        guard = self.guard()
        self.assertFalse(guard.target_hit)
        self.assertEqual(guard.state["orders_today"], 2)

    def test_corrupt_target_flag_fails_closed(self):
        write_json_atomic(self.path, {"date": DAY.isoformat(), "orders_today": 0,
                                      "halted": False, "halt_reason": "", "target_hit": "yes"})
        with self.assertRaises(StateCorrupt):
            self.guard()


class ConfigTest(unittest.TestCase):
    RISK = {"max_quantity_per_order": 500, "max_order_value": 1250.0, "max_orders_per_day": 8,
            "max_open_orders": 3, "max_daily_loss": 50.0}
    CONFIG = {"limits": {"allowed_symbols": ["*"], "limit_orders_only": True}}

    def load(self, **risk):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rules.json"
            path.write_text(json.dumps({"strategies": {"s": {"risk": {**self.RISK, **risk}}}}))
            return load_limits(self.CONFIG, path, "s")

    def test_values_are_read_and_optional(self):
        limits = self.load(capital_usd=5000, daily_profit_target_usd=100)
        self.assertEqual((limits.capital_usd, limits.daily_profit_target_usd), (5000.0, 100.0))
        self.assertIsNone(self.load().capital_usd)
        self.assertIsNone(self.load(daily_profit_target_usd=None).daily_profit_target_usd)

    def test_invalid_values_refuse_to_start(self):
        for key in ("capital_usd", "daily_profit_target_usd"):
            for bad in (0, -100, "100", True):
                with self.subTest(key=key, value=bad), self.assertRaises(ConfigError):
                    self.load(**{key: bad})

    def test_live_rules_use_the_5000_account_and_100_target(self):
        from paperbot.core import profile as profiles
        for key in profiles.strategy_keys():
            with self.subTest(strategy=key):
                limits = profiles.load(key).limits
                self.assertEqual(limits.capital_usd, 5000.0)
                self.assertEqual(limits.daily_profit_target_usd, 100.0)


class CapitalCapTest(unittest.TestCase):
    def test_capital_view(self):
        self.assertEqual(capital_view(LIMITS, 781_120.0, 3e6, 0.0), (5000.0, 5000.0))
        self.assertEqual(capital_view(LIMITS, 781_120.0, 3e6, 4200.0), (5000.0, 800.0))
        self.assertEqual(capital_view(LIMITS, 4500.0, 3000.0, 0.0), (4500.0, 3000.0))  # smaller live
        self.assertEqual(capital_view(LIMITS, 781_120.0, 3e6, 6000.0)[1], 0.0)  # never negative
        self.assertEqual(capital_view(LIMITS, None, None, 0.0), (None, None))  # unknown stays unknown
        no_cap = replace(LIMITS, capital_usd=None)
        self.assertEqual(capital_view(no_cap, 781_120.0, 3e6, 4200.0), (781_120.0, 3e6))

    def test_deployed_counts_positions_and_working_buys(self):
        positions = [NS(contract=NS(secType="STK"), position=10.0, avgCost=100.5),
                     NS(contract=NS(secType="OPT"), position=1.0, avgCost=500.0),  # not stock
                     NS(contract=NS(secType="STK"), position=0.0, avgCost=50.0)]
        buys = [NS(order=NS(totalQuantity=8, lmtPrice=50.0), orderStatus=NS(remaining=5.0))]
        self.assertAlmostEqual(deployed_usd(positions, buys), 10 * 100.5 + 5 * 50.0)

    def test_unknown_cost_fails_closed(self):
        no_cost = [NS(contract=NS(secType="STK"), position=10.0, avgCost=float("nan"))]
        self.assertEqual(deployed_usd(no_cost, []), float("inf"))
        unset = [NS(order=NS(totalQuantity=8, lmtPrice=1.7976931348623157e308),
                    orderStatus=NS(remaining=8.0))]
        self.assertEqual(deployed_usd([], unset), float("inf"))
        self.assertEqual(capital_view(LIMITS, 1e6, 1e6, float("inf"))[1], 0.0)

    def test_guard_uses_the_capped_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = RiskGuard(LIMITS, Path(tmp) / "s.json")
            big = AccountState(781_120.0, {}, set(), buying_power=3e6)
            # $1,000, risk $24: fine for a $5,000 account
            ok = OrderIntent("AAPL", "BUY", 10, 100.0, stop_price=97.6)
            self.assertEqual(guard.check(ok, 0, 0.0, big), [])
            # risk $40 is 0.005% of $781k but 0.8% of the $5,000 the bot may use
            risky = OrderIntent("AAPL", "BUY", 10, 100.0, stop_price=96.0)
            self.assertIn("trade risk 40.00 > 0.5% of portfolio (25.00)",
                          guard.check(risky, 0, 0.0, big))
            busy = AccountState(781_120.0, {}, set(), buying_power=3e6, deployed_usd=4500.0)
            self.assertIn("order value 1000.00 > capital left 500.00",
                          guard.check(ok, 0, 0.0, busy))


class DecideTargetTest(StrategyTestBase):
    NOW = dt.datetime(2026, 10, 19, 11, 0, tzinfo=ET)

    def decide(self, ib, now, target_hit=False):
        with mock.patch.object(strategy, "_now", lambda: now):
            return strategy.decide(ib, "DU1", target_hit=target_hit)

    def test_intraday_positions_are_flattened_when_the_target_is_hit(self):
        self.own()
        with mock.patch.object(strategy, "_find_entries") as scan:
            reqs = self.decide(FakeIB(9, [trade(REF_STOP, 5, 9, 95.0)], 101.0, self.NOW),
                               self.NOW, target_hit=True)
        scan.assert_not_called()
        sells = [r for r in reqs if isinstance(r, OrderIntent)]
        self.assertEqual(len(sells), 1)
        self.assertEqual((sells[0].action, sells[0].quantity, sells[0].closing), ("SELL", 9, True))
        self.assertTrue(sells[0].reason.startswith("daily target (attempt 1)"))

    def test_without_the_target_11_00_is_a_normal_pass(self):
        self.own()
        reqs = self.decide(FakeIB(9, [trade(REF_STOP, 5, 9, 95.0)], 101.0, self.NOW), self.NOW)
        self.assertFalse(any(isinstance(r, OrderIntent) for r in reqs))

    def test_multi_day_positions_are_kept_and_no_entries_are_scanned(self):
        self.own()
        with mock.patch.object(strategy, "ACTIVE", "macro_swing_etf"), \
                mock.patch.object(strategy, "_profile", None), \
                mock.patch.object(strategy, "_find_entries") as scan:
            late = self.NOW.replace(hour=15, minute=47)  # inside its 15:45-15:55 entry window
            reqs = self.decide(FakeIB(9, [trade("paperbot:macro_swing_etf:stop", 5, 9, 95.0)],
                                      101.0, late), late, target_hit=True)
        scan.assert_not_called()
        self.assertFalse(any(isinstance(r, OrderIntent) for r in reqs))

    def test_entries_are_sized_for_the_capped_account(self):
        # FakeIB reports $1,000,000; the risk section caps the bot at $5,000
        ib = FakeIB(0, [], 101.0, self.NOW)
        signal = {"pass": True, "price": 101.0, "stop": 95.0, "gap": 3.0, "rvol": 2.0}
        with mock.patch.object(strategy, "_universe", lambda p, today: ["MSFT"]), \
                mock.patch.object(strategy, "evaluate", return_value=signal), \
                self.assertLogs("paperbot", "INFO"):
            reqs = self.decide(ib, self.NOW)
        buy = next(r for r in reqs if isinstance(r, OrderIntent))
        # risk 0.5% of $5,000 = $25 at ~$6.22 a share -> 4 shares (not 500 on $1M)
        self.assertEqual(buy.quantity, 4)

    def entry(self, ib, stop):
        signal = {"pass": True, "price": 20.0, "stop": stop, "gap": 3.0, "rvol": 2.0}
        with mock.patch.object(strategy, "_universe", lambda p, today: ["MSFT"]), \
                mock.patch.object(strategy, "evaluate", return_value=signal), \
                self.assertLogs("paperbot", "INFO"):
            reqs = self.decide(ib, self.NOW)
        return next((r for r in reqs if isinstance(r, OrderIntent) and r.action == "BUY"), None)

    def test_capital_already_deployed_limits_the_next_entry(self):
        # held: 48 AAPL at $95 = $4,560, so $440 of the $5,000 is left. A $20.05 limit with a
        # $19.00 stop: risk allows 23 shares, the capital left only 21 (R $22, fees 9% of R)
        ib = FakeIB(48, [], 20.0, self.NOW)
        ib._positions[0].avgCost = 95.0
        self.assertEqual(self.entry(ib, 19.0).quantity, 21)
        strategy._scan_bar = None
        ib._positions[0].avgCost = 100.0  # $200 left: 9 shares, R $9.45 -> fee rule skips it
        self.assertIsNone(self.entry(ib, 19.0))


class EngineTargetTest(unittest.TestCase):
    def test_new_target_is_announced_once_and_cancels_working_orders(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = RiskGuard(LIMITS, Path(tmp) / "s.json")
            ib = mock.Mock()
            with mock.patch.object(bot, "cancel_working") as cancel, \
                    mock.patch.object(bot, "notify") as push, \
                    self.assertLogs("paperbot", "WARNING"):
                bot.update_guard(ib, guard, 101.5)
                bot.update_guard(ib, guard, 120.0)
            cancel.assert_called_once()
            push.assert_called_once()
            self.assertEqual(push.call_args.args[0], "Daily Target Reached")

    def test_day_done_needs_the_target_and_no_owned_position(self):
        guard = NS(target_hit=True)
        with mock.patch.object(strategy, "load_state", return_value={"positions": {}}):
            self.assertTrue(bot.day_done(guard))
            self.assertFalse(bot.day_done(NS(target_hit=False)))
            self.assertFalse(bot.day_done(mock.MagicMock()))  # only a real True counts
        with mock.patch.object(strategy, "load_state", return_value={"positions": {"AAPL": {}}}):
            self.assertFalse(bot.day_done(guard))


@unittest.skipUnless(os.name == "nt", "Windows power API")
class IdleTimerTest(unittest.TestCase):
    """Scheduled runs keep Windows from idling out during the session (2026-11-11: the PC
    hibernated at 14:34 ET after 3 h without input while runs kept firing)."""

    def calls(self, action):
        import ctypes
        from paperbot.system import procs
        with mock.patch.object(ctypes.windll.kernel32, "SetThreadExecutionState") as api:
            action(procs)
        return [c.args[0] for c in api.call_args_list]

    def test_reset_restarts_the_idle_timer_without_holding_it(self):
        self.assertEqual(self.calls(lambda procs: procs.reset_idle_timer()), [0x00000001])

    def test_keep_awake_holds_while_running_then_counts_idle_from_the_end(self):
        def run(procs):
            with procs.keep_awake():
                pass
        self.assertEqual(self.calls(run), [0x80000001, 0x80000000, 0x00000001])


class DailyPnlUsdTest(unittest.TestCase):
    """reqPnL is in the account's base currency; the guard compares it with USD limits."""

    def setUp(self):
        self.patches = [mock.patch.object(account, "_last_daily", None),
                        mock.patch.object(account, "fallback_mode", return_value="auto")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    @staticmethod
    def ib(values):
        return mock.Mock(accountValues=lambda acct=None: list(values))

    def test_usd_account_unchanged(self):
        ib = self.ib([NS(tag="NetLiquidation", currency="USD", value="5000")])
        self.assertEqual(account.daily_pnl(ib, "U1", NS(dailyPnL=128.0)), 128.0)

    def test_sgd_account_converted_with_the_ibkr_rate(self):
        ib = self.ib([NS(tag="NetLiquidation", currency="SGD", value="6400"),
                      NS(tag="ExchangeRate", currency="USD", value="1.28")])
        self.assertEqual(account.daily_pnl(ib, "U1", NS(dailyPnL=128.0)), 100.0)
        self.assertIn("SGD", account.last_daily_pnl()["source"])

    def test_no_rate_fails_closed(self):
        ib = self.ib([NS(tag="NetLiquidation", currency="SGD", value="6400")])
        with mock.patch.object(account, "_rate", return_value=(None, "no rate")):
            self.assertIsNone(account.daily_pnl(ib, "U1", NS(dailyPnL=128.0)))

    def test_unreadable_account_values_keep_ibkrs_figure(self):
        ib = mock.Mock()
        ib.accountValues.side_effect = RuntimeError("not connected")
        self.assertEqual(account.daily_pnl(ib, "U1", NS(dailyPnL=-20.0)), -20.0)


if __name__ == "__main__":
    unittest.main()
