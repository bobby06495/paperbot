"""Offline tests for bot.py order building and the post-close order check (no Gateway).

Run: python -m unittest discover -s tests -p test_bot.py -v
"""
import os

os.environ["PAPERBOT_NOTIFY"] = "off"  # tests must never send real notifications

import datetime as dt
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

from paperbot.trading import bot
from paperbot.trading.risk import ET, REF_ENTRY, REF_EXIT, REF_STOP, OrderIntent


class FakeTrade:
    def __init__(self, order_id: int, ref: str, done: bool = False):
        self.order = NS(orderId=order_id, orderRef=ref)
        self.contract = NS(symbol="AAPL")
        self.done = done

    def isDone(self):
        return self.done


class FakeIB:
    """openTrades() plus a clock: ib.sleep advances it and runs scheduled expiries."""

    def __init__(self, trades, expire_after=None, honour_cancel=True):
        self.trades = trades
        self.expire_after = expire_after  # seconds until IBKR expires the DAY orders
        self.honour_cancel = honour_cancel
        self.elapsed = 0.0
        self.cancelled = []
        self.client = NS(getReqId=lambda: 100)

    def reqAllOpenOrders(self):
        return self.trades

    def openTrades(self):
        return self.trades

    def sleep(self, seconds):
        self.elapsed += seconds
        if self.expire_after is not None and self.elapsed >= self.expire_after:
            for t in self.trades:
                t.done = True

    def cancelOrder(self, order):
        self.cancelled.append(order.orderId)
        if self.honour_cancel:
            for t in self.trades:
                if t.order.orderId == order.orderId:
                    t.done = True


class AwaitDayOrdersTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [mock.patch.object(bot, "LOGS", Path(self.tmp.name)),
                        mock.patch.object(bot.time, "monotonic", self.clock)]
        self.ib = None
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def clock(self):
        return self.ib.elapsed if self.ib else 0.0

    def test_waits_until_ibkr_expires_day_orders(self):
        self.ib = FakeIB([FakeTrade(7, REF_EXIT), FakeTrade(3, REF_STOP)], expire_after=12)
        self.assertTrue(bot.await_day_orders_done(self.ib, submit=True))
        self.assertEqual(self.ib.cancelled, [])  # IBKR expired them; nothing cancelled
        self.assertGreaterEqual(self.ib.elapsed, 12)

    def test_cancels_what_ibkr_did_not_expire(self):
        self.ib = FakeIB([FakeTrade(7, REF_EXIT)])
        self.assertTrue(bot.await_day_orders_done(self.ib, submit=True, timeout=5))
        self.assertEqual(self.ib.cancelled, [7])

    def test_unconfirmed_cancel_alerts_and_returns_false(self):
        self.ib = FakeIB([FakeTrade(7, REF_EXIT)], honour_cancel=False)
        with self.assertLogs("paperbot", "ERROR") as logs:
            self.assertFalse(bot.await_day_orders_done(self.ib, submit=True, timeout=5))
        self.assertIn("ALERT post-close", logs.output[0])

    def test_dry_run_never_cancels(self):
        self.ib = FakeIB([FakeTrade(7, REF_ENTRY)], honour_cancel=True)
        with self.assertLogs("paperbot", "ERROR"):
            self.assertFalse(bot.await_day_orders_done(self.ib, submit=False, timeout=2))
        self.assertEqual(self.ib.cancelled, [])

    def test_connection_drop_fails_closed(self):
        self.ib = FakeIB([])
        self.ib.reqAllOpenOrders = mock.Mock(side_effect=ConnectionError("Not connected"))
        with self.assertLogs("paperbot", "ERROR"):
            self.assertFalse(bot.await_day_orders_done(self.ib, submit=True))

    def test_post_close_window(self):
        monday = dt.datetime(2026, 10, 19, tzinfo=ET)
        self.assertFalse(bot.post_close(monday.replace(hour=15, minute=59)))
        self.assertTrue(bot.post_close(monday.replace(hour=16, minute=0)))
        self.assertTrue(bot.post_close(monday.replace(hour=16, minute=9)))
        self.assertFalse(bot.post_close(monday.replace(hour=16, minute=10)))
        self.assertFalse(bot.post_close(monday.replace(day=17, hour=16, minute=2)))  # Saturday


class OrderBuildTest(unittest.TestCase):
    CUTOFF = dt.datetime(2026, 10, 20, 15, 31, tzinfo=ET)

    def test_entry_bracket_is_gtd_with_oca_stop(self):
        intent = OrderIntent("AAPL", "BUY", 1, 100.2, stop_price=95.0, good_till=self.CUTOFF)
        parent, stop = bot.entry_bracket(FakeIB([]), intent, "DU1", "DAY")
        self.assertEqual((parent.tif, parent.goodTillDate), ("GTD", "20261020 15:31:00 US/Eastern"))
        self.assertFalse(parent.transmit)
        self.assertEqual((stop.tif, stop.parentId, stop.orderRef), ("GTC", parent.orderId, REF_STOP))
        self.assertTrue(stop.ocaGroup.startswith("paperbot:AAPL:"))
        self.assertEqual(stop.ocaType, bot.OCA_REDUCE_WITH_BLOCK)

    def test_closing_sell_joins_oca_group(self):
        intent = OrderIntent("AAPL", "SELL", 9, 100.0, closing=True, oca_group="g1")
        order = bot._order(intent, "DU1", "DAY", REF_EXIT)
        self.assertEqual((order.tif, order.ocaGroup, order.ocaType), ("DAY", "g1", 2))

    def test_plain_exit_unchanged(self):
        order = bot._order(OrderIntent("AAPL", "SELL", 3, 100.0, closing=True), "DU1", "DAY",
                           REF_EXIT)
        self.assertEqual((order.tif, order.ocaGroup, order.goodTillDate), ("DAY", "", ""))

    def test_oca_group_names_are_unique(self):
        self.assertNotEqual(bot.oca_group("aapl"), bot.oca_group("aapl"))


if __name__ == "__main__":
    unittest.main()
