"""Offline tests for strategy.py, using a fake IB object.

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

from paperbot.trading import strategy
from paperbot.trading.risk import REF_EXIT, REF_STOP, CancelRequest, OrderIntent, StopUpdate
from paperbot.runtime.runstate import write_json_atomic

ET = strategy.ET
UTC = dt.timezone.utc


def bar(start: dt.datetime, close: float, low: float | None = None):
    return NS(date=start.astimezone(UTC), open=close, high=close, low=low or close,
              close=close, volume=1000)


def trade(ref: str, order_id: int, qty: int, aux: float = 0.0, age_s: float = 0.0):
    sent = dt.datetime.now(UTC) - dt.timedelta(seconds=age_s)
    return NS(order=NS(orderRef=ref, orderId=order_id, totalQuantity=qty, auxPrice=aux),
              contract=NS(symbol="AAPL"), orderStatus=NS(filled=0),
              log=[NS(time=sent)], isDone=lambda: False)


class FakeIB:
    def __init__(self, shares: int, trades: list, close: float, now: dt.datetime):
        self._positions = ([NS(contract=NS(symbol="AAPL", secType="STK"), position=float(shares),
                               avgCost=100.0)] if shares else [])
        self._trades = trades
        start = now.replace(hour=9, minute=30, second=0, microsecond=0)
        self._bars = [bar(start + i * strategy.BAR, close) for i in range(6)]

    def positions(self, account=None):
        return self._positions

    def openTrades(self):
        return self._trades

    def qualifyContracts(self, contract):
        contract.conId = 1

    def reqHistoricalData(self, *args, **kwargs):
        return self._bars

    def accountValues(self, account=None):
        return []


class StrategyTestBase(unittest.TestCase):
    """Temp state and safety-log files, cleared caches, and decide() at a fixed time."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tmp.name) / "strategy_state.json"
        self.safety_log = Path(self.tmp.name) / "safety_log.jsonl"
        self.patches = [mock.patch.object(strategy, "STATE", self.state_path),
                        mock.patch.object(strategy, "SAFETY_LOG", self.safety_log),
                        mock.patch.object(strategy, "_universe", lambda rules, today: [])]
        for p in self.patches:
            p.start()
        strategy._orphans.clear()
        strategy._daily_cache.clear()
        strategy._rvol_cache.clear()

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
        # entry 100, stop 95 -> R = 5; 0.75R target = 103.75
        reqs = self.decide(FakeIB(9, [trade(REF_STOP, 5, 9, 95.0)], 104.0, now), now)
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


MONDAY_11 = dt.datetime(2026, 10, 19, 11, 0, tzinfo=ET)


class FakeMarket(FakeIB):
    """Market data for one symbol that passes every Trend Join Long filter:
    rising daily trend, 4% gap, new highs above premarket and prior day, RVOL 3."""

    def __init__(self, now=MONDAY_11, shares=0, gap_open=96.0):
        super().__init__(shares, [], 0.0, now)
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
        return strategy.evaluate(ib, "TEST", strategy.load_rules(), now)

    def test_all_filters_pass_with_reasons(self):
        result = self.evaluate(FakeMarket())
        self.assertTrue(result["pass"], result["reasons"])
        self.assertIs(type(result["pass"]), bool)
        self.assertEqual([r.split()[0] for r in result["reasons"]],
                         ["D2", "price", "D3", "D1", "I1", "I2", "I3"])
        self.assertAlmostEqual(result["price"], 97.7)
        self.assertEqual(result["stop"], round(95.5 * 0.99, 2))

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
        result = self.check(FakeMarket(), now=MONDAY_11.replace(hour=9, minute=50))
        self.assertTrue(result["reasons"][0].startswith("FAIL outside entry window"))
        self.assertFalse(result["pass"])

    def test_check_only_pass_shows_gates_first(self):
        result = self.check(FakeMarket())
        self.assertTrue(result["pass"])
        self.assertEqual(result["reasons"][0], "no existing position")
        self.assertTrue(result["reasons"][1].startswith("time gate ok"))


if __name__ == "__main__":
    unittest.main()
