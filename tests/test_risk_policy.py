"""Risk-policy regression tests for the LIVE config/config.json and config/rules.json.

Two layers per strategy, so a change to what the bot may do is always deliberate:

1. APPROVED_LIMITS pins every strategy's approved limits exactly. Editing a limit
   in the config files, or adding a strategy to rules.json, fails this test until
   the table below is updated in the same reviewed change.
2. A reference scenario per strategy checks that those limits stay inside the
   portfolio model: the reference account below and reference prices, each with
   realistic stop distances, are run through the bot's real sizing
   (rule_strategy._size_entry) and risk guard.

Changing a limit or adding a strategy: update APPROVED_LIMITS (date and reason) and
SCENARIOS, and keep everything passing. If the account changes materially, update
REFERENCE_NET_LIQ too.

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
from paperbot.core import profile as profiles
from paperbot.core.risk import AccountState, RiskGuard, load_limits, round_trip_commission
from paperbot.strategies import rule_strategy as strategy

APPROVED_LIMITS = {
    # Approved 2026-10-20: caps raised so high-priced watchlist names (up to ~$1,360/share)
    # can size above the commission-drag floor; daily loss and drawdown scaled with R.
    "trend_join_long": {
        "allowed_symbols": ["*"], "limit_orders_only": True,
        "max_quantity_per_order": 500, "max_order_value": 30000.0, "max_orders_per_day": 5,
        "max_open_orders": 2, "max_daily_loss": 2000.0, "max_risk_per_trade_pct": 1.0,
        "max_position_size_pct_of_portfolio": 10.0, "max_concurrent_positions": 5,
        "max_commission_pct_of_risk": 1.0, "max_drawdown_usd": 6000.0,
    },
    # Approved 2026-10-20 (Phase 3): same caps and loss limits as Trend Join Long; up to
    # 3 overnight positions, 1,000 shares so ~$40 sector ETFs can reach the $30k cap,
    # 3 working orders so the end-of-day scan can enter up to 3 positions at once.
    "macro_swing_etf": {
        "allowed_symbols": ["*"], "limit_orders_only": True,
        "max_quantity_per_order": 1000, "max_order_value": 30000.0, "max_orders_per_day": 5,
        "max_open_orders": 3, "max_daily_loss": 2000.0, "max_risk_per_trade_pct": 1.0,
        "max_position_size_pct_of_portfolio": 10.0, "max_concurrent_positions": 3,
        "max_commission_pct_of_risk": 1.0, "max_drawdown_usd": 6000.0,
    },
    # Approved 2026-10-20 (Phase 4): scalps with ~0.15% stops on $500+ ETFs cannot keep
    # fees under 1% of R at $30k, so this strategy alone allows fees up to 5% of R;
    # half the per-trade risk of the others, at most 2 positions.
    "equity_mean_reversion": {
        "allowed_symbols": ["*"], "limit_orders_only": True,
        "max_quantity_per_order": 500, "max_order_value": 30000.0, "max_orders_per_day": 5,
        "max_open_orders": 2, "max_daily_loss": 2000.0, "max_risk_per_trade_pct": 0.5,
        "max_position_size_pct_of_portfolio": 10.0, "max_concurrent_positions": 2,
        "max_commission_pct_of_risk": 5.0, "max_drawdown_usd": 6000.0,
    },
}

# Paper account on 2026-10-19: SGD 1,000,013.88 NetLiquidation at 1.2802 SGD per USD.
REFERENCE_NET_LIQ = 781_120.0
SCENARIOS = {  # strategy: (reference prices, stop distances below the ask)
    # data/watchlist.txt opens on 2026-10-19 (top S&P 500 gappers); a typical (2%) and
    # the tightest possible (~1.2%: LOD - 1% plus the 0.2% limit offset) stop.
    "trend_join_long": (
        {"INTC": 94.53, "KLAC": 172.66, "ANET": 182.87, "MRVL": 240.10, "LRCX": 275.19,
         "AVGO": 338.77, "PANW": 356.00, "AMAT": 425.60, "DELL": 470.50, "AMD": 551.97,
         "STX": 664.37, "MU": 913.01, "SNDK": 1358.50},
        {"typical 2%": 0.02, "tightest ~1.2%": 0.01}),
    # Representative sector-ETF price levels (not quotes) and 2 x ATR stops for a calm
    # (ATR 1.5%) and a volatile (ATR 2.5%) market. A ~$40 ETF with a stop under ~2.4%
    # is skipped by the 1% commission rule (fees are per share): that is intended.
    "macro_swing_etf": (
        {"XLK": 230.0, "XLF": 50.0, "XLE": 90.0, "XLV": 145.0, "XLI": 135.0, "XLY": 210.0,
         "XLP": 80.0, "XLU": 80.0, "XLB": 90.0, "XLRE": 42.0, "XLC": 100.0},
        {"2xATR calm 3%": 0.03, "2xATR volatile 5%": 0.05}),
    # Representative index-ETF price levels (not quotes); 1-sigma stops of a quiet
    # (0.15%) and a busy (0.3%) session on 1-minute VWAP deviations.
    "equity_mean_reversion": (
        {"SPY": 600.0, "QQQ": 530.0, "IWM": 230.0},
        {"1sd quiet 0.15%": 0.0015, "1sd busy 0.3%": 0.003}),
}

# Envelope policy, as fractions of the reference account.
MAX_AGGREGATE_EXPOSURE = 0.25  # all concurrent positions at the order-value cap
MAX_DAILY_LOSS = 0.005  # daily loss halt
MAX_DRAWDOWN = 0.01  # multi-day breaker


def live_limits(key: str):
    return load_limits(json.loads(paths.CONFIG_FILE.read_text()), paths.RULES_FILE, key)


def live_strategies() -> list[str]:
    return profiles.strategy_keys(paths.RULES_FILE)


class ApprovedLimitsTest(unittest.TestCase):
    def test_every_strategy_is_approved(self):
        self.assertEqual(sorted(live_strategies()), sorted(APPROVED_LIMITS),
                         "rules.json strategies and APPROVED_LIMITS differ: approve new "
                         "strategies here (and add a SCENARIO), remove deleted ones")
        self.assertEqual(sorted(SCENARIOS), sorted(APPROVED_LIMITS))

    def test_live_limits_match_the_approved_tables(self):
        for key, approved in APPROVED_LIMITS.items():
            with self.subTest(strategy=key):
                live = vars(live_limits(key))
                changed = {k: (approved.get(k), live.get(k)) for k in set(approved) | set(live)
                           if approved.get(k) != live.get(k)}
                self.assertEqual(changed, {}, "limits changed (approved, live): update "
                                              "APPROVED_LIMITS deliberately, with the date and reason")


class ReferenceScenarioTest(unittest.TestCase):
    """Every reference trade, sized by the bot, stays inside value, risk, size and cost limits."""

    @classmethod
    def setUpClass(cls):
        logging.getLogger("paperbot").addHandler(logging.NullHandler())
        cls.tmp = tempfile.TemporaryDirectory()
        cls.account = AccountState(REFERENCE_NET_LIQ, {}, set(), buying_power=4 * REFERENCE_NET_LIQ)
        cls.trades, cls.guards, cls.limits = {}, {}, {}
        for key, (prices, distances) in SCENARIOS.items():
            profile = profiles.load(key)
            cls.limits[key] = profile.limits
            cls.guards[key] = RiskGuard(profile.limits, Path(cls.tmp.name) / f"{key}.json")
            for case, distance in distances.items():
                for symbol, price in prices.items():
                    signal = {"price": price, "stop": round(price * (1 - distance), 2),
                              "gap": 4.0, "rvol": 3.0}
                    cls.trades[(key, case, symbol)] = strategy._size_entry(
                        symbol, signal, profile, REFERENCE_NET_LIQ, cls.account.buying_power, price)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def sized(self):
        """((strategy, stop case, symbol), intent) for every reference trade that sized;
        the others are reported by test_every_reference_symbol_is_tradable."""
        return [(key, intent) for key, intent in self.trades.items() if intent is not None]

    def test_every_reference_symbol_is_tradable(self):
        for (key, case, symbol), intent in self.trades.items():
            with self.subTest(strategy=key, stop=case, symbol=symbol):
                self.assertIsNotNone(intent, f"{symbol} sizes to zero or is blocked by sizing")

    def test_orders_stay_inside_value_risk_and_size_limits(self):
        for (key, case, symbol), intent in self.sized():
            with self.subTest(strategy=key, stop=case, symbol=symbol):
                self.check_inside_limits(self.limits[key], intent)

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
        for (key, case, symbol), intent in self.sized():
            with self.subTest(strategy=key, stop=case, symbol=symbol):
                self.assertEqual(self.guards[key].check(intent, 0, 0.0, self.account), [])

    def test_daily_loss_halt_survives_one_full_stop_out(self):
        for key in SCENARIOS:
            with self.subTest(strategy=key):
                worst = max((i.quantity * (i.limit_price - i.stop_price)
                             for (k, _, _), i in self.sized() if k == key), default=0.0)
                self.assertGreaterEqual(self.limits[key].max_daily_loss, worst,
                                        f"one stop-out of {worst:.0f} would halt the whole day")


class EnvelopeTest(unittest.TestCase):
    """Each strategy's limits, measured against the reference account."""

    def each(self):
        for key in APPROVED_LIMITS:
            with self.subTest(strategy=key):
                yield live_limits(key)

    def test_one_order_fits_the_position_size_cap(self):
        for lim in self.each():
            self.assertLessEqual(lim.max_order_value,
                                 REFERENCE_NET_LIQ * lim.max_position_size_pct_of_portfolio / 100)

    def test_aggregate_exposure_is_bounded(self):
        for lim in self.each():
            exposure = lim.max_concurrent_positions * lim.max_order_value
            self.assertLessEqual(exposure, REFERENCE_NET_LIQ * MAX_AGGREGATE_EXPOSURE,
                                 f"{exposure:,.0f} open at the caps")

    def test_loss_limits_are_bounded_and_ordered(self):
        for lim in self.each():
            self.assertLessEqual(lim.max_daily_loss, REFERENCE_NET_LIQ * MAX_DAILY_LOSS)
            self.assertLessEqual(lim.max_drawdown_usd, REFERENCE_NET_LIQ * MAX_DRAWDOWN)
            self.assertGreaterEqual(lim.max_drawdown_usd, lim.max_daily_loss)


if __name__ == "__main__":
    unittest.main()
