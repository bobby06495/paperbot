"""Offline tests for multi-day strategies and the Macro Swing ETF profile (Phase 3).

Covers: RSI/ATR indicators, the 1d timeframe (live price as today's close),
multi-day holding (no force close, held overnight on the GTC stop), signal
exits (close_above_sma, time_stop) closing in the stop's OCA group, the entry
window, and the scheduler/dashboard handling of a strategy without force close.
Run: python -m unittest discover -s tests -p test_macro_swing.py -v
"""
import os

os.environ["PAPERBOT_NOTIFY"] = "off"  # tests must never send real notifications

import copy
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

from paperbot import paths
from paperbot.core import market_data as md
from paperbot.core import orders
from paperbot.core import profile as profiles
from paperbot.core import scheduled_pass
from paperbot.core.common import ET
from paperbot.core.risk import REF_STOP, CancelRequest, ConfigError, OrderIntent, StopUpdate
from paperbot.indicators import atr, rsi
from paperbot.reporting import telemetry as tm
from paperbot.runtime.runstate import write_json_atomic
from paperbot.strategies import rule_strategy as strategy

UTC = dt.timezone.utc
TUESDAY = dt.date(2026, 10, 20)
SWING_REFS = profiles.load("macro_swing_etf").refs
LIVE_RULES = json.loads(paths.RULES_FILE.read_text())
SWING = LIVE_RULES["strategies"]["macro_swing_etf"]


def at(hour, minute, day=TUESDAY):
    return dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=ET)


def daily_series(closes: list[float], end: dt.date) -> list:
    """Daily bars ending the trading day before `end` (weekdays only)."""
    days, d = [], end - dt.timedelta(days=1)
    while len(days) < len(closes):
        if d.weekday() < 5:
            days.append(d)
        d -= dt.timedelta(days=1)
    days.reverse()
    return [NS(date=day, open=c, high=c + 1.0, low=c - 1.0, close=c, volume=1_000_000)
            for day, c in zip(days, closes)]


def pullback_closes() -> list[float]:
    """A long uptrend (far above its SMA200) with a 3-day pullback at the end."""
    return [100 + 0.5 * i for i in range(217)] + [205.0, 202.0, 199.0]


def trade(ref, order_id, qty, aux=0.0, age_s=0.0, oca="", symbol="XLK"):
    sent = dt.datetime.now(UTC) - dt.timedelta(seconds=age_s)
    return NS(order=NS(orderRef=ref, orderId=order_id, totalQuantity=qty, auxPrice=aux,
                       ocaGroup=oca),
              contract=NS(symbol=symbol), orderStatus=NS(filled=0),
              log=[NS(time=sent)], isDone=lambda: False)


class SwingIB:
    def __init__(self, closes=None, quote=(197.95, 198.05), held=None, trades=(), today=TUESDAY):
        self.daily = daily_series(closes or pullback_closes(), today)
        self.quote, self._trades = quote, list(trades)
        self._positions = [NS(contract=NS(symbol=s, secType="STK"), position=float(q), avgCost=c)
                           for s, (q, c) in (held or {}).items()]
        self.requests = []

    def qualifyContracts(self, contract):
        contract.conId = 1

    def reqHistoricalData(self, contract, end, duration, size, *args, **kwargs):
        self.requests.append(size)
        return self.daily if size == "1 day" else []

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


class IndicatorTest(unittest.TestCase):
    def test_rsi_extremes_and_wilder_value(self):
        self.assertEqual(rsi([1, 2, 3, 4, 5], 2), 100.0)
        self.assertEqual(rsi([5, 4, 3, 2, 1], 2), 0.0)
        self.assertEqual(rsi([1, 1, 1], 2), 50.0)
        self.assertIsNone(rsi([1, 2], 2))
        # changes +1, -1, +2: seed avg gain 0.5, loss 0.5; then gain (0.5+2)/2=1.25, loss 0.25
        self.assertAlmostEqual(rsi([10, 11, 10, 12], 2), 100 - 100 / (1 + 1.25 / 0.25))

    def test_atr_wilder(self):
        bars = [NS(high=c + 1, low=c - 1, close=c) for c in (10, 10, 10, 10)]
        self.assertEqual(atr(bars, 2), 2.0)
        gap = bars + [NS(high=16, low=14, close=15)]  # true range from the prior close: 6
        self.assertAlmostEqual(atr(gap, 2), (2.0 + 6.0) / 2)
        self.assertIsNone(atr(bars[:2], 2))


class SwingProfileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "rules.json"
        self.config = {"limits": {"allowed_symbols": ["*"], "limit_orders_only": True}}

    def tearDown(self):
        self.tmp.cleanup()

    def load(self, section):
        self.path.write_text(json.dumps({"strategies": {"s": section}}))
        return profiles.load("s", self.path, self.config)

    def fails(self, change, text):
        with self.assertRaises(ConfigError) as caught:
            self.load({**copy.deepcopy(SWING), **change})
        self.assertIn(text, str(caught.exception))

    def test_live_profile(self):
        p = profiles.load("macro_swing_etf")
        self.assertEqual((p.timeframe, p.holding, p.time_filter.force_close_time()), ("1d", "multi_day", None))
        self.assertEqual(p.refs[1], "paperbot:macro_swing_etf:stop")
        self.assertEqual(p.scan_every, dt.timedelta(minutes=5))
        self.assertEqual([b.type for b in p.signal_exits], ["close_above_sma", "time_stop"])
        self.assertIn("holds overnight", p.menu_line())

    def test_multi_day_rules(self):
        self.fails({"exit": [{"type": "force_close"}]}, "multi_day strategy holds overnight")
        self.fails({"time_filter": {**SWING["time_filter"], "force_close_et": "15:51"}},
                   "multi_day strategy holds overnight")
        self.fails({"holding": "intraday"}, "needs a force_close exit")
        self.fails({"entry": [{"type": "min_rvol", "min": 2.0}]},
                   "strategies.s.entry[0]: min_rvol needs intraday bars; not available on 1d")
        self.fails({"stop": {"type": "low_of_day_minus_pct", "pct": 1.0}}, "needs intraday bars")

    def test_exit_block_settings(self):
        self.fails({"exit": [{"type": "time_stop"}]}, "time_stop needs exactly one of days or minutes")
        self.fails({"exit": [{"type": "time_stop", "days": 5, "minutes": 30}]}, "exactly one")
        self.fails({"exit": [{"type": "close_above_sma", "period": 5, "at_et": "3pm"}]},
                   'at_et must be "HH:MM"')


class SwingBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state.json"
        self.patches = [mock.patch.object(strategy, "STATE", self.state),
                        mock.patch.object(strategy, "SAFETY_LOG", Path(self.tmp.name) / "s.jsonl"),
                        mock.patch.object(strategy, "ACTIVE", "macro_swing_etf"),
                        mock.patch.object(strategy, "_profile", None),
                        mock.patch.object(strategy, "_universe", lambda p, today: ["XLK"])]
        for p in self.patches:
            p.start()
        orders._orphans.clear()
        md.clear_caches()
        strategy._scan_bar, strategy._scan_pos, strategy._scan_list = None, 0, []

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def own(self, opened: dt.date, **extra):
        pos = {"initial_stop": 190.0, "stop": 190.0, "breakeven": False, "partial_done": False,
               "opened_at": dt.datetime.combine(opened, dt.time(15, 47), ET).isoformat(), **extra}
        write_json_atomic(self.state, {"positions": {"XLK": pos}, "entered": {"XLK": opened.isoformat()}})

    def decide(self, ib, now):
        with mock.patch.object(strategy, "_now", lambda: now):
            return strategy.decide(ib, "DU1")


class SwingEntryTest(SwingBase):
    def test_daily_signal_on_live_price(self):
        result = strategy.evaluate(SwingIB(), "XLK", strategy.load_profile(), at(15, 47))
        self.assertTrue(result["pass"], result["reasons"])
        self.assertEqual(result["price"], 198.0)  # live mid as today's close
        self.assertTrue(result["reasons"][0].startswith("T1 price 198.00 > SMA200"))
        self.assertTrue(result["reasons"][1].startswith("P1 RSI(2) "))
        value = atr(SwingIB().daily, 14)
        self.assertAlmostEqual(result["stop"], 198.0 - 2 * value, delta=0.011)
        self.assertLess(result["stop"], 198.0 - 2 * value + 1e-9)  # rounded down

    def test_failures(self):
        uptrend_no_pullback = [100 + 0.5 * i for i in range(220)]
        result = strategy.evaluate(SwingIB(closes=uptrend_no_pullback, quote=(209.9, 210.1)), "XLK",
                                   strategy.load_profile(), at(15, 47))
        self.assertTrue(result["reasons"][-1].startswith("FAIL P1 RSI(2) "))
        md.clear_caches()  # a different fake market for the same symbol
        downtrend = [300 - 0.5 * i for i in range(220)]
        result = strategy.evaluate(SwingIB(closes=downtrend, quote=(189.0, 189.1)), "XLK",
                                   strategy.load_profile(), at(15, 47))
        self.assertTrue(result["reasons"][-1].startswith("FAIL T1 price"))
        md.clear_caches()
        with mock.patch.object(md, "QUOTE_TIMEOUT", 0):
            result = strategy.evaluate(SwingIB(quote=(0.0, 0.0)), "XLK", strategy.load_profile(),
                                       at(15, 47))
        self.assertEqual(result["reasons"], ["FAIL no live quote for today's price"])

    def test_buys_only_in_the_closing_window_with_gtd(self):
        self.assertEqual(self.decide(SwingIB(), at(15, 40)), [])
        reqs = self.decide(SwingIB(), at(15, 47))
        self.assertEqual(len(reqs), 1)
        buy = reqs[0]
        self.assertEqual((buy.action, buy.symbol, buy.good_till), ("BUY", "XLK", at(15, 56)))
        self.assertLess(buy.stop_price, buy.limit_price)
        self.assertTrue(buy.reason.startswith("macro swing: RSI(2) pullback"))

    def test_no_intraday_bars_are_requested(self):
        ib = SwingIB()
        self.decide(ib, at(15, 47))
        self.assertTrue(ib.requests)
        self.assertEqual(set(ib.requests), {"1 day"})

    def test_on_sent_records_open_time(self):
        strategy.on_sent(OrderIntent("XLK", "BUY", 10, 198.5, stop_price=192.0))
        self.assertIn("opened_at", strategy.load_state()["positions"]["XLK"])


class SwingExitTest(SwingBase):
    HELD = {"XLK": (100, 200.0)}

    def ib(self, quote, trades=None, closes=None):
        trades = [trade(SWING_REFS[1], 5, 100, 190.0, oca="g1")] if trades is None else trades
        return SwingIB(closes=closes, quote=quote, held=self.HELD, trades=trades)

    def test_close_above_sma_exit_at_1550_in_the_oca_group(self):
        self.own(TUESDAY - dt.timedelta(days=1))
        rally = (209.9, 210.1)
        self.assertEqual(self.decide(self.ib(rally), at(15, 45)), [])  # before at_et
        reqs = self.decide(self.ib(rally), at(15, 50))
        sell = reqs[-1]
        self.assertEqual((sell.action, sell.quantity, sell.closing, sell.oca_group),
                         ("SELL", 100, True, "g1"))
        self.assertTrue(sell.reason.startswith("exit (attempt 1): price 210.00 above SMA5"))
        self.assertFalse(any(isinstance(r, (CancelRequest, StopUpdate)) for r in reqs))
        self.assertIn("exit_reason", strategy.load_state()["positions"]["XLK"])

    def test_no_exit_on_the_entry_day(self):
        self.own(TUESDAY)
        self.assertEqual(self.decide(self.ib((209.9, 210.1)), at(15, 50)), [])

    def test_time_stop_counts_trading_days(self):
        self.own(TUESDAY - dt.timedelta(days=14))  # 10 trading days ago
        reqs = self.decide(self.ib((199.9, 200.1)), at(15, 50))
        self.assertTrue(reqs[-1].reason.startswith("exit (attempt 1): held 10 trading days"))
        self.own(TUESDAY - dt.timedelta(days=13))  # 9 trading days
        self.assertEqual(self.decide(self.ib((199.9, 200.1)), at(15, 50)), [])

    def test_decided_exit_is_kept_and_repriced(self):
        self.own(TUESDAY - dt.timedelta(days=2), exit_reason="time stop", exit_attempts=1)
        fresh = [trade(SWING_REFS[1], 5, 100, 190.0, oca="g1"),
                 trade(SWING_REFS[2], 7, 100, age_s=10)]
        self.assertEqual(self.decide(self.ib((199.9, 200.1), fresh), at(10, 0)), [])
        stale = [fresh[0], trade(SWING_REFS[2], 7, 100, age_s=90)]
        reqs = self.decide(self.ib((199.9, 200.1), stale), at(10, 1))
        self.assertEqual(reqs[0], CancelRequest(7, "XLK", "exit: replace"))
        self.assertEqual(reqs[-1].reason, "exit (attempt 2): time stop")

    def test_held_overnight_without_force_close(self):
        self.own(TUESDAY - dt.timedelta(days=1))
        stop = [trade(SWING_REFS[1], 5, 100, 190.0, oca="g1")]
        self.assertEqual(self.decide(self.ib((199.9, 200.1), stop), at(15, 55)), [])  # no force close
        with self.assertLogs("paperbot", "ERROR") as logs:  # only the missing-stop ALERT
            reqs = self.decide(self.ib((199.9, 200.1), []), at(17, 0))
        self.assertEqual(reqs, [StopUpdate("XLK", 100, 190.0, "restore protective stop")])
        self.assertFalse(any("still open after the close" in line for line in logs.output))


class NoForceCloseRuntimeTest(unittest.TestCase):
    def test_scheduled_pass_has_no_force_close_phase(self):
        self.assertEqual(scheduled_pass.time_gate(at(15, 55), None), "run")
        self.assertEqual(scheduled_pass.time_gate(at(16, 2), None), "post_close")
        self.assertEqual(scheduled_pass.seconds_to_force_close(at(15, 50), None), 0.0)
        with mock.patch.object(scheduled_pass, "_active_section", return_value=SWING):
            self.assertIsNone(scheduled_pass.force_close_at())

    def test_dashboard_phases(self):
        view = tm.rules_view("macro_swing_etf", SWING)
        times = tm.session_times(view)
        self.assertEqual(times["force_close"], dt.time(16, 0))
        self.assertEqual(tm.phase(at(15, 55), times), "entry_window")  # 15:45-15:55 inclusive
        self.assertEqual(tm.phase(at(15, 57), times), "manage_only")  # not force_close
        self.assertEqual([b["type"] for b in view["blocks"]["exit"]], ["close_above_sma", "time_stop"])
        self.assertEqual(view["universe_symbols"][0], "XLK")


class CommissionEdgeTest(unittest.TestCase):
    def test_cheap_etf_with_tight_stop_is_skipped_by_the_fee_rule(self):
        p = profiles.load("macro_swing_etf")
        tight = {"price": 42.0, "stop": round(42.0 * 0.98, 2), "gap": None, "rvol": None}
        wide = {**tight, "stop": round(42.0 * 0.97, 2)}
        with self.assertLogs("paperbot", "INFO") as logs:
            self.assertIsNone(strategy._size_entry("XLRE", tight, p, 781_120.0, 4e6, 42.0))
        self.assertIn("commission", logs.output[-1])
        self.assertIsNotNone(strategy._size_entry("XLRE", wide, p, 781_120.0, 4e6, 42.0))


if __name__ == "__main__":
    unittest.main()
