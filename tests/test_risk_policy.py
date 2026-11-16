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

# Approved 2026-11-16 (owner): the bot runs a $5,000 USD account, paper and live alike.
# capital_usd caps what sizing treats as the account (the paper account is far larger);
# daily_profit_target_usd 100 stops trading for the day. Every strategy: positions up
# to 25% ($1,250, so the $2 minimum round-trip commission stays near 8% of a $25 R),
# fees up to 10% of R (was 1-2%: unreachable at this size), daily loss $50 (1%),
# drawdown breaker $500 (10%). Risk per trade, position counts and order counts unchanged.
FIVE_K = {"capital_usd": 5000.0, "daily_profit_target_usd": 100.0,
          "max_order_value": 1250.0, "max_position_size_pct_of_portfolio": 25.0,
          "max_daily_loss": 50.0, "max_drawdown_usd": 500.0}
APPROVED_LIMITS = {
    "trend_join_long": {
        "allowed_symbols": ["*"], "limit_orders_only": True, **FIVE_K,
        "max_quantity_per_order": 500, "max_orders_per_day": 8, "max_open_orders": 3,
        "max_risk_per_trade_pct": 0.5, "max_concurrent_positions": 4,
        "max_commission_pct_of_risk": 10.0,
    },
    "macro_swing_etf": {
        "allowed_symbols": ["*"], "limit_orders_only": True, **FIVE_K,
        "max_quantity_per_order": 1000, "max_orders_per_day": 5, "max_open_orders": 3,
        "max_risk_per_trade_pct": 0.5, "max_concurrent_positions": 3,
        "max_commission_pct_of_risk": 10.0,
    },
    # Fee rule kept at 5% of R: at $5,000 no index-ETF scalp can size (see SKIPPED).
    "equity_mean_reversion": {
        "allowed_symbols": ["*"], "limit_orders_only": True, **FIVE_K,
        "max_quantity_per_order": 500, "max_orders_per_day": 5, "max_open_orders": 2,
        "max_risk_per_trade_pct": 0.5, "max_concurrent_positions": 2,
        "max_commission_pct_of_risk": 5.0,
    },
}

# The account the bot may use: capital_usd (the IBKR paper account is much larger).
REFERENCE_NET_LIQ = 5_000.0
SCENARIOS = {  # strategy: (reference prices, stop distances below the ask)
    # Gappers the bot saw (data/watchlist.txt, backtests) priced up to ~$300, with the
    # median (2.6%) and the tightest (1.7%) stop distance of the Sept-Oct 2026 backtests.
    "trend_join_long": (
        {"AZTA": 48.22, "MDLZ": 64.11, "MO": 75.54, "INTC": 94.53, "MDT": 92.53,
         "KLAC": 172.66, "ANET": 182.87, "MRSH": 187.90, "MRVL": 240.10, "LRCX": 275.19},
        {"typical 2.6%": 0.026, "tight 1.7%": 0.017}),
    # Representative sector-ETF price levels (not quotes) and 2 x ATR stops for a calm
    # (ATR 1.5%) and a volatile (ATR 2.5%) market.
    "macro_swing_etf": (
        {"XLK": 230.0, "XLF": 50.0, "XLE": 90.0, "XLV": 145.0, "XLI": 135.0, "XLY": 210.0,
         "XLP": 80.0, "XLU": 80.0, "XLB": 90.0, "XLRE": 42.0, "XLC": 100.0},
        {"2xATR calm 3%": 0.03, "2xATR volatile 5%": 0.05}),
}
# Trades that must NOT size at $5,000 (intended): whole shares of expensive names leave
# R too small for the $2 round trip (or not one share fits $1,250), and index-ETF
# scalps with 0.15-0.3% stops can never cover their commission at this account size.
SKIPPED = {
    "trend_join_long": ({"AVGO": 338.77, "DELL": 470.50, "STX": 664.37, "MU": 913.01,
                         "LLY": 1258.69, "SNDK": 1358.50}, {"typical 2.6%": 0.026}),
    "equity_mean_reversion": ({"SPY": 600.0, "QQQ": 530.0, "IWM": 230.0},
                              {"1sd quiet 0.15%": 0.0015, "1sd busy 0.3%": 0.003}),
}

# Envelope policy, as fractions of the reference account.
MAX_AGGREGATE_EXPOSURE = 1.0  # all positions at the order-value cap: never above capital
MAX_DAILY_LOSS = 0.01  # daily loss halt
MAX_DRAWDOWN = 0.10  # multi-day breaker


def live_limits(key: str):
    return load_limits(json.loads(paths.CONFIG_FILE.read_text()), paths.RULES_FILE, key)


def live_strategies() -> list[str]:
    return profiles.strategy_keys(paths.RULES_FILE)


class ApprovedLimitsTest(unittest.TestCase):
    def test_every_strategy_is_approved(self):
        self.assertEqual(sorted(live_strategies()), sorted(APPROVED_LIMITS),
                         "rules.json strategies and APPROVED_LIMITS differ: approve new "
                         "strategies here (and add a SCENARIO), remove deleted ones")
        # every strategy has reference trades that size, or is documented as untradable
        self.assertEqual(sorted(set(SCENARIOS) | set(SKIPPED)), sorted(APPROVED_LIMITS))

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
        cls.account = AccountState(REFERENCE_NET_LIQ, {}, set(), buying_power=REFERENCE_NET_LIQ)
        cls.trades, cls.skipped, cls.guards, cls.limits = {}, {}, {}, {}
        for table, out in ((SCENARIOS, cls.trades), (SKIPPED, cls.skipped)):
            for key, (prices, distances) in table.items():
                profile = profiles.load(key)
                cls.limits[key] = profile.limits
                cls.guards[key] = RiskGuard(profile.limits, Path(cls.tmp.name) / f"{key}.json")
                for case, distance in distances.items():
                    for symbol, price in prices.items():
                        signal = {"price": price, "stop": round(price * (1 - distance), 2),
                                  "gap": 4.0, "rvol": 3.0}
                        out[(key, case, symbol)] = strategy._size_entry(
                            symbol, signal, profile, REFERENCE_NET_LIQ,
                            cls.account.buying_power, price)

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

    def test_untradable_at_this_account_size_is_skipped(self):
        for (key, case, symbol), intent in self.skipped.items():
            with self.subTest(strategy=key, stop=case, symbol=symbol):
                self.assertIsNone(intent, f"{symbol} was expected to be skipped at $5,000")

    def test_capital_cap_sizes_a_large_account_like_the_reference(self):
        """The paper account (~$780k) sizes exactly like $5,000 once capital_usd applies."""
        from paperbot.core.risk import capital_view
        for key, (prices, distances) in SCENARIOS.items():
            profile = profiles.load(key)
            liq, bp = capital_view(profile.limits, 781_120.0, 3_124_480.0, 0.0)
            self.assertEqual((liq, bp), (REFERENCE_NET_LIQ, REFERENCE_NET_LIQ))
            symbol, price = next(iter(prices.items()))
            distance = next(iter(distances.values()))
            signal = {"price": price, "stop": round(price * (1 - distance), 2), "gap": 4.0,
                      "rvol": 3.0}
            with self.subTest(strategy=key):
                self.assertEqual(strategy._size_entry(symbol, signal, profile, liq, bp, price),
                                 self.trades[(key, next(iter(distances)), symbol)])

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
