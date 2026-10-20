"""Risk-policy regression tests for the LIVE config/config.json and config/rules.json.

Two layers, so a change to the execution limits is always deliberate and reviewable:

1. APPROVED_LIMITS pins the approved values exactly. Editing a limit in the config
   files fails this test until the table below is updated in the same change, so
   every change to what the bot may do shows up in a reviewed diff.
2. A reference scenario checks that those limits stay inside the portfolio model:
   the reference account below and the 2026-10-19 watchlist, each with a typical
   (2%) and the tightest possible (~1.2%: LOD - 1% plus the 0.2% limit offset) stop,
   are run through the bot's real sizing (strategy._size_entry) and risk guard.

Changing a limit: update APPROVED_LIMITS (with the date and reason) and keep the
scenario passing. If the account changes materially, update REFERENCE_NET_LIQ too.

Run: python -m unittest discover -s tests -p test_risk_policy.py -v
"""
import os

os.environ["PAPERBOT_NOTIFY"] = "off"  # tests must never send real notifications

import json
import logging
import tempfile
import unittest
from pathlib import Path

from paperbot import paths
from paperbot.trading import strategy
from paperbot.trading.risk import AccountState, RiskGuard, load_limits, round_trip_commission

# Approved 2026-10-20: caps raised so high-priced watchlist names (up to ~$1,360/share)
# can size above the commission-drag floor; daily loss and drawdown scaled with R.
APPROVED_LIMITS = {
    "allowed_symbols": ["*"],
    "limit_orders_only": True,
    "max_quantity_per_order": 500,
    "max_order_value": 30000.0,
    "max_orders_per_day": 5,
    "max_open_orders": 2,
    "max_daily_loss": 2000.0,
    "max_risk_per_trade_pct": 1.0,
    "max_position_size_pct_of_portfolio": 10.0,
    "max_concurrent_positions": 5,
    "max_commission_pct_of_risk": 1.0,
    "max_drawdown_usd": 6000.0,
}

# Paper account on 2026-10-19: SGD 1,000,013.88 NetLiquidation at 1.2802 SGD per USD.
REFERENCE_NET_LIQ = 781_120.0
# data/watchlist.txt opens on 2026-10-19 (top S&P 500 gappers).
REFERENCE_PRICES = {"INTC": 94.53, "KLAC": 172.66, "ANET": 182.87, "MRVL": 240.10,
                    "LRCX": 275.19, "AVGO": 338.77, "PANW": 356.00, "AMAT": 425.60,
                    "DELL": 470.50, "AMD": 551.97, "STX": 664.37, "MU": 913.01,
                    "SNDK": 1358.50}
STOP_DISTANCES = {"typical 2%": 0.02, "tightest ~1.2%": 0.01}  # below the ask; +0.2% limit offset

# Envelope policy, as fractions of the reference account.
MAX_AGGREGATE_EXPOSURE = 0.25  # all concurrent positions at the order-value cap
MAX_DAILY_LOSS = 0.005  # daily loss halt
MAX_DRAWDOWN = 0.01  # multi-day breaker


def live_limits():
    return load_limits(json.loads(paths.CONFIG_FILE.read_text()), paths.RULES_FILE)


class ApprovedLimitsTest(unittest.TestCase):
    def test_live_limits_match_the_approved_table(self):
        live = vars(live_limits())
        changed = {k: (APPROVED_LIMITS.get(k), live.get(k))
                   for k in set(APPROVED_LIMITS) | set(live)
                   if APPROVED_LIMITS.get(k) != live.get(k)}
        self.assertEqual(changed, {}, "limits changed (approved, live): update APPROVED_LIMITS "
                                      "in this file deliberately, with the date and reason")


class ReferenceScenarioTest(unittest.TestCase):
    """Every reference trade, sized by the bot, stays inside value, risk, size and cost limits."""

    @classmethod
    def setUpClass(cls):
        logging.getLogger("paperbot").addHandler(logging.NullHandler())
        cls.limits = live_limits()
        cls.rules = strategy.load_rules()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.guard = RiskGuard(cls.limits, Path(cls.tmp.name) / "state.json")
        cls.account = AccountState(REFERENCE_NET_LIQ, {}, set(), buying_power=4 * REFERENCE_NET_LIQ)
        cls.trades = {}
        for case, distance in STOP_DISTANCES.items():
            for symbol, price in REFERENCE_PRICES.items():
                signal = {"price": price, "stop": round(price * (1 - distance), 2),
                          "gap": 4.0, "rvol": 3.0}
                intent = strategy._size_entry(symbol, signal, cls.rules, REFERENCE_NET_LIQ,
                                              cls.account.buying_power, price)
                cls.trades[(case, symbol)] = intent

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def sized(self):
        """((stop case, symbol), intent) for every reference trade that sized; the
        others are reported by test_every_reference_symbol_is_tradable."""
        return [(key, intent) for key, intent in self.trades.items() if intent is not None]

    def test_every_reference_symbol_is_tradable(self):
        for (case, symbol), intent in self.trades.items():
            with self.subTest(stop=case, symbol=symbol):
                self.assertIsNotNone(intent, f"{symbol} sizes to zero or is blocked by sizing")

    def test_orders_stay_inside_value_risk_and_size_limits(self):
        lim = self.limits
        for (case, symbol), intent in self.sized():
            with self.subTest(stop=case, symbol=symbol):
                self.check_inside_limits(lim, intent)

    def check_inside_limits(self, lim, intent):
        value = intent.quantity * intent.limit_price
        risk = intent.quantity * (intent.limit_price - intent.stop_price)
        self.assertLessEqual(intent.quantity, lim.max_quantity_per_order)
        self.assertLessEqual(value, lim.max_order_value)
        self.assertLessEqual(value, REFERENCE_NET_LIQ * lim.max_position_size_pct_of_portfolio / 100)
        self.assertLessEqual(risk, REFERENCE_NET_LIQ * lim.max_risk_per_trade_pct / 100)
        fees = round_trip_commission(intent.quantity, intent.limit_price)
        self.assertLessEqual(fees, risk * lim.max_commission_pct_of_risk / 100)

    def test_risk_guard_approves_every_sized_order(self):
        for (case, symbol), intent in self.sized():
            with self.subTest(stop=case, symbol=symbol):
                self.assertEqual(self.guard.check(intent, 0, 0.0, self.account), [])

    def test_daily_loss_halt_survives_one_full_stop_out(self):
        worst = max((i.quantity * (i.limit_price - i.stop_price) for _, i in self.sized()),
                    default=0.0)
        self.assertGreaterEqual(self.limits.max_daily_loss, worst,
                                f"one stop-out of {worst:.0f} would halt the whole day")


class EnvelopeTest(unittest.TestCase):
    """The limits themselves, measured against the reference account."""

    def setUp(self):
        self.lim = live_limits()

    def test_one_order_fits_the_position_size_cap(self):
        self.assertLessEqual(self.lim.max_order_value,
                             REFERENCE_NET_LIQ * self.lim.max_position_size_pct_of_portfolio / 100)

    def test_aggregate_exposure_is_bounded(self):
        exposure = self.lim.max_concurrent_positions * self.lim.max_order_value
        self.assertLessEqual(exposure, REFERENCE_NET_LIQ * MAX_AGGREGATE_EXPOSURE,
                             f"{exposure:,.0f} open at the caps")

    def test_loss_limits_are_bounded_and_ordered(self):
        self.assertLessEqual(self.lim.max_daily_loss, REFERENCE_NET_LIQ * MAX_DAILY_LOSS)
        self.assertLessEqual(self.lim.max_drawdown_usd, REFERENCE_NET_LIQ * MAX_DRAWDOWN)
        self.assertGreaterEqual(self.lim.max_drawdown_usd, self.lim.max_daily_loss)


if __name__ == "__main__":
    unittest.main()
