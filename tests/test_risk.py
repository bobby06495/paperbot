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

from paperbot.core.risk import (AccountState, ConfigError, DrawdownBreaker, Limits, OrderIntent,
                                   RiskGuard, StopUpdate, commission_drag, load_limits,
                                   require_limits, round_trip_commission, today_et, verify_paper)
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
ACCOUNT = AccountState(100_000.0, buying_power=400_000.0)  # 1% risk = 1,000; 10% size = 10,000


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
        full = AccountState(100_000.0, {s: 1.0 for s in "ABCD"}, {"E"}, 400_000.0)
        buy = OrderIntent("F", "BUY", 1, 100.0, stop_price=99.0)
        self.assertTrue(self.check(buy, full))
        # adding to a symbol already held does not open a new position
        self.assertEqual(self.check(replace(buy, symbol="A"), full), [])

    def test_unknown_account_value_blocks(self):
        intent = OrderIntent("AAPL", "BUY", 1, 100.0, stop_price=99.0)
        self.assertTrue(self.check(intent, None))
        self.assertTrue(self.check(intent, AccountState(None)))



CONFIG_RAILS = {"limits": {"allowed_symbols": ["NVDA"], "limit_orders_only": True}}
RULES_RISK = {"max_quantity_per_order": 1, "max_order_value": 500.0, "max_orders_per_day": 3,
              "max_open_orders": 2, "max_daily_loss": 1000.0, "max_risk_per_trade_pct": 1.0,
              "max_position_size_pct_of_portfolio": 10, "max_concurrent_positions": 5,
              "max_commission_pct_of_risk": None}


class LoadLimitsTest(unittest.TestCase):
    """Safety rails from config.json, every threshold from rules.json risk; fail closed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rules = Path(self.tmp.name) / "rules.json"

    def tearDown(self):
        self.tmp.cleanup()

    def load(self, risk=None, config=None):
        self.rules.write_text(json.dumps(
            {"strategies": {"s": {"risk": RULES_RISK if risk is None else risk}}}))
        return load_limits(CONFIG_RAILS if config is None else config, self.rules, "s")

    def fails(self, text, **kwargs):
        with self.assertRaises(ConfigError) as caught:
            self.load(**kwargs)
        self.assertIn(text, str(caught.exception))

    def test_reads_each_key_from_its_file(self):
        limits = self.load()
        self.assertEqual(limits, replace(LIMITS, max_risk_per_trade_pct=1.0,
                                         max_position_size_pct_of_portfolio=10.0,
                                         max_concurrent_positions=5))
        self.assertIsNone(limits.max_commission_pct_of_risk)  # null = not enforced

    def test_types_are_cast(self):
        limits = self.load(risk={**RULES_RISK, "max_quantity_per_order": 2.0,
                                 "max_daily_loss": 1000})
        self.assertIs(type(limits.max_quantity_per_order), int)
        self.assertIs(type(limits.max_daily_loss), float)
        self.assertIs(type(limits.max_position_size_pct_of_portfolio), float)

    def test_limit_orders_only_defaults_to_true(self):
        self.assertTrue(self.load(config={"limits": {"allowed_symbols": ["*"]}}).limit_orders_only)

    def test_threshold_left_in_config_json_refuses(self):
        stale = {"limits": {**CONFIG_RAILS["limits"], "max_quantity_per_order": 1}}
        self.fails("still contains ['max_quantity_per_order']", config=stale)

    def test_missing_threshold_refuses(self):
        risk = {k: v for k, v in RULES_RISK.items() if k != "max_daily_loss"}
        self.fails("missing ['max_daily_loss']", risk=risk)

    def test_missing_rules_file_or_section_refuses(self):
        with self.assertRaises(ConfigError):
            load_limits(CONFIG_RAILS, self.rules.with_name("none.json"), "s")
        self.rules.write_text(json.dumps({"strategies": {"s": {"exit": {}}}}))
        with self.assertRaises(ConfigError):
            load_limits(CONFIG_RAILS, self.rules, "s")
        with self.assertRaises(ConfigError):  # unknown strategy
            load_limits(CONFIG_RAILS, self.rules, "other")

    def test_bad_values_refuse(self):
        for key, bad in (("max_quantity_per_order", "1"), ("max_quantity_per_order", 1.5),
                         ("max_quantity_per_order", True), ("max_order_value", 0),
                         ("max_daily_loss", -1000.0), ("max_drawdown_usd", "3000")):
            with self.subTest(key=key, value=bad), self.assertRaises(ConfigError):
                self.load(risk={**RULES_RISK, key: bad})
        for rails in ({"allowed_symbols": []}, {"allowed_symbols": "NVDA"},
                      {"allowed_symbols": ["*"], "limit_orders_only": "yes"}):
            with self.subTest(rails=rails), self.assertRaises(ConfigError):
                self.load(config={"limits": rails})

    def test_unknown_keys_refuse(self):
        self.fails("unknown keys ['max_shares']", risk={**RULES_RISK, "max_shares": 1})
        self.fails("unknown keys ['typo']", config={"limits": {"allowed_symbols": ["*"], "typo": 1}})

    def test_require_limits_alerts_and_exits(self):
        self.rules.write_text(json.dumps({"strategies": {"s": {"risk": {}}}}))
        with self.assertLogs("paperbot", "ERROR") as logs, self.assertRaises(SystemExit) as caught:
            require_limits(CONFIG_RAILS, self.rules, "s")
        self.assertIn("Refusing to start", str(caught.exception))
        self.assertIn("ALERT refusing to start", logs.output[0])

    def test_live_config_files_load(self):
        from paperbot import paths
        limits = load_limits(json.loads(paths.CONFIG_FILE.read_text()), paths.RULES_FILE,
                             "trend_join_long")
        # Types only here: the exact approved values of every strategy are locked in
        # test_risk_policy.APPROVED_LIMITS, and checked against the portfolio model there.
        for name, kind in (("max_quantity_per_order", int), ("max_order_value", float),
                           ("max_orders_per_day", int), ("max_open_orders", int),
                           ("max_daily_loss", float), ("allowed_symbols", list),
                           ("limit_orders_only", bool)):
            self.assertIs(type(getattr(limits, name)), kind, name)


class CostsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def guard(self, **limits):
        return RiskGuard(replace(RULES_LIMITS, **limits), Path(self.tmp.name) / "state.json")

    def test_round_trip_commission_fixed_pricing(self):
        self.assertEqual(round_trip_commission(1, 189.58), 2.00)  # $1 minimum per side
        self.assertEqual(round_trip_commission(1000, 50.0), 10.00)  # $0.005/share
        self.assertAlmostEqual(round_trip_commission(1, 20.0), 0.40)  # capped at 1% of value

    def test_commission_drag_threshold(self):
        # 1 share, R = 6.03: $2 commission is 33% of R
        self.assertIn("commission", commission_drag(1, 189.58, 183.55, 1.0))
        self.assertIsNone(commission_drag(1, 189.58, 183.55, None))
        # 500 shares, R = 3,015: $5 commission is 0.17% of R
        self.assertIsNone(commission_drag(500, 189.58, 183.55, 1.0))

    def test_guard_blocks_commission_drag(self):
        guard = self.guard(max_commission_pct_of_risk=1.0)
        one = OrderIntent("AAPL", "BUY", 1, 100.0, stop_price=94.0)
        self.assertTrue(any("commission" in p for p in guard.check(one, 0, 0.0, ACCOUNT)))
        big = OrderIntent("AAPL", "BUY", 100, 100.0, stop_price=90.0)
        self.assertEqual(guard.check(big, 0, 0.0, ACCOUNT), [])

    def test_buying_power_rejects_and_unknown_blocks(self):
        guard = self.guard()
        buy = OrderIntent("AAPL", "BUY", 100, 100.0, stop_price=90.0)  # value 10,000
        self.assertEqual(guard.check(buy, 0, 0.0, replace(ACCOUNT, buying_power=10_000.0)), [])
        self.assertTrue(guard.check(buy, 0, 0.0, replace(ACCOUNT, buying_power=9_999.0)))
        self.assertIn("buying power unavailable, cannot check margin",
                      guard.check(buy, 0, 0.0, replace(ACCOUNT, buying_power=None)))

    def test_share_cap_does_not_override_value_risk_or_size_limits(self):
        guard = self.guard(max_quantity_per_order=35, max_order_value=2500.0)
        small = AccountState(20_000.0, buying_power=1e9)  # 1% risk = 200, 10% size = 2,000
        # every order is within the 35-share cap; each breaks exactly one other limit
        for intent, limit in ((OrderIntent("NVDA", "BUY", 35, 189.58, stop_price=183.55),
                               "order value"),
                              (OrderIntent("AAPL", "BUY", 19, 100.0, stop_price=80.0),
                               "trade risk"),
                              (OrderIntent("AAPL", "BUY", 21, 100.0, stop_price=99.0),
                               "position value")):
            with self.subTest(limit=limit):
                problems = guard.check(intent, 0, 0.0, small)
                self.assertTrue(any(p.startswith(limit) for p in problems), problems)
                self.assertFalse(any("quantity" in p for p in problems))

    def test_expired_gtd_entry_blocked(self):
        past = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
        buy = OrderIntent("AAPL", "BUY", 100, 100.0, stop_price=90.0, good_till=past)
        self.assertTrue(any("entry window closed" in p for p in self.guard().check(buy, 0, 0.0, ACCOUNT)))
        future = replace(buy, good_till=past + dt.timedelta(hours=1))
        self.assertEqual(self.guard().check(future, 0, 0.0, ACCOUNT), [])

    def test_exits_ignore_buying_power(self):
        exit_ = OrderIntent("AAPL", "SELL", 3, 100.0, closing=True)
        self.assertEqual(self.guard().check(exit_, 0, 0.0, AccountState(None, {"AAPL": 3.0})), [])


class DrawdownTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "drawdown_state.json"
        self.day = dt.date(2026, 10, 19)

    def tearDown(self):
        self.tmp.cleanup()

    def breaker(self, limit=3000.0):
        return DrawdownBreaker(self.path, limit, today=lambda: self.day)

    def guard(self):
        limits = replace(LIMITS, max_drawdown_usd=3000.0, max_daily_loss=1e9)
        return RiskGuard(limits, Path(self.tmp.name) / "state.json", today=lambda: self.day,
                         drawdown_path=self.path)

    def test_cumulative_drawdown_over_days_trips_and_persists(self):
        guard = self.guard()
        for day, pnl in ((19, 1000.0), (20, -1500.0), (21, -1400.0)):  # cum 1000, -500, -1900
            self.day = dt.date(2026, 10, day)
            guard.update_daily_pnl(pnl)
        self.assertFalse(guard.halted)  # peak 1000, drawdown 2900 < 3000
        self.day = dt.date(2026, 10, 22)
        guard.update_daily_pnl(-200.0)  # cum -2100: drawdown 3100
        self.assertTrue(guard.halted)
        self.assertIn("drawdown", guard.halt_reason)
        self.assertTrue(any("drawdown breaker" in p for p in guard.check(OK, 0, 0.0)))
        self.day = dt.date(2026, 10, 23)  # survives restart and a new day
        guard = self.guard()
        guard.update_daily_pnl(500.0)
        self.assertTrue(guard.halted)

    def test_intraday_updates_keep_last_value_of_day(self):
        breaker = self.breaker()
        breaker.update(-100.0)
        breaker.update(-2500.0)
        breaker.update(-200.0)
        self.assertEqual(breaker.cumulative(), -200.0)
        self.assertFalse(breaker.tripped)

    def test_reset_rearms_from_current_level(self):
        breaker = self.breaker()
        breaker.update(-3000.0)
        self.assertTrue(breaker.tripped)
        breaker.reset()
        reloaded = self.breaker()
        self.assertFalse(reloaded.tripped)
        self.assertEqual(reloaded.state["peak"], -3000.0)
        self.day = dt.date(2026, 10, 20)
        self.assertFalse(reloaded.update(-100.0))

    def test_unknown_pnl_is_ignored(self):
        breaker = self.breaker()
        self.assertFalse(breaker.update(None))
        self.assertFalse(self.path.exists())

    def test_corrupt_file_fails_closed(self):
        self.path.write_text('{"days": {"2026-10-19": -5')
        with self.assertRaises(StateCorrupt):
            self.breaker()
        self.path.write_text('{"days": [], "peak": 0, "tripped": false}')
        with self.assertRaises(StateCorrupt):
            self.breaker()

    def test_exits_allowed_while_tripped(self):
        guard = self.guard()
        guard.update_daily_pnl(-3000.0)
        exit_ = OrderIntent("NVDA", "SELL", 1, 100.0, closing=True)
        self.assertEqual(guard.check(exit_, 0, None, AccountState(None, {"NVDA": 1.0})), [])


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
