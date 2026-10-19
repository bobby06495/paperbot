"""Offline tests for compute_perf.py and rotate_logs.py (temp folders only).

Run: python -m unittest discover -s tests -p test_perf_rotate.py -v
"""
import os

os.environ["PAPERBOT_NOTIFY"] = "off"  # tests must never send real notifications

import csv
import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from paperbot.reporting import compute_perf as perf
from paperbot.ops import rotate_logs as rot

FIELDS = ["timestamp_iso", "symbol", "side", "size", "fill_price", "order_id", "status",
          "order_ref", "exec_id", "commission"]
DAY = "2026-10-19"


def row(time, symbol, side, size, price, order_id, commission="1.0"):
    return {"timestamp_iso": f"{DAY}T{time}:00-04:00", "symbol": symbol, "side": side,
            "size": str(size), "fill_price": str(price), "order_id": str(order_id),
            "status": "filled", "order_ref": "", "exec_id": f"{order_id}-{time}",
            "commission": commission}


def write_csv(path: Path, rows: list[dict]):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


class PerfTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.csv = Path(self.tmp.name) / "trades.csv"

    def tearDown(self):
        self.tmp.cleanup()

    def summary(self, rows):
        write_csv(self.csv, rows)
        return perf.summarize(DAY, perf.round_trips(perf.load_day(self.csv, DAY)))

    def test_partial_sells_make_one_round_trip(self):
        s = self.summary([
            row("10:10", "AAPL", "BUY", 2, 100.0, 7),
            row("10:10", "AAPL", "BUY", 1, 100.0, 7),       # same entry order, 2nd execution
            row("11:00", "AAPL", "SELL", 1, 104.0, 8),      # partial profit
            row("15:51", "AAPL", "SELL", 2, 102.0, 9),      # force close
        ])
        self.assertEqual(s["total_trades"], 1)
        trip = s["trades"][0]
        # proceeds 104 + 204 = 308, cost 300, commissions 4 x $1 -> +4.00
        self.assertEqual(trip["pnl_usd"], 4.0)
        self.assertAlmostEqual(trip["pnl_pct"], 1.33)
        self.assertEqual(trip["hold_min"], 341.0)
        self.assertEqual((s["wins"], s["losses"], s["profit_factor"]), (1, 0, "inf"))

    def test_metrics_and_message(self):
        s = self.summary([
            row("10:10", "AAPL", "BUY", 1, 100.0, 1, "0"), row("10:40", "AAPL", "SELL", 1, 110.0, 2, "0"),
            row("10:15", "MSFT", "BUY", 1, 200.0, 3, "0"), row("10:50", "MSFT", "SELL", 1, 195.0, 4, "0"),
            row("10:20", "NVDA", "BUY", 1, 50.0, 5, "0"), row("11:00", "NVDA", "SELL", 1, 52.0, 6, "0"),
        ])
        self.assertEqual((s["total_trades"], s["wins"], s["losses"]), (3, 2, 1))
        self.assertEqual(s["win_rate_pct"], 66.7)
        self.assertEqual(s["gross_pnl_usd"], 7.0)
        self.assertEqual(s["largest_winner"], "AAPL $+10.00")
        self.assertEqual(s["largest_loser"], "MSFT $-5.00")
        self.assertEqual((s["avg_winner"], s["avg_loser"], s["profit_factor"]), (6.0, -5.0, 2.4))
        title, body = perf.message(s)
        self.assertEqual(title, f"Daily Summary {DAY}")
        self.assertEqual(body, "Trades: 3 (2W / 1L, 66.7%)\nP&L: $+7.00\nBest: AAPL $+10.00\n"
                               "Worst: MSFT $-5.00\nPF: 2.4")

    def test_other_days_unmatched_and_open_lots(self):
        old = row("10:00", "AAPL", "BUY", 1, 100.0, 1)
        old["timestamp_iso"] = "2026-10-16T10:00:00-04:00"
        s = self.summary([old,
                          row("09:45", "AAPL", "SELL", 1, 101.0, 2),   # sells yesterday's lot
                          row("10:30", "TSLA", "BUY", 1, 250.0, 3)])   # still open
        self.assertEqual((s["total_trades"], s["unmatched_sells"], s["open_lots"]), (0, 1, 1))
        self.assertEqual(s["profit_factor"], "n/a")
        self.assertEqual(perf.message(s)[1], "No closed trades today.")

    def test_missing_or_empty_ledger_is_zero_trades(self):
        self.assertEqual(perf.round_trips(perf.load_day(self.csv, DAY))["trips"], [])
        self.csv.write_text("")
        self.assertEqual(perf.round_trips(perf.load_day(self.csv, DAY))["trips"], [])

    def test_main_never_writes_the_ledger(self):
        write_csv(self.csv, [row("10:10", "AAPL", "BUY", 1, 100.0, 1),
                             row("10:40", "AAPL", "SELL", 1, 110.0, 2)])
        before = self.csv.read_bytes()
        page = Path(self.tmp.name) / "dashboard" / "performance.html"
        with mock.patch.object(perf, "TRADES_CSV", self.csv), \
                mock.patch.object(perf, "DASHBOARD", page), \
                mock.patch("paperbot.reporting.dashboard.build") as site, \
                mock.patch.object(perf, "HEARTBEAT", Path(self.tmp.name) / "none.json"), \
                mock.patch.object(perf, "notify") as sent, mock.patch("builtins.print"):
            self.assertEqual(perf.main(["--date", DAY]), 0)
        self.assertEqual(self.csv.read_bytes(), before)
        self.assertEqual(sent.call_args.args[0], f"Daily Summary {DAY}")
        self.assertTrue(page.exists())
        site.assert_called_once_with(page.parent)  # monitoring pages go next to it


class RotateTest(unittest.TestCase):
    TODAY = dt.date(2026, 10, 19)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.logs = Path(self.tmp.name, "logs").resolve()
        self.archive = self.logs / "archive"
        self.logs.mkdir()
        self.report: list[str] = []

    def tearDown(self):
        self.tmp.cleanup()

    def file(self, name: str, written: dt.date, text="x") -> Path:
        path = self.logs / name
        path.write_text(text)
        stamp = dt.datetime(written.year, written.month, written.day, 12, tzinfo=rot.ET).timestamp()
        os.utime(path, (stamp, stamp))
        return path

    def test_moves_only_older_log_and_jsonl_files(self):
        yesterday = self.TODAY - dt.timedelta(days=1)
        self.file("bot.log", yesterday)
        self.file("orders.jsonl", yesterday)
        self.file("cycle_errors.log", self.TODAY)          # written today: stays
        self.file("state.json", yesterday)                  # state file: never moved
        self.file("bot.pid", yesterday)
        moved = rot.rotate_dated(self.logs, self.archive, self.TODAY, self.report)
        self.assertEqual(moved, 2)
        self.assertTrue((self.archive / "2026-10-18" / "bot.log").exists())
        self.assertTrue((self.archive / "2026-10-18" / "orders.jsonl").exists())
        self.assertEqual(sorted(p.name for p in self.logs.iterdir() if p.is_file()),
                         ["bot.pid", "cycle_errors.log", "state.json"])

    def test_locked_file_is_skipped_not_forced(self):
        self.file("bot.log", self.TODAY - dt.timedelta(days=1))
        with mock.patch.object(rot.os, "replace", side_effect=PermissionError("in use")):
            moved = rot.rotate_dated(self.logs, self.archive, self.TODAY, self.report)
        self.assertEqual(moved, 0)
        self.assertIn("skipped bot.log: in use by another process", self.report)
        self.assertTrue((self.logs / "bot.log").exists())

    def test_same_name_twice_keeps_both(self):
        day = self.TODAY - dt.timedelta(days=1)
        self.file("bot.log", day)
        rot.rotate_dated(self.logs, self.archive, self.TODAY, self.report)
        self.file("bot.log", day)
        rot.rotate_dated(self.logs, self.archive, self.TODAY, self.report)
        self.assertEqual(sorted(p.name for p in (self.archive / day.isoformat()).iterdir()),
                         ["bot-1.log", "bot.log"])

    def test_safety_log_over_limit(self):
        path = self.file("safety_log.jsonl", self.TODAY, "y" * 50)
        with mock.patch.object(rot, "SAFETY_MAX_BYTES", 10):
            self.assertEqual(rot.rotate_safety_log(path, self.archive, self.TODAY, self.report), 1)
        self.assertEqual(path.stat().st_size, 0)  # fresh empty file
        self.assertEqual((self.archive / "2026-10-19_safety_log.jsonl").read_text(), "y" * 50)
        self.file("small.jsonl", self.TODAY)
        self.assertEqual(rot.rotate_safety_log(self.logs / "small.jsonl", self.archive,
                                               self.TODAY, self.report), 0)

    def test_trades_keep_90_days(self):
        trades = Path(self.tmp.name) / "trades.csv"
        recent, old, bad = row("10:00", "AAPL", "BUY", 1, 1, 1), row("10:00", "OLD", "BUY", 1, 1, 2), \
            row("10:00", "BAD", "BUY", 1, 1, 3)
        old["timestamp_iso"] = "2026-07-01T10:00:00-04:00"   # 110 days before TODAY
        bad["timestamp_iso"] = "not-a-date"
        write_csv(trades, [old, recent, bad])
        with mock.patch.object(rot.runstate, "running_pid", return_value=None):
            self.assertEqual(rot.trim_trades(trades, self.archive, self.TODAY, self.report), 1)
        with open(trades, newline="") as f:
            self.assertEqual([r["symbol"] for r in csv.DictReader(f)], ["AAPL", "BAD"])
        with open(self.archive / "trades_20261019.csv", newline="") as f:
            self.assertEqual([r["symbol"] for r in csv.DictReader(f)], ["OLD"])

    def test_trades_untouched_while_bot_runs(self):
        trades = Path(self.tmp.name) / "trades.csv"
        old = row("10:00", "OLD", "BUY", 1, 1, 2)
        old["timestamp_iso"] = "2026-01-01T10:00:00-05:00"
        write_csv(trades, [old])
        before = trades.read_bytes()
        with mock.patch.object(rot.runstate, "running_pid", return_value=4321):
            self.assertEqual(rot.trim_trades(trades, self.archive, self.TODAY, self.report), 0)
        self.assertEqual(trades.read_bytes(), before)
        self.assertIn("trades.csv not trimmed: bot or cycle run active", self.report)

    def test_refuses_destination_outside_archive(self):
        src = self.file("bot.log", self.TODAY)
        self.assertFalse(rot._move(src, Path(self.tmp.name) / "elsewhere.log", self.archive,
                                   self.report))
        self.assertTrue(src.exists())


if __name__ == "__main__":
    unittest.main()
