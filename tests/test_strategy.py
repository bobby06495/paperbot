"""Offline tests for the strategy engine (strategies/rule_strategy.py running the
Trend Join Long profile from rules.json), using a fake IB object.

Covers stop reconciliation, orphan stops, partial profit, force close, the
filter reasons from evaluate(), the check-only gates and the safety log.
Run: python -m unittest discover -s tests -p test_strategy.py -v
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

from paperbot import paths
from paperbot.core import market_data as md
from paperbot.core import orders, sizing
from paperbot.core import profile as profiles
from paperbot.core.common import ET
from paperbot.core.profile import TimeFilter
from paperbot.core.risk import (REF_ENTRY, REF_EXIT, REF_STOP, CancelRequest, OrderIntent,
                                StopUpdate)
from paperbot.runtime.runstate import write_json_atomic
from paperbot.strategies import rule_strategy as strategy

BAR = dt.timedelta(minutes=5)  # Trend Join Long's timeframe
UTC = dt.timezone.utc


def bar(start: dt.datetime, close: float, low: float | None = None):
    return NS(date=start.astimezone(UTC), open=close, high=close, low=low or close,
              close=close, volume=1000)


def trade(ref: str, order_id: int, qty: int, aux: float = 0.0, age_s: float = 0.0,
          oca: str = ""):
    sent = dt.datetime.now(UTC) - dt.timedelta(seconds=age_s)
    return NS(order=NS(orderRef=ref, orderId=order_id, totalQuantity=qty, auxPrice=aux,
                       ocaGroup=oca),
              contract=NS(symbol="AAPL"), orderStatus=NS(filled=0),
              log=[NS(time=sent)], isDone=lambda: False)


class FakeIB:
    def __init__(self, shares: int, trades: list, close: float, now: dt.datetime,
                 quote: tuple | None = None, buying_power: float = 4_000_000.0):
        self._positions = ([NS(contract=NS(symbol="AAPL", secType="STK"), position=float(shares),
                               avgCost=100.0)] if shares else [])
        self._trades = trades
        start = now.replace(hour=9, minute=30, second=0, microsecond=0)
        self._bars = [bar(start + i * BAR, close) for i in range(6)]
        # live top of book: (bid, ask), default one cent either side of the bar close
        self.quote = quote if quote is not None else (close - 0.01, close + 0.01)
        self.buying_power = buying_power
        self.subscribed, self.cancelled = [], []

    def reqMktData(self, contract, *args, **kwargs):
        self.subscribed.append(contract.symbol)
        bid, ask = self.quote
        return NS(contract=contract, bid=bid, ask=ask, halted=0.0)

    def cancelMktData(self, contract):
        self.cancelled.append(contract.symbol)

    def sleep(self, seconds):
        pass

    def positions(self, account=None):
        return self._positions

    def openTrades(self):
        return self._trades

    def qualifyContracts(self, contract):
        contract.conId = 1

    def reqHistoricalData(self, *args, **kwargs):
        return self._bars

    def accountValues(self, account=None):
        return [NS(tag="NetLiquidation", currency="USD", value="1000000"),
                NS(tag="BuyingPower", currency="USD", value=str(self.buying_power))]


def _live_tjl():
    return profiles.load("trend_join_long")


def partial_price() -> float:
    """A bar close just past the live partial-profit target for entry 100 / stop 95
    (R = 5), still below the breakeven trigger, so only the partial fires."""
    tjl = _live_tjl()
    partial, breakeven = tjl.block("manage", "partial_profit"), tjl.block("manage", "breakeven")
    price = round(100.0 + 5.0 * partial.at_R + 0.25, 2)
    assert breakeven is None or price < 100.0 + 5.0 * breakeven.at_R, "fixture needs a gap"
    return price


def live_cutoff(day: dt.datetime) -> dt.datetime:
    """The end of the live latest_entry_et minute on `day` (when unfilled entries die)."""
    latest = profiles.hhmm(_live_tjl().time_filter.latest_entry_et)
    return day.replace(hour=latest.hour, minute=latest.minute, second=0) + dt.timedelta(minutes=1)


class StrategyTestBase(unittest.TestCase):
    """Temp state and safety-log files, cleared caches, and decide() at a fixed time."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tmp.name) / "strategy_state.json"
        self.safety_log = Path(self.tmp.name) / "safety_log.jsonl"
        self.patches = [mock.patch.object(strategy, "STATE", self.state_path),
                        mock.patch.object(strategy, "SAFETY_LOG", self.safety_log),
                        mock.patch.object(strategy, "_universe", lambda profile, today: []),
                        mock.patch.object(strategy, "ACTIVE", "trend_join_long"),
                        mock.patch.object(strategy, "_profile", None)]
        for p in self.patches:
            p.start()
        orders._orphans.clear()
        md.clear_caches()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def own(self, **extra):
        pos = {"initial_stop": 95.0, "stop": 95.0, "breakeven": False, "partial_done": False}
        write_json_atomic(self.state_path, {"positions": {"AAPL": {**pos, **extra}},
                                            "entered": {}})

    def decide(self, ib, now):
        with mock.patch.object(strategy, "_now", lambda: now):
            return strategy.decide(ib, "DU1")


class HarnessProfileTest(StrategyTestBase):
    """The mocks above patch files and the universe, never the strategy itself."""

    def test_harness_runs_the_real_trend_join_long_profile(self):
        from paperbot.core.risk import load_limits
        loaded = strategy.load_profile()
        rules = json.loads(paths.RULES_FILE.read_text())
        self.assertEqual(loaded.key, "trend_join_long")
        self.assertEqual(loaded.raw, rules["strategies"]["trend_join_long"])
        self.assertEqual(loaded.limits, load_limits(json.loads(paths.CONFIG_FILE.read_text()),
                                                    paths.RULES_FILE, "trend_join_long"))
        self.assertEqual(loaded.refs, (REF_ENTRY, REF_STOP, REF_EXIT))


class DecideTest(StrategyTestBase):
    def test_missing_stop_is_restored(self):
        now = dt.datetime(2026, 10, 19, 11, 0, tzinfo=ET)
        self.own()
        reqs = self.decide(FakeIB(9, [], 101.0, now), now)
        self.assertIn(StopUpdate("AAPL", 9, 95.0, "restore protective stop"), reqs)

    def test_stop_size_follows_position(self):
        now = dt.datetime(2026, 10, 19, 11, 0, tzinfo=ET)
        self.own()
        reqs = self.decide(FakeIB(6, [trade(REF_STOP, 5, 9, 95.0)], 101.0, now), now)
        self.assertIn(StopUpdate("AAPL", 6, 95.0, "sync protective stop"), reqs)

    def test_orphan_stop_cancelled_on_second_loop(self):
        now = dt.datetime(2026, 10, 19, 11, 0, tzinfo=ET)
        ib = FakeIB(0, [trade(REF_STOP, 5, 9, 95.0)], 101.0, now)
        self.assertEqual(self.decide(ib, now), [])
        self.assertIn(CancelRequest(5, "AAPL", "orphan stop, no position"), self.decide(ib, now))

    def test_partial_profit_shrinks_stop_first(self):
        now = dt.datetime(2026, 10, 19, 11, 0, tzinfo=ET)
        self.own()
        # entry 100, stop 95 -> R = 5; the bar closes just past the live partial target
        reqs = self.decide(FakeIB(9, [trade(REF_STOP, 5, 9, 95.0)], partial_price(), now), now)
        kinds = [type(r) for r in reqs]
        self.assertEqual(kinds, [StopUpdate, OrderIntent])
        self.assertEqual((reqs[0].quantity, reqs[1].quantity, reqs[1].closing), (6, 3, True))

    def test_force_close_cancels_stop_then_sells_all(self):
        now = dt.datetime(2026, 10, 19, 15, 52, tzinfo=ET)
        self.own()
        reqs = self.decide(FakeIB(9, [trade(REF_STOP, 5, 9, 95.0)], 101.0, now), now)
        self.assertIsInstance(reqs[0], CancelRequest)
        self.assertEqual(reqs[0].order_id, 5)
        self.assertEqual((reqs[-1].action, reqs[-1].quantity, reqs[-1].closing), ("SELL", 9, True))
        self.assertEqual(len(reqs), 2)

    def test_force_close_replaces_stale_exit_without_new_stop(self):
        now = dt.datetime(2026, 10, 19, 15, 55, tzinfo=ET)
        self.own()
        stale = trade(REF_EXIT, 7, 9, age_s=120)
        reqs = self.decide(FakeIB(9, [stale], 101.0, now), now)
        self.assertFalse(any(isinstance(r, StopUpdate) for r in reqs))
        self.assertEqual(reqs[0], CancelRequest(7, "AAPL", "force close: replace"))
        self.assertEqual(reqs[-1].quantity, 9)

    def test_force_close_waits_for_fresh_exit(self):
        now = dt.datetime(2026, 10, 19, 15, 52, tzinfo=ET)
        self.own()
        reqs = self.decide(FakeIB(9, [trade(REF_EXIT, 7, 9, age_s=10)], 101.0, now), now)
        self.assertEqual(reqs, [])

    def test_after_close_only_protects(self):
        now = dt.datetime(2026, 10, 19, 17, 0, tzinfo=ET)
        self.own()
        reqs = self.decide(FakeIB(9, [], 101.0, now), now)
        self.assertEqual(reqs, [StopUpdate("AAPL", 9, 95.0, "restore protective stop")])

    def test_after_close_live_day_exit_gets_no_stop_next_to_it(self):
        # the 16:00 race: a DAY exit not yet expired by IBKR covers the shares, so no stop
        # is placed beside it (it could double-sell); bot.await_day_orders_done runs first
        now = dt.datetime(2026, 10, 19, 16, 0, 5, tzinfo=ET)
        self.own()
        reqs = self.decide(FakeIB(9, [trade(REF_EXIT, 7, 9, age_s=300)], 101.0, now), now)
        self.assertFalse(any(isinstance(r, StopUpdate) for r in reqs))


class ForceCloseOcaTest(StrategyTestBase):
    NOW = dt.datetime(2026, 10, 19, 15, 51, 2, tzinfo=ET)

    def test_linked_stop_stays_and_sell_joins_its_oca_group(self):
        self.own()
        ib = FakeIB(9, [trade(REF_STOP, 5, 9, 95.0, oca="paperbot:AAPL:1")], 101.0, self.NOW,
                    quote=(100.50, 100.60))
        reqs = self.decide(ib, self.NOW)
        self.assertFalse(any(isinstance(r, CancelRequest) for r in reqs))  # stop never cancelled
        sell = reqs[-1]
        self.assertEqual((sell.action, sell.quantity, sell.oca_group), ("SELL", 9, "paperbot:AAPL:1"))
        self.assertEqual(sell.limit_price, sizing.to_tick(100.50 * (1 - sizing.LIMIT_SLIPPAGE), "SELL"))  # bid

    def test_stale_exit_replaced_while_linked_stop_stays(self):
        self.own()
        trades = [trade(REF_STOP, 5, 9, 95.0, oca="g1"), trade(REF_EXIT, 7, 9, age_s=90)]
        reqs = self.decide(FakeIB(9, trades, 101.0, self.NOW), self.NOW)
        self.assertEqual([r.order_id for r in reqs if isinstance(r, CancelRequest)], [7])
        self.assertEqual(reqs[-1].oca_group, "g1")

    def test_no_quote_falls_back_to_bar_close_with_alert(self):
        self.own()
        ib = FakeIB(9, [trade(REF_STOP, 5, 9, 95.0, oca="g1")], 101.0, self.NOW, quote=(0.0, 0.0))
        with mock.patch.object(md, "QUOTE_TIMEOUT", 0), \
                self.assertLogs("paperbot", "ERROR") as logs:
            reqs = self.decide(ib, self.NOW)
        self.assertEqual(reqs[-1].limit_price, sizing.to_tick(101.0 * (1 - sizing.LIMIT_SLIPPAGE), "SELL"))
        self.assertTrue(any("no live quote" in line for line in logs.output))


class EntryWindowTest(StrategyTestBase):
    TF = TimeFilter("10:05", "15:30", "15:51")

    def test_window_is_inclusive_to_the_minute(self):
        day = dt.datetime(2026, 10, 19, tzinfo=ET)
        self.assertTrue(self.TF.in_entry_window(day.replace(hour=15, minute=30, second=4)))
        self.assertTrue(self.TF.in_entry_window(day.replace(hour=15, minute=30, second=59)))
        self.assertFalse(self.TF.in_entry_window(day.replace(hour=15, minute=31)))
        self.assertFalse(self.TF.in_entry_window(day.replace(hour=10, minute=4, second=59)))

    def test_entry_cutoff_is_end_of_last_minute(self):
        now = dt.datetime(2026, 10, 19, 11, 0, tzinfo=ET)
        self.assertEqual(self.TF.entry_cutoff(now), now.replace(hour=15, minute=31))

    def test_sweep_cancels_unfilled_entries_after_cutoff(self):
        entry = trade(REF_ENTRY, 3, 1)
        at_cutoff = live_cutoff(dt.datetime(2026, 10, 19, tzinfo=ET))  # live latest_entry_et + 1 min
        reqs = self.decide(FakeIB(0, [entry], 101.0, at_cutoff), at_cutoff)
        self.assertEqual(reqs, [CancelRequest(3, "AAPL", "entry window closed")])
        before = at_cutoff - dt.timedelta(seconds=30)
        self.assertEqual(self.decide(FakeIB(0, [entry], 101.0, before), before), [])


PRICE = partial_price()  # past the live partial-profit target (entry 100, stop 95)


class PartialRetryTest(StrategyTestBase):
    NOW = dt.datetime(2026, 10, 19, 11, 0, tzinfo=ET)

    def send_partial(self, ib):
        reqs = self.decide(ib, self.NOW)
        sell = reqs[-1]
        self.assertEqual(sell.reason, "partial profit")
        strategy.on_sent(sell)
        return sell

    def test_unfilled_partial_resets_and_retries(self):
        self.own()
        sell = self.send_partial(FakeIB(9, [trade(REF_STOP, 5, 9, 95.0)], PRICE, self.NOW))
        bid = round(PRICE - 0.01, 2)  # FakeIB quotes one cent under the bar close
        self.assertEqual(sell.limit_price, sizing.to_tick(bid * (1 - sizing.LIMIT_SLIPPAGE), "SELL"))
        st = strategy.load_state()["positions"]["AAPL"]
        self.assertEqual((st["partial_pending"], st["partial_attempts"], st["partial_done"]),
                         (9, 1, False))
        # the order expired unfilled: still 9 shares, no exit working -> reset, no order this pass
        reqs = self.decide(FakeIB(9, [trade(REF_STOP, 5, 6, 95.0)], PRICE, self.NOW), self.NOW)
        self.assertFalse(any(isinstance(r, OrderIntent) for r in reqs))
        self.assertIn(StopUpdate("AAPL", 9, 95.0, "sync protective stop"), reqs)
        st = strategy.load_state()["positions"]["AAPL"]
        self.assertNotIn("partial_pending", st)
        self.assertFalse(st["partial_done"])
        # next pass: target recalculated and the partial retried
        self.send_partial(FakeIB(9, [trade(REF_STOP, 5, 9, 95.0)], PRICE, self.NOW))
        self.assertEqual(strategy.load_state()["positions"]["AAPL"]["partial_attempts"], 2)

    def test_working_partial_is_left_alone(self):
        self.own()
        self.send_partial(FakeIB(9, [trade(REF_STOP, 5, 9, 95.0)], PRICE, self.NOW))
        working = [trade(REF_STOP, 5, 6, 95.0), trade(REF_EXIT, 7, 3, age_s=10)]
        self.assertEqual(self.decide(FakeIB(9, working, PRICE, self.NOW), self.NOW), [])
        self.assertEqual(strategy.load_state()["positions"]["AAPL"]["partial_pending"], 9)

    def test_filled_partial_is_done(self):
        self.own()
        self.send_partial(FakeIB(9, [trade(REF_STOP, 5, 9, 95.0)], PRICE, self.NOW))
        self.decide(FakeIB(6, [trade(REF_STOP, 5, 6, 95.0)], PRICE, self.NOW), self.NOW)
        self.assertTrue(strategy.load_state()["positions"]["AAPL"]["partial_done"])

    def test_gives_up_after_max_attempts(self):
        self.own(partial_pending=9, partial_attempts=orders.PARTIAL_MAX_ATTEMPTS)
        self.decide(FakeIB(9, [trade(REF_STOP, 5, 9, 95.0)], PRICE, self.NOW), self.NOW)
        self.assertTrue(strategy.load_state()["positions"]["AAPL"]["partial_done"])

    def test_no_quote_defers_partial(self):
        self.own()
        ib = FakeIB(9, [trade(REF_STOP, 5, 9, 95.0)], PRICE, self.NOW, quote=(0.0, 0.0))
        with mock.patch.object(md, "QUOTE_TIMEOUT", 0):
            reqs = self.decide(ib, self.NOW)
        self.assertFalse(any(isinstance(r, OrderIntent) for r in reqs))


class QuoteTest(StrategyTestBase):
    def test_quotes_released_when_no_longer_needed(self):
        now = dt.datetime(2026, 10, 19, 11, 0, tzinfo=ET)
        ib = FakeIB(9, [trade(REF_STOP, 5, 9, 95.0)], PRICE, now)
        self.own()
        self.decide(ib, now)  # partial profit quotes AAPL
        self.assertEqual(ib.subscribed, ["AAPL"])
        flat = FakeIB(0, [], PRICE, now)
        self.decide(flat, now)
        self.assertEqual(flat.cancelled, ["AAPL"])

    def test_halted_or_crossed_quote_is_rejected(self):
        now = dt.datetime(2026, 10, 19, 11, 0, tzinfo=ET)
        with mock.patch.object(md, "QUOTE_TIMEOUT", 0):
            for quote in ((101.0, 100.0), (float("nan"), 100.0)):
                md._tickers.clear()
                self.assertIsNone(md.quote(FakeIB(0, [], 1.0, now, quote=quote), "AAPL"))
            ib = FakeIB(0, [], 1.0, now)
            ib.reqMktData = lambda c, *a, **k: NS(contract=c, bid=100.0, ask=100.1, halted=1.0)
            md._tickers.clear()
            self.assertIsNone(md.quote(ib, "AAPL"))


class SizingTest(StrategyTestBase):
    SIGNAL = {"price": 100.0, "stop": 90.0, "gap": 4.0, "rvol": 3.0}
    BIG = {"max_quantity_per_order": 10_000, "max_order_value": 1e9}

    def setUp(self):
        super().setUp()
        base = strategy.load_profile()  # what-if caps below start from the real profile
        self.assertEqual(base.key, "trend_join_long")
        self.assertEqual(base.limits, profiles.load("trend_join_long").limits)

    def size(self, buying_power=1e9, rules_extra=None, caps=None, good_till=None):
        profile = strategy.with_limits(strategy.load_profile(), **(caps or self.BIG),
                                       **(rules_extra or {}))
        return strategy._size_entry("AAPL", self.SIGNAL, profile, 1_000_000.0, buying_power,
                                    100.0, good_till)

    def test_priced_from_ask(self):
        self.assertEqual(self.size().limit_price, sizing.to_tick(100.0 * (1 + sizing.LIMIT_SLIPPAGE), "BUY"))

    def test_buying_power_caps_quantity(self):
        self.assertEqual(self.size(buying_power=5_010.0).quantity, 50)
        self.assertIsNone(self.size(buying_power=50.0))

    def test_commission_drag_blocks_one_share_trade(self):
        # 1 share: R about $10, round-trip commission $2 = 20% of R > 1%
        one = {"max_quantity_per_order": 1, "max_order_value": 1e9}
        self.assertIsNone(self.size(caps=one))
        self.assertIsNotNone(self.size(caps=one, rules_extra={"max_commission_pct_of_risk": None}))

    def test_large_share_cap_never_bypasses_other_limits(self):
        # a generous max_quantity_per_order is only a ceiling: value, risk and size still bind
        profile = strategy.with_limits(strategy.load_profile(), max_quantity_per_order=35,
                                       max_order_value=2500.0)
        by_value = strategy._size_entry("AAPL", self.SIGNAL, profile, 1_000_000.0, 1e9, 100.0)
        by_risk = strategy._size_entry("AAPL", self.SIGNAL, profile, 21_000.0, 1e9, 100.0)
        self.assertEqual(by_value.quantity, 24)  # floor(2500 / 100.20), not 35
        self.assertLessEqual(by_value.quantity * by_value.limit_price, 2500.0)
        lim = profile.limits
        risk_pct, size_pct = lim.max_risk_per_trade_pct / 100, lim.max_position_size_pct_of_portfolio / 100
        expected = min(int(21_000.0 * size_pct / by_risk.limit_price),
                       int(21_000.0 * risk_pct / (by_risk.limit_price - 90.0)))
        self.assertEqual(by_risk.quantity, expected)  # position % and risk % of 21,000, not 35
        self.assertLess(by_risk.quantity, 35)
        self.assertLessEqual(by_risk.quantity * (by_risk.limit_price - 90.0), 21_000.0 * risk_pct)
        self.assertLessEqual(by_risk.quantity * by_risk.limit_price, 21_000.0 * size_pct)

    def test_entry_is_gtd_at_cutoff(self):
        cutoff = dt.datetime(2026, 10, 19, 15, 31, tzinfo=ET)
        self.assertEqual(self.size(good_till=cutoff).good_till, cutoff)


MONDAY_11 = dt.datetime(2026, 10, 19, 11, 0, tzinfo=ET)


class FakeMarket(FakeIB):
    """Market data for one symbol that passes every Trend Join Long filter:
    rising daily trend, 4% gap, new highs above premarket and prior day, RVOL 3."""

    def __init__(self, now=MONDAY_11, shares=0, gap_open=96.0):
        super().__init__(shares, [], 0.0, now, quote=(97.69, 97.71))
        today = now.date()
        self.daily = [NS(date=today - dt.timedelta(days=210 - i), close=50 + i * 0.2,
                         high=51 + i * 0.2, low=49 + i * 0.2, open=50 + i * 0.2)
                      for i in range(210)]  # prior close 91.8, prior high 92.8
        day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        pre = [NS(date=(day + dt.timedelta(hours=8, minutes=5 * i)).astimezone(UTC),
                  open=95, high=95, low=94, close=95, volume=500) for i in range(12)]
        rth = [NS(date=(day + dt.timedelta(hours=9, minutes=30 + 5 * i)).astimezone(UTC),
                  open=gap_open if i == 0 else 96 + 0.1 * i, high=96 + 0.1 * i,
                  low=95.5 + 0.1 * i, close=96 + 0.1 * i, volume=3000) for i in range(18)]
        self.today = pre + rth
        self.history = [NS(date=(day - dt.timedelta(days=d, hours=-9, minutes=-30 - 5 * i))
                           .astimezone(UTC), volume=1000)
                        for d in range(1, 15) for i in range(78)]

    def reqHistoricalData(self, contract, end, duration, size, *args, **kwargs):
        if size == "1 day":
            return self.daily
        return self.today if duration == "1 D" else self.history


class EvaluateTest(StrategyTestBase):
    def evaluate(self, ib, now=MONDAY_11):
        return strategy.evaluate(ib, "TEST", strategy.load_profile(), now)

    def test_all_filters_pass_with_reasons(self):
        result = self.evaluate(FakeMarket())
        self.assertTrue(result["pass"], result["reasons"])
        self.assertIs(type(result["pass"]), bool)
        self.assertEqual([r.split()[0] for r in result["reasons"]],
                         ["D2", "price", "D3", "D1", "I1", "I2", "I3"])
        self.assertAlmostEqual(result["price"], 97.7)
        pct = strategy.load_profile().stop.pct  # low of day minus the live stop %
        self.assertEqual(result["stop"], sizing.to_tick(95.5 * (1 - pct / 100), "STOP"))

    def test_first_failure_is_reported(self):
        result = self.evaluate(FakeMarket(gap_open=92.0))  # gap 0.2% < 3%
        self.assertFalse(result["pass"])
        self.assertTrue(result["reasons"][-1].startswith("FAIL D3 gap"))

    def test_every_result_is_logged(self):
        self.evaluate(FakeMarket())
        self.evaluate(FakeMarket(gap_open=92.0))
        lines = [json.loads(l) for l in self.safety_log.read_text().splitlines()]
        self.assertEqual([l["pass"] for l in lines], [True, False])

    def check(self, ib, now=MONDAY_11):
        with mock.patch.object(strategy, "_now", lambda: now):
            return strategy.check_symbol(ib, "DU1", "aapl")

    def test_check_only_blocks_existing_position(self):
        result = self.check(FakeMarket(shares=5))
        self.assertEqual(result["reasons"], ["FAIL already in position (5 shares)"])

    def test_check_only_blocks_outside_window(self):
        earliest = profiles.hhmm(strategy.load_profile().time_filter.earliest_entry_et)
        before = (dt.datetime.combine(MONDAY_11.date(), earliest, ET) - dt.timedelta(minutes=1))
        self.assertGreaterEqual(before.time(), dt.time(9, 30))  # market open, window not yet
        result = self.check(FakeMarket(), now=before)
        self.assertTrue(result["reasons"][0].startswith("FAIL outside entry window"))
        self.assertFalse(result["pass"])

    def test_check_only_pass_shows_gates_first(self):
        result = self.check(FakeMarket())
        self.assertTrue(result["pass"])
        self.assertEqual(result["reasons"][0], "no existing position")
        self.assertTrue(result["reasons"][1].startswith("time gate ok"))


if __name__ == "__main__":
    unittest.main()
