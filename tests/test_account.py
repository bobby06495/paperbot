"""Offline tests for account.py: net liquidation in USD for a non-USD base currency.

Run: python -m unittest discover -s tests -p test_account.py -v
"""
import os

os.environ["PAPERBOT_NOTIFY"] = "off"  # tests must never send real notifications

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

from paperbot.core import account
from paperbot.core import engine as bot
from paperbot.strategies import rule_strategy as strategy

TODAY = dt.date(2026, 10, 19)
SGD = [NS(tag="NetLiquidation", currency="SGD", value="1000013.88"),
       NS(tag="BuyingPower", currency="SGD", value="6666759.20")]


def ib_with(values):
    return mock.Mock(accountValues=lambda acct=None: list(values), positions=lambda acct=None: [])


class NetLiquidationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.config = root / "config.json"
        self.config.write_text(json.dumps({"fx_fallback": "auto", "limits": {}}))
        self.rate_file = root / "fx_rate.json"
        fake_now = mock.Mock(wraps=dt.datetime)
        fake_now.now = lambda tz=None: dt.datetime(2026, 10, 19, 11, 0, tzinfo=account.ET)
        self.fetch = mock.Mock(return_value=1.28)
        self.patchers = [mock.patch.object(account, "RATE_FILE", self.rate_file),
                         mock.patch.object(account.paths, "CONFIG_FILE", self.config),
                         mock.patch.object(account.dt, "datetime", fake_now),
                         mock.patch.object(account, "fetch_yahoo_rate", self.fetch),
                         mock.patch.object(account, "_last", None)]
        for p in self.patchers:
            p.start()

    def tearDown(self):
        for p in reversed(self.patchers):
            p.stop()
        self.tmp.cleanup()

    def test_usd_account_is_used_as_is(self):
        ib = ib_with([NS(tag="NetLiquidation", currency="USD", value="100000.00"), *SGD])
        self.assertEqual(account.net_liquidation_usd(ib, "DU0000000"), 100000.0)
        self.fetch.assert_not_called()
        self.assertFalse(self.rate_file.exists())  # no conversion state at all

    def test_sgd_converted_with_yahoo_once_per_day(self):
        self.assertEqual(account.net_liquidation_usd(ib_with(SGD), "DU0000000"), round(1000013.88 / 1.28, 2))
        self.assertEqual(account.net_liquidation_usd(ib_with(SGD), "DU0000000"), round(1000013.88 / 1.28, 2))
        self.fetch.assert_called_once_with("SGD")  # second call used the saved rate
        saved = json.loads(self.rate_file.read_text())
        self.assertEqual((saved["currency"], saved["rate"], saved["date"]), ("SGD", 1.28, "2026-10-19"))
        conv = account.last_conversion()
        self.assertEqual((conv["currency"], conv["rate"]), ("SGD", 1.28))

    def test_ibkr_exchange_rate_preferred(self):
        values = [*SGD, NS(tag="ExchangeRate", currency="USD", value="1.25")]
        self.assertEqual(account.net_liquidation_usd(ib_with(values), "DU0000000"), round(1000013.88 / 1.25, 2))
        self.fetch.assert_not_called()

    def test_recent_saved_rate_used_when_yahoo_fails(self):
        self.fetch.return_value = None
        self.rate_file.write_text(json.dumps({"currency": "SGD", "rate": 1.3, "date": "2026-10-16"}))
        self.assertEqual(account.net_liquidation_usd(ib_with(SGD), "DU0000000"), round(1000013.88 / 1.3, 2))
        self.assertIn("saved", account.last_conversion()["source"])

    def test_no_rate_fails_closed_with_one_alert_per_day(self):
        self.fetch.return_value = None
        self.rate_file.write_text(json.dumps({"currency": "SGD", "rate": 1.3, "date": "2026-10-01"}))  # too old
        with mock.patch.object(account.log, "error") as alert, mock.patch.object(account.log, "warning"):
            self.assertIsNone(account.net_liquidation_usd(ib_with(SGD), "DU0000000"))
            self.assertIsNone(account.net_liquidation_usd(ib_with(SGD), "DU0000000"))
        alert.assert_called_once()
        self.assertIn("ALERT", alert.call_args.args[0])
        self.fetch.assert_called_once()  # Yahoo retried tomorrow, not every pass

    def test_fallback_off_restores_usd_only(self):
        self.config.write_text(json.dumps({"fx_fallback": "off", "limits": {}}))
        self.assertIsNone(account.net_liquidation_usd(ib_with(SGD), "DU0000000"))
        self.fetch.assert_not_called()

    def test_bad_quote_is_rejected(self):
        self.fetch.return_value = float("nan")
        with mock.patch.object(account.log, "error"):
            self.assertIsNone(account.net_liquidation_usd(ib_with(SGD), "DU0000000"))

    def test_nothing_reported_yet(self):
        self.assertIsNone(account.net_liquidation_usd(ib_with([]), "DU0000000"))
        self.fetch.assert_not_called()

    def test_bot_and_strategy_use_the_converted_value(self):
        expected = round(1000013.88 / 1.28, 2)
        self.assertEqual(strategy.net_liquidation_usd(ib_with(SGD), "DU0000000"), expected)
        self.assertEqual(bot.account_state(ib_with(SGD), "DU0000000", []).net_liquidation, expected)

    def test_telemetry_reads_memory_only(self):
        account.net_liquidation_usd(ib_with(SGD), "DU0000000")
        self.fetch.reset_mock()
        self.assertEqual(account.net_liquidation_usd(None, "DU0000000", fetch=False), round(1000013.88 / 1.28, 2))
        self.fetch.assert_not_called()


class BuyingPowerTest(NetLiquidationTest):
    def test_usd_buying_power_used_as_is(self):
        ib = ib_with([NS(tag="BuyingPower", currency="USD", value="400000"), *SGD])
        self.assertEqual(account.buying_power_usd(ib, "DU0000000"), 400000.0)
        self.fetch.assert_not_called()

    def test_sgd_buying_power_converted(self):
        self.assertEqual(account.buying_power_usd(ib_with(SGD), "DU0000000"),
                         round(6666759.20 / 1.28, 2))

    def test_missing_buying_power_is_none(self):
        self.assertIsNone(account.buying_power_usd(ib_with([]), "DU0000000"))


class AccountValueParsingTest(NetLiquidationTest):
    """IBKR account-value rows as they arrive: odd values, other tags, other currencies."""

    def test_unusable_values_count_as_not_reported(self):
        for bad in ("", "abc", "nan", "inf", "-inf", None):
            with self.subTest(value=bad):
                ib = ib_with([NS(tag="NetLiquidation", currency="USD", value=bad),
                              NS(tag="BuyingPower", currency="USD", value=bad)])
                self.assertIsNone(account.net_liquidation_usd(ib, "DU0000000"))
                self.assertIsNone(account.buying_power_usd(ib, "DU0000000"))
        self.fetch.assert_not_called()  # nothing to convert either

    def test_unusable_usd_row_falls_back_to_the_base_currency(self):
        ib = ib_with([NS(tag="NetLiquidation", currency="USD", value="nan"), *SGD])
        self.assertEqual(account.net_liquidation_usd(ib, "DU0000000"), round(1000013.88 / 1.28, 2))

    def test_segment_tags_and_base_rows_are_ignored(self):
        ib = ib_with([NS(tag="NetLiquidation-S", currency="USD", value="5"),
                      NS(tag="NetLiquidationByCurrency", currency="USD", value="7"),
                      NS(tag="NetLiquidation", currency="BASE", value="9"),
                      NS(tag="BuyingPower", currency="BASE", value="9")])
        self.assertIsNone(account.net_liquidation_usd(ib, "DU0000000"))
        self.assertIsNone(account.buying_power_usd(ib, "DU0000000"))

    def test_values_are_read_for_the_given_account(self):
        ib = mock.Mock(accountValues=mock.Mock(return_value=[
            NS(tag="NetLiquidation", currency="USD", value="100")]))
        account.net_liquidation_usd(ib, "DU0000000")
        ib.accountValues.assert_called_once_with("DU0000000")

    def test_zero_or_negative_values_block_entries(self):
        from paperbot.core.risk import AccountState, OrderIntent, RiskGuard
        from paperbot.core import profile as profiles
        config = {"limits": {"allowed_symbols": ["*"], "limit_orders_only": True}}
        limits = profiles.load("trend_join_long", config=config).limits
        guard = RiskGuard(limits, Path(self.tmp.name) / "risk.json")
        intent = OrderIntent("AAPL", "BUY", 200, 100.0, stop_price=90.0)
        for net_liq, buying_power in ((0.0, 1e6), (-5000.0, 1e6), (None, 1e6),
                                      (1e6, 0.0), (1e6, -1.0), (1e6, None)):
            with self.subTest(net_liq=net_liq, buying_power=buying_power):
                state = AccountState(net_liq, {}, set(), buying_power=buying_power)
                self.assertNotEqual(guard.check(intent, 0, 0.0, state), [])
        ok = AccountState(1e6, {}, set(), buying_power=1e6)
        self.assertEqual(guard.check(intent, 0, 0.0, ok), [])


class DailyPnlTest(unittest.TestCase):
    """account.daily_pnl(): 0.0 only when the account is provably flat today."""
    NOW = dt.datetime(2026, 10, 19, 10, 5, tzinfo=account.ET)
    NAN = NS(dailyPnL=float("nan"))

    def setUp(self):
        fake_now = mock.Mock(wraps=dt.datetime)
        fake_now.now = lambda tz=None: self.NOW
        self.patchers = [mock.patch.object(account.dt, "datetime", fake_now),
                         mock.patch.object(account, "_last_daily", None)]
        for p in self.patchers:
            p.start()

    def tearDown(self):
        for p in reversed(self.patchers):
            p.stop()

    @staticmethod
    def ib(positions=(), fills=()):
        ib = mock.Mock()
        ib.positions.return_value = list(positions)
        ib.reqExecutions.return_value = list(fills)
        return ib

    @staticmethod
    def fill(acct="DU0000000", when=dt.datetime(2026, 10, 19, 14, 5, tzinfo=dt.timezone.utc)):
        return NS(execution=NS(acctNumber=acct, time=when))

    def test_ibkr_value_used_as_is(self):
        ib = self.ib()
        self.assertEqual(account.daily_pnl(ib, "DU0000000", NS(dailyPnL=-250.0)), -250.0)
        ib.reqExecutions.assert_not_called()
        self.assertEqual(account.last_daily_pnl()["source"], "IBKR")

    def test_flat_all_day_is_zero(self):
        self.assertEqual(account.daily_pnl(self.ib(), "DU0000000", self.NAN), 0.0)
        self.assertIn("flat today", account.last_daily_pnl()["source"])
        self.assertEqual(account.daily_pnl(self.ib(), "DU0000000", NS(dailyPnL=None)), 0.0)

    def test_position_held_stays_unknown(self):
        ib = self.ib(positions=[NS(position=1.0)])
        self.assertIsNone(account.daily_pnl(ib, "DU0000000", self.NAN))
        ib.reqExecutions.assert_not_called()

    def test_traded_today_stays_unknown(self):
        self.assertIsNone(account.daily_pnl(self.ib(fills=[self.fill()]), "DU0000000", self.NAN))
        self.assertIn("traded today", account.last_daily_pnl()["source"])
        self.assertIsNone(account.daily_pnl(self.ib(fills=[self.fill(acct="")]), "DU0000000", self.NAN))

    def test_other_accounts_and_earlier_days_ignored(self):
        fills = [self.fill(acct="U0000000"),  # another account (all-zero placeholder)
                 self.fill(when=dt.datetime(2026, 10, 16, 19, 0, tzinfo=dt.timezone.utc))]
        self.assertEqual(account.daily_pnl(self.ib(fills=fills), "DU0000000", self.NAN), 0.0)

    def test_check_failure_fails_closed(self):
        ib = self.ib()
        ib.reqExecutions.side_effect = TimeoutError("slow")
        with mock.patch.object(account.log, "warning"):
            self.assertIsNone(account.daily_pnl(ib, "DU0000000", self.NAN))

    def test_guard_allows_the_first_entry_only_when_flat(self):
        from paperbot.core.risk import AccountState, Limits, OrderIntent, RiskGuard
        limits = Limits(allowed_symbols=["*"], max_quantity_per_order=1, max_order_value=2500.0,
                        max_orders_per_day=5, max_open_orders=2, max_daily_loss=1000.0,
                        max_risk_per_trade_pct=1.0, max_position_size_pct_of_portfolio=10,
                        max_concurrent_positions=5)
        with tempfile.TemporaryDirectory() as tmp:
            guard = RiskGuard(limits, Path(tmp) / "state.json")
            buy = OrderIntent("NVDA", "BUY", 1, 189.58, reason="trend join", stop_price=183.55)
            state = AccountState(781120.46, {}, set(), buying_power=3_124_481.84)
            flat = account.daily_pnl(self.ib(), "DU0000000", self.NAN)
            self.assertEqual(guard.check(buy, 0, flat, state), [])
            held = account.daily_pnl(self.ib(positions=[NS(position=1.0)]), "DU0000000", self.NAN)
            self.assertIn("daily P&L unavailable, cannot check loss limit", guard.check(buy, 0, held, state))


if __name__ == "__main__":
    unittest.main()
