"""Offline tests for notify.py and the fill notifications in bot.py.

No network: requests.post is mocked and messages are sent synchronously.
Run: python -m unittest discover -s tests -p test_notify.py -v
"""
import os

os.environ["PAPERBOT_NOTIFY"] = "off"  # other test modules must never reach Telegram

import csv
import datetime as dt
import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

from paperbot.trading import bot
from paperbot.reporting import notify
from paperbot.trading.risk import REF_ENTRY, REF_EXIT, REF_STOP

TOKEN, CHAT = "123456:SECRET-token", "987654"


class InlineThread:
    """threading.Thread stand-in that runs the target immediately."""

    def __init__(self, target, args=(), **kwargs):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


class NotifyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [
            mock.patch.dict(os.environ, {"PAPERBOT_NOTIFY": ""}),
            mock.patch.object(notify, "TELEGRAM_BOT_TOKEN", TOKEN),
            mock.patch.object(notify, "TELEGRAM_CHAT_ID", CHAT),
            mock.patch.object(notify, "NOTIFY_URL", ""),
            mock.patch.object(notify, "ERROR_LOG", Path(self.tmp.name) / "notify_errors.log"),
            mock.patch.object(notify.threading, "Thread", InlineThread),
            mock.patch.object(notify.requests, "post"),
        ]
        started = [p.start() for p in self.patches]
        self.post = started[-1]
        self.post.return_value = mock.Mock(ok=True)

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def test_telegram_message(self):
        notify.notify("BUY BRK B", "@ $480.00, stop_loss $470.00", "default")
        url = self.post.call_args.args[0]
        data = self.post.call_args.kwargs["data"]
        self.assertEqual(url, f"https://api.telegram.org/bot{TOKEN}/sendMessage")
        self.assertEqual(data["chat_id"], CHAT)
        self.assertEqual(data["parse_mode"], "Markdown")
        self.assertEqual(data["text"], "*BUY BRK B*\n@ $480.00, stop\\_loss $470.00")
        self.assertEqual(self.post.call_args.kwargs["timeout"], 5)

    def test_ntfy_only_as_fallback(self):
        with mock.patch.object(notify, "NOTIFY_URL", "https://ntfy.example/topic"):
            notify.notify("T", "body", "high")
            self.assertEqual(self.post.call_count, 1)  # Telegram worked: no ntfy
            self.post.return_value = mock.Mock(ok=False, status_code=400, text="bad")
            notify.notify("T", "body", "high")
        self.assertEqual(self.post.call_count, 3)  # Telegram failed -> ntfy
        ntfy = self.post.call_args
        self.assertEqual(ntfy.args[0], "https://ntfy.example/topic")
        self.assertEqual(ntfy.kwargs["headers"], {"Title": "T", "Priority": "high"})
        self.assertEqual(ntfy.kwargs["data"], b"body")

    def test_never_raises_and_masks_secrets(self):
        self.post.side_effect = ConnectionError(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage chat {CHAT} unreachable")
        notify.notify("T", "body")  # must not raise
        log = notify.ERROR_LOG.read_text()
        self.assertIn("ConnectionError", log)
        self.assertNotIn(TOKEN, log)
        self.assertNotIn(CHAT, log)

    def test_disabled_or_unconfigured_sends_nothing(self):
        with mock.patch.dict(os.environ, {"PAPERBOT_NOTIFY": "off"}):
            notify.notify("T", "body")
        with mock.patch.object(notify, "TELEGRAM_BOT_TOKEN", ""):
            notify.notify("T", "body")
        self.post.assert_not_called()

    def test_alert_handler_forwards_alerts_once(self):
        logger = logging.getLogger("notify-test")
        logger.propagate = False
        handler = notify.AlertHandler()
        logger.addHandler(handler)
        with mock.patch.object(notify, "notify") as sent:
            logger.info("normal line")
            logger.warning("ALERT AAPL has no protective stop")
            logger.warning("ALERT AAPL has no protective stop")  # repeat within 10 min
            logger.error("cannot connect to IB Gateway")
        logger.removeHandler(handler)
        self.assertEqual([c.args for c in sent.call_args_list], [
            ("ALERT", "AAPL has no protective stop", "high"),
            ("ERROR", "cannot connect to IB Gateway", "high")])


def fill(exec_id, ref, order_id, shares, price, pnl=None, symbol="AAPL", minute=0):
    report = NS(execId=exec_id if pnl is not None else "", realizedPNL=pnl or 0.0,
                commission=1.0)
    when = dt.datetime(2026, 10, 19, 14, 30 + minute, tzinfo=dt.timezone.utc)
    side = "BOT" if ref == REF_ENTRY else "SLD"
    return NS(execution=NS(execId=exec_id, orderRef=ref, orderId=order_id, acctNumber="DU1",
                           shares=shares, price=price, side=side, time=when),
              contract=NS(symbol=symbol), commissionReport=report)


class FillNotifyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Path(self.tmp.name) / "trades.csv"
        mock.patch.object(bot, "TRADES_CSV", self.ledger).start()
        mock.patch.object(bot, "NOTIFY_STATE", Path(self.tmp.name) / "notify_state.json").start()
        mock.patch("paperbot.trading.strategy.load_state", return_value={
            "positions": {"AAPL": {"stop": 95.0, "initial_stop": 94.0}}, "entered": {}}).start()
        self.sent = mock.patch.object(bot, "notify").start()

    def tearDown(self):
        mock.patch.stopall()
        self.tmp.cleanup()

    def ib(self, fills, held=0.0):
        positions = [NS(contract=NS(symbol="AAPL"), position=held)] if held else []
        return mock.Mock(reqExecutions=lambda: fills, positions=lambda account: positions)

    def messages(self):
        return [c.args[:2] for c in self.sent.call_args_list]

    def test_entry_partial_fills_combined_into_one_buy(self):
        ib = self.ib([fill("e1", REF_ENTRY, 7, 2, 100.0), fill("e2", REF_ENTRY, 7, 1, 100.3)], 3)
        bot.notify_fills(ib, "DU1")
        self.assertEqual(self.messages(), [("BUY AAPL", "@ $100.10, stop $95.00, qty 3")])

    def test_stop_out_with_realized_pnl_sent_once(self):
        ib = self.ib([fill("s1", REF_STOP, 8, 3, 95.0, pnl=-15.4)])
        bot.notify_fills(ib, "DU1")
        bot.notify_fills(ib, "DU1")  # next loop / next cycle run: already pushed
        self.assertEqual(self.messages(), [("STOP AAPL", "exit $95.00, P&L $-15.40")])

    def test_partial_vs_exit(self):
        bot.notify_fills(self.ib([fill("x1", REF_EXIT, 9, 1, 104.0, pnl=4.0)], held=2), "DU1")
        bot.notify_fills(self.ib([fill("x2", REF_EXIT, 10, 2, 101.0, pnl=2.0)]), "DU1")
        self.assertEqual(self.messages(), [("PARTIAL AAPL", "sold 1/3 @ $104.00"),
                                           ("EXIT AAPL", "@ $101.00, P&L $+2.00")])

    def test_ignores_manual_and_other_account_fills(self):
        other = fill("m1", "", 11, 1, 50.0)
        foreign = fill("f1", REF_STOP, 12, 1, 50.0)
        foreign.execution.acctNumber = "DU999"
        bot.notify_fills(self.ib([other, foreign]), "DU1")
        self.sent.assert_not_called()

    def test_fills_recorded_in_ledger_once(self):
        fills = [fill("s1", REF_STOP, 8, 3, 95.0, pnl=-15.4, minute=5),
                 fill("e1", REF_ENTRY, 7, 3, 100.0)]
        bot.notify_fills(self.ib(fills), "DU1")
        bot.append_ledger(fills, self.ledger)  # a repeat never duplicates rows
        with open(self.ledger, newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual([(r["side"], r["exec_id"]) for r in rows],
                         [("BUY", "e1"), ("SELL", "s1")])  # time order, not order-id order
        self.assertEqual(rows[0]["timestamp_iso"], "2026-10-19T10:30:00-04:00")  # ET
        self.assertEqual((rows[0]["commission"], rows[1]["commission"]), ("", "1.0000"))
        self.assertEqual(rows[1]["order_ref"], REF_STOP)
        self.assertEqual((rows[0]["stop_price"], rows[1]["stop_price"]), ("94.0000", ""))

    def test_older_ledger_keeps_its_columns(self):
        self.ledger.write_text("timestamp_iso,symbol,side,size,fill_price,order_id,status,"
                               "order_ref,exec_id,commission\n")
        bot.append_ledger([fill("e1", REF_ENTRY, 7, 1, 100.0)], self.ledger)
        with open(self.ledger, newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 1)
        self.assertNotIn("stop_price", rows[0])  # no misaligned extra column

    def test_safe_wrapper_never_raises(self):
        broken = mock.Mock(reqExecutions=mock.Mock(side_effect=RuntimeError("socket closed")))
        with mock.patch.object(bot.log, "warning") as warned:
            bot.notify_fills_safely(broken, "DU1")
        warned.assert_called_once()


if __name__ == "__main__":
    unittest.main()
