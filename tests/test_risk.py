"""Offline tests for the risk limits (no Gateway connection needed).

Run: python -m unittest discover -s tests -p test_risk.py -v
"""
import os

os.environ["PAPERBOT_NOTIFY"] = "off"  # tests must never send real notifications

import datetime as dt
import json
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path

from paperbot.trading.risk import (AccountState, Limits, OrderIntent, RiskGuard, StopUpdate, load_limits,
                  today_et, verify_paper)
from paperbot.runtime.runstate import StateCorrupt

LIMITS = Limits(allowed_symbols=["NVDA"], max_quantity_per_order=1,
                max_order_value=500.0, max_orders_per_day=3, max_open_orders=2,
                max_daily_loss=1000.0, limit_orders_only=True)
OK = OrderIntent("NVDA", "BUY", 1, 100.0, stop_price=99.0)


class RiskGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.day = dt.date(2026, 10, 19)
        self.guard = self.make_guard()

    def tearDown(self):
        self.tmp.cleanup()

    def make_guard(self):
        return RiskGuard(LIMITS, Path(self.tmp.name) / "state.json", today=lambda: self.day)

    def check(self, intent=OK, open_orders=0, pnl=0.0):
        return self.guard.check(intent, open_orders, pnl)

    def test_valid_order_passes(self):
        self.assertEqual(self.check(), [])

    def test_symbol_not_allowed(self):
        self.assertTrue(self.check(OrderIntent("TSLA", "BUY", 1, 100.0)))

    def test_quantity_limit(self):
        self.assertTrue(self.check(OrderIntent("NVDA", "BUY", 2, 100.0)))
        self.assertTrue(self.check(OrderIntent("NVDA", "BUY", 0, 100.0)))

    def test_order_value_limit(self):
        self.assertTrue(self.check(OrderIntent("NVDA", "BUY", 1, 501.0)))

    def test_market_order_blocked(self):
        self.assertTrue(self.check(OrderIntent("NVDA", "BUY", 1, None)))

    def test_bad_action_and_price(self):
        self.assertTrue(self.check(OrderIntent("NVDA", "HOLD", 1, 100.0)))
        self.assertTrue(self.check(OrderIntent("NVDA", "BUY", 1, -5.0)))

    def test_open_orders_limit(self):
        self.assertTrue(self.check(open_orders=2))

    def test_daily_order_count_persists_and_resets(self):
        for _ in range(3):
            self.guard.record_order()
        self.assertTrue(self.make_guard().check(OK, 0, 0.0))  # survives restart
        self.day = dt.date(2026, 10, 20)
        self.assertEqual(self.check(), [])  # new day resets

    def test_daily_loss_halts_for_the_day(self):
        self.assertTrue(self.check(pnl=-1000.0))
        self.assertTrue(self.check(pnl=50.0))  # stays halted after recovering
        self.assertTrue(self.make_guard().halted)  # survives restart
        self.day = dt.date(2026, 10, 20)
        self.assertEqual(self.check(), [])

    def test_unknown_pnl_blocks(self):
        self.assertTrue(self.check(pnl=None))

    def test_wildcard_allows_any_symbol(self):
        guard = RiskGuard(replace(LIMITS, allowed_symbols=["*"]),
                          Path(self.tmp.name) / "s.json", today=lambda: self.day)
        self.assertEqual(guard.check(replace(OK, symbol="TSLA"), 0, 0.0), [])

    def test_opening_sell_blocked(self):
        self.assertTrue(self.check(OrderIntent("NVDA", "SELL", 1, 100.0)))

    def test_buy_requires_protective_stop(self):
        self.assertTrue(self.check(replace(OK, stop_price=None)))
        self.assertTrue(self.check(replace(OK, stop_price=100.0)))  # not below the limit
        self.assertTrue(self.check(replace(OK, stop_price=0.0)))

    def test_corrupt_state_fails_closed(self):
        path = Path(self.tmp.name) / "state.json"
        self.guard.record_order()
        path.write_text('{"date": "2026-10-19", "orders_to')  # truncated write
        with self.assertRaises(StateCorrupt):
            self.make_guard()
        path.write_text('{"date": "2026-10-19"}')  # missing keys
        with self.assertRaises(StateCorrupt):
            self.make_guard()

    def test_state_saved_atomically(self):
        self.guard.record_order()
        files = sorted(p.name for p in Path(self.tmp.name).iterdir())
        self.assertEqual(files, ["state.json"])  # no leftover temp file
        self.assertEqual(self.make_guard().state["orders_today"], 1)


EXIT = OrderIntent("NVDA", "SELL", 3, 100.0, closing=True)
HOLDING = AccountState(100_000.0, {"NVDA": 3.0})


class ClosingOrderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.guard = RiskGuard(LIMITS, Path(self.tmp.name) / "state.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_exit_ignores_entry_limits_and_halt(self):
        for _ in range(3):
            self.guard.record_order()
        self.guard.halt("test")
        # 3 shares > max 1, 3 orders sent today, 2 open orders, P&L unknown: still allowed
        self.assertEqual(self.guard.check(EXIT, 2, None, HOLDING), [])

    def test_exit_cannot_exceed_position(self):
        self.assertTrue(self.guard.check(EXIT, 0, 0.0, AccountState(100_000.0, {"NVDA": 2.0})))
        self.assertTrue(self.guard.check(EXIT, 0, 0.0, None))

    def test_exit_must_be_valid_sell(self):
        self.assertTrue(self.guard.check(replace(EXIT, action="BUY"), 0, 0.0, HOLDING))
        self.assertTrue(self.guard.check(replace(EXIT, limit_price=None), 0, 0.0, HOLDING))

    def test_stop_update_allowed_while_halted(self):
        self.guard.halt("test")
        self.assertEqual(self.guard.check_stop(StopUpdate("NVDA", 3, 95.0), HOLDING), [])

    def test_stop_update_never_exceeds_position(self):
        self.assertTrue(self.guard.check_stop(StopUpdate("NVDA", 4, 95.0), HOLDING))
        self.assertTrue(self.guard.check_stop(StopUpdate("NVDA", 1, 95.0), None))

    def test_stop_update_must_be_valid(self):
        self.assertTrue(self.guard.check_stop(StopUpdate("NVDA", 0, 95.0), HOLDING))
        self.assertTrue(self.guard.check_stop(StopUpdate("NVDA", 1, 0.0), HOLDING))


RULES_LIMITS = replace(LIMITS, allowed_symbols=["*"], max_quantity_per_order=1000,
                       max_order_value=1e9, max_risk_per_trade_pct=1.0,
                       max_position_size_pct_of_portfolio=10,
                       max_concurrent_positions=5)
ACCOUNT = AccountState(100_000.0)  # 1% risk = 1,000; 10% size = 10,000


class RulesRiskTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.guard = RiskGuard(RULES_LIMITS, Path(self.tmp.name) / "state.json")

    def tearDown(self):
        self.tmp.cleanup()

    def check(self, intent, account=ACCOUNT):
        return self.guard.check(intent, 0, 0.0, account)

    def test_within_limits_passes(self):
        # 100 x 100.00 = 10,000 value; risk 100 x 10.00 = 1,000
        self.assertEqual(self.check(OrderIntent("AAPL", "BUY", 100, 100.0, stop_price=90.0)), [])

    def test_position_size_limit(self):
        self.assertTrue(self.check(OrderIntent("AAPL", "BUY", 101, 100.0, stop_price=99.0)))

    def test_risk_per_trade_limit(self):
        self.assertTrue(self.check(OrderIntent("AAPL", "BUY", 50, 100.0, stop_price=79.0)))

    def test_stop_required_below_price(self):
        self.assertTrue(self.check(OrderIntent("AAPL", "BUY", 1, 100.0)))
        self.assertTrue(self.check(OrderIntent("AAPL", "BUY", 1, 100.0, stop_price=100.0)))

    def test_concurrent_positions_limit(self):
        full = AccountState(100_000.0, {s: 1.0 for s in "ABCD"}, {"E"})
        buy = OrderIntent("F", "BUY", 1, 100.0, stop_price=99.0)
        self.assertTrue(self.check(buy, full))
        # adding to a symbol already held does not open a new position
        self.assertEqual(self.check(replace(buy, symbol="A"), full), [])

    def test_unknown_account_value_blocks(self):
        intent = OrderIntent("AAPL", "BUY", 1, 100.0, stop_price=99.0)
        self.assertTrue(self.check(intent, None))
        self.assertTrue(self.check(intent, AccountState(None)))

    def test_load_limits_merges_rules_json(self):
        rules = Path(self.tmp.name) / "rules.json"
        rules.write_text(json.dumps({"risk": {"max_risk_per_trade_pct": 1.0,
                                              "max_position_size_pct_of_portfolio": 10,
                                              "max_concurrent_positions": 5}}))
        limits = load_limits({"limits": asdict(LIMITS)}, rules)
        self.assertEqual(limits.max_concurrent_positions, 5)
        self.assertEqual(limits.allowed_symbols, ["NVDA"])
        self.assertIsNone(load_limits({"limits": asdict(LIMITS)},
                                      rules.with_name("none.json")).max_risk_per_trade_pct)


class TradingDateTest(unittest.TestCase):
    """Daily counters roll over at ET midnight, not the PC's local midnight."""
    SGT = dt.timezone(dt.timedelta(hours=8))

    def test_singapore_midnight_is_still_same_et_day(self):
        # 00:30 Tue in Singapore = 12:30 Mon in New York (mid-session)
        self.assertEqual(today_et(dt.datetime(2026, 10, 20, 0, 30, tzinfo=self.SGT)),
                         dt.date(2026, 10, 19))

    def test_rolls_over_at_et_midnight(self):
        # 12:30 Tue in Singapore = 00:30 Tue in New York
        self.assertEqual(today_et(dt.datetime(2026, 10, 20, 12, 30, tzinfo=self.SGT)),
                         dt.date(2026, 10, 20))

    def test_guard_uses_et_date_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            guard = RiskGuard(LIMITS, Path(tmp) / "state.json")
            self.assertEqual(guard.state["date"], today_et().isoformat())


class VerifyPaperTest(unittest.TestCase):
    def test_accepts_paper(self):
        self.assertEqual(verify_paper(4002, ["DU0000000"]), "DU0000000")

    def test_rejects_live_port(self):
        with self.assertRaises(SystemExit):
            verify_paper(4001, ["DU0000000"])

    def test_rejects_live_account(self):
        with self.assertRaises(SystemExit):
            verify_paper(4002, ["U0000000"])

    def test_rejects_multiple_accounts(self):
        with self.assertRaises(SystemExit):
            verify_paper(4002, ["DU1", "DU2"])


if __name__ == "__main__":
    unittest.main()
