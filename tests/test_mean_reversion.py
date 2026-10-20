"""Offline tests for the Equities Mean Reversion profile (Phase 4).

Covers: session VWAP and its deviation, the below_vwap_sigma entry and the
sigma_below_entry stop on 1-minute bars, the strategy's 5% fee limit, up to 3
entries per symbol per day (Trend Join Long keeps 1), the target_vwap and
30-minute time_stop exits in the stop's OCA group, the 15:50 force close and
the refusal of scheduled (5-minute) runs.
Run: python -m unittest discover -s tests -p test_mean_reversion.py -v
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

from paperbot.core import market_data as md
from paperbot.core import orders
from paperbot.core import profile as profiles
from paperbot.core.common import ET
from paperbot.core.risk import OrderIntent
from paperbot.indicators import vwap_bands
from paperbot.runtime.runstate import write_json_atomic
from paperbot.strategies import rule_strategy as strategy

UTC = dt.timezone.utc
TUESDAY = dt.date(2026, 10, 20)
REFS = profiles.load("equity_mean_reversion").refs


def at(hour, minute, second=0):
    return dt.datetime(2026, 10, 20, hour, minute, second, tzinfo=ET)


def minute_bars(last_closes: list[float], start=at(9, 30), count=90) -> list:
    """`count` 1-minute bars from 09:30 oscillating around 100, ending with last_closes."""
    closes = [100.1 if i % 2 else 99.9 for i in range(count - len(last_closes))] + last_closes
    return [NS(date=(start + dt.timedelta(minutes=i)).astimezone(UTC), open=c, high=c + 0.05,
               low=c - 0.05, close=c, volume=1000) for i, c in enumerate(closes)]


def trade(ref, order_id, qty, aux=0.0, age_s=0.0, oca="", symbol="SPY"):
    sent = dt.datetime.now(UTC) - dt.timedelta(seconds=age_s)
    return NS(order=NS(orderRef=ref, orderId=order_id, totalQuantity=qty, auxPrice=aux,
                       ocaGroup=oca),
              contract=NS(symbol=symbol), orderStatus=NS(filled=0),
              log=[NS(time=sent)], isDone=lambda: False)


STRETCH = [99.6, 99.4, 99.2, 99.1, 99.0]  # a sharp drop below VWAP


class MinuteIB:
    def __init__(self, last=STRETCH, quote=(98.98, 99.02), held=None, trades=()):
        self.bars, self.quote, self._trades = minute_bars(last), quote, list(trades)
        self._positions = [NS(contract=NS(symbol=s, secType="STK"), position=float(q), avgCost=c)
                           for s, (q, c) in (held or {}).items()]

    def qualifyContracts(self, contract):
        contract.conId = 1

    def reqHistoricalData(self, contract, end, duration, size, *args, **kwargs):
        return self.bars if size == "1 min" else []

    def reqMktData(self, contract, *args, **kwargs):
        bid, ask = self.quote
        return NS(contract=contract, bid=bid, ask=ask, halted=0.0)

    def cancelMktData(self, contract):
        pass

    def sleep(self, seconds):
        pass

    def positions(self, account=None):
        return self._positions

    def openTrades(self):
        return self._trades

    def accountValues(self, account=None):
        return [NS(tag="NetLiquidation", currency="USD", value="1000000"),
                NS(tag="BuyingPower", currency="USD", value="4000000")]


class VwapTest(unittest.TestCase):
    def test_volume_weighted_mean_and_deviation(self):
        bars = [NS(high=11, low=9, close=10, volume=100), NS(high=13, low=11, close=12, volume=300)]
        vwap, sd = vwap_bands(bars)
        self.assertAlmostEqual(vwap, (10 * 100 + 12 * 300) / 400)  # 11.5
        self.assertAlmostEqual(sd, ((100 * 1.5 ** 2 + 300 * 0.5 ** 2) / 400) ** 0.5)
        self.assertIsNone(vwap_bands([]))
        self.assertIsNone(vwap_bands([NS(high=1, low=1, close=1, volume=0)]))


class MeanReversionBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state.json"
        self.patches = [mock.patch.object(strategy, "STATE", self.state),
                        mock.patch.object(strategy, "SAFETY_LOG", Path(self.tmp.name) / "s.jsonl"),
                        mock.patch.object(strategy, "ACTIVE", "equity_mean_reversion"),
                        mock.patch.object(strategy, "_profile", None),
                        mock.patch.object(strategy, "_universe", lambda p, today: ["SPY"])]
        for p in self.patches:
            p.start()
        orders._orphans.clear()
        md.clear_caches()
        strategy._scan_bar, strategy._scan_pos, strategy._scan_list = None, 0, []

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def write_state(self, positions=None, counts=None):
        today = TUESDAY.isoformat()
        write_json_atomic(self.state, {
            "positions": positions or {},
            "entered": {s: today for s in (counts or {})},
            "entry_counts": {s: [today, n] for s, n in (counts or {}).items()}})

    def decide(self, ib, now):
        with mock.patch.object(strategy, "_now", lambda: now):
            return strategy.decide(ib, "DU1")


class EntryTest(MeanReversionBase):
    def test_stretch_below_vwap_passes_with_sigma_stop(self):
        result = strategy.evaluate(MinuteIB(), "SPY", strategy.load_profile(), at(11, 0))
        self.assertTrue(result["pass"], result["reasons"])
        self.assertTrue(result["reasons"][0].startswith("V1 price 99.00 <= VWAP "))
        vwap, sd = vwap_bands(minute_bars(STRETCH))
        self.assertAlmostEqual(result["vwap"], vwap, places=3)
        self.assertLessEqual(result["stop"], 99.0 - sd + 1e-9)
        self.assertGreater(result["stop"], 99.0 - sd - 0.011)

    def test_price_near_vwap_fails(self):
        result = strategy.evaluate(MinuteIB(last=[100.0]), "SPY", strategy.load_profile(), at(11, 0))
        self.assertTrue(result["reasons"][-1].startswith("FAIL V1 price 100.00 above VWAP"))

    def test_only_completed_minutes_count(self):
        # at 10:59:30 the 10:59 bar (the last 99.0) is not complete yet
        result = strategy.evaluate(MinuteIB(), "SPY", strategy.load_profile(), at(10, 59, 30))
        self.assertAlmostEqual(result["price"], 99.1)

    def test_five_percent_fee_limit_lets_the_scalp_size(self):
        buy = self.decide(MinuteIB(), at(11, 0))[0]
        self.assertEqual((buy.action, buy.symbol), ("BUY", "SPY"))
        self.assertEqual(buy.good_till, at(15, 31))
        strict = strategy.with_limits(strategy.load_profile(), max_commission_pct_of_risk=1.0)
        signal = {"price": 99.0, "stop": buy.stop_price, "gap": None, "rvol": None}
        with self.assertLogs("paperbot", "INFO"):
            self.assertIsNone(strategy._size_entry("SPY", signal, strict, 1e6, 4e6, 99.02))

    def test_up_to_three_entries_per_symbol_per_day(self):
        self.write_state(counts={"SPY": 2})
        self.assertEqual(len(self.decide(MinuteIB(), at(11, 0))), 1)
        strategy._scan_bar = None
        self.write_state(counts={"SPY": 3})
        self.assertEqual(self.decide(MinuteIB(), at(11, 1)), [])

    def test_entry_count_is_recorded(self):
        for _ in range(2):
            strategy.on_sent(OrderIntent("SPY", "BUY", 10, 99.2, stop_price=98.8))
        state = strategy.load_state()
        self.assertEqual(state["entry_counts"]["SPY"][1], 2)
        self.assertEqual(state["entered"]["SPY"], dt.datetime.now(ET).date().isoformat())

    def test_trend_join_long_keeps_one_entry_per_day(self):
        tjl = profiles.load("trend_join_long")
        self.assertEqual(tjl.max_entries_per_symbol_per_day, 1)
        state = {"entered": {"NVDA": "2026-10-20"}, "entry_counts": {}}
        self.assertEqual(strategy.entries_today(state, "NVDA", "2026-10-20"), 1)  # old states count
        self.assertEqual(strategy.entries_today(state, "NVDA", "2026-10-21"), 0)


class ExitTest(MeanReversionBase):
    HELD = {"SPY": (100, 99.0)}

    def position(self, opened: dt.datetime):
        return {"SPY": {"initial_stop": 98.8, "stop": 98.8, "breakeven": False,
                        "partial_done": False, "opened_at": opened.isoformat()}}

    def ib(self, quote, last=STRETCH):
        return MinuteIB(last=last, quote=quote, held=self.HELD,
                        trades=[trade(REFS[1], 5, 100, 98.8, oca="g1")])

    def test_back_at_vwap_exits_in_the_oca_group(self):
        self.write_state(self.position(at(10, 55)), {"SPY": 1})
        reqs = self.decide(self.ib((99.98, 100.02)), at(11, 0))
        sell = reqs[-1]
        self.assertEqual((sell.action, sell.quantity, sell.oca_group), ("SELL", 100, "g1"))
        self.assertIn("back at VWAP", sell.reason)

    def test_thirty_minute_time_stop(self):
        self.write_state(self.position(at(10, 45)), {"SPY": 1})
        self.assertEqual(self.decide(self.ib((98.98, 99.02)), at(11, 0)), [])  # 15 min, below VWAP
        md.clear_caches()
        reqs = self.decide(self.ib((98.98, 99.02)), at(11, 16))
        self.assertIn("held 31 min (time stop 30)", reqs[-1].reason)

    def test_force_close_at_1550(self):
        self.write_state(self.position(at(15, 45)), {"SPY": 1})
        reqs = self.decide(self.ib((98.98, 99.02)), at(15, 50, 5))
        self.assertTrue(reqs[-1].reason.startswith("force close (attempt 1)"))
        self.assertEqual(reqs[-1].oca_group, "g1")


class ContinuousOnlyTest(unittest.TestCase):
    def test_scheduled_runs_are_refused(self):
        from paperbot.core import scheduled_pass
        with mock.patch("paperbot.core.engine.setup_logging"), \
                mock.patch("paperbot.runtime.runstate.stop_requested", return_value=False), \
                mock.patch("paperbot.runtime.runstate.running_pid", return_value=None), \
                mock.patch("paperbot.runtime.runstate.claim_pid") as claim, \
                mock.patch("paperbot.core.selector.unattended_key",
                           return_value="equity_mean_reversion"), \
                self.assertLogs("paperbot", "ERROR") as logs, \
                self.assertRaises(SystemExit):
            scheduled_pass.run("run", True)
        claim.assert_not_called()  # refused before anything is claimed or connected
        self.assertIn("continuous mode only", logs.output[-1])

    def test_profile(self):
        p = profiles.load("equity_mean_reversion")
        self.assertEqual((p.modes, p.timeframe, p.max_entries_per_symbol_per_day),
                         (("continuous",), "1m", 3))
        self.assertEqual(p.limits.max_commission_pct_of_risk, 5.0)
        self.assertEqual(p.scan_every, dt.timedelta(minutes=1))


if __name__ == "__main__":
    unittest.main()
