"""Offline tests for the dashboard (compute_perf.py), heartbeat and R metrics.

Run: python -m unittest discover -s tests -p test_dashboard.py -v
"""
import os

os.environ["PAPERBOT_NOTIFY"] = "off"  # tests must never send real notifications

import csv
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

from paperbot.trading import bot
from paperbot.reporting import compute_perf as perf
from paperbot.runtime import runstate

ET = perf.ET
MON_11 = dt.datetime(2026, 10, 19, 11, 0, tzinfo=ET)
FIELDS = ["timestamp_iso", "symbol", "side", "size", "fill_price", "order_id", "status",
          "order_ref", "exec_id", "commission", "stop_price"]


def row(time, symbol, side, size, price, order_id, stop=""):
    return {"timestamp_iso": f"2026-10-19T{time}:00-04:00", "symbol": symbol, "side": side,
            "size": str(size), "fill_price": str(price), "order_id": str(order_id),
            "status": "filled", "order_ref": "", "exec_id": f"{order_id}-{time}",
            "commission": "0", "stop_price": stop}


class RTest(unittest.TestCase):
    def test_r_uses_recorded_stop(self):
        self.assertEqual(perf.r_multiple(100.0, 110.0, 95.0), (2.0, False))
        self.assertEqual(perf.r_multiple(100.0, 95.0, 95.0), (-1.0, False))

    def test_proxy_stop_one_percent_below_entry(self):
        self.assertEqual(perf.r_multiple(100.0, 102.0, None), (2.0, True))
        self.assertEqual(perf.r_multiple(100.0, 102.0, 100.5), (2.0, True))  # stop above entry

    def test_bucket_edges(self):
        counts = dict(perf.r_histogram([-2.0, -1.5, -1.0, 0.0, 0.5, 1.0, 2.0, 3.0, 3.01, -9]))
        self.assertEqual(list(counts.values()), [2, 2, 1, 2, 1, 1, 1])

    def test_round_trip_carries_r(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trades.csv"
            with open(path, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=FIELDS)
                w.writeheader()
                w.writerows([row("10:10", "AAPL", "BUY", 2, 100.0, 7, "95.0"),
                             row("10:10", "AAPL", "BUY", 1, 100.0, 7),
                             row("11:00", "AAPL", "SELL", 3, 110.0, 8)])
            trip = perf.round_trips(perf.load_rows(path))["trips"][0]
        self.assertEqual((trip["r_multiple"], trip["r_estimated"], trip["initial_stop"]),
                         (2.0, False, 95.0))


class StatusTest(unittest.TestCase):
    def hb(self, minutes_ago, status="ok"):
        when = MON_11 - dt.timedelta(minutes=minutes_ago)
        return {"time": when.isoformat(), "source": "cycle.py", "mode": "SUBMIT",
                "status": status, "error": "boom" if status == "crashed" else ""}

    def test_states(self):
        self.assertEqual(perf.bot_status(None, MON_11)[:2], ("NO DATA", "no cycle data yet"))
        self.assertEqual(perf.bot_status(self.hb(3), MON_11)[0], "ACTIVE")
        self.assertEqual(perf.bot_status(self.hb(25), MON_11)[0], "STALE")
        self.assertEqual(perf.bot_status(self.hb(3, "crashed"), MON_11)[0], "CRASHED")
        evening = MON_11.replace(hour=19)
        self.assertEqual(perf.bot_status(self.hb(3), evening)[0], "IDLE")

    def test_unrealized_r(self):
        hb = {"positions": [{"symbol": "AAPL", "qty": 3, "entry": 100.0, "stop": 100.0,
                             "initial_stop": 95.0, "market_price": 107.5, "managed": True},
                            {"symbol": "MU", "qty": 1, "entry": 50.0, "stop": None,
                             "initial_stop": None, "market_price": None, "managed": False}]}
        rows = perf.open_position_rows(hb)
        self.assertEqual((rows[0]["r"], rows[0]["r_estimated"]), (1.5, False))
        self.assertIsNone(rows[1]["r"])


class RenderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def page(self, rows=(), heartbeat=None) -> str:
        trades = self.dir / "trades.csv"
        with open(trades, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            w.writerows(rows)
        hb_path = self.dir / "heartbeat.json"
        if heartbeat is not None:
            hb_path.write_text(json.dumps(heartbeat))
        out = perf.refresh_dashboard(self.dir / "dashboard" / "index.html", trades, hb_path, MON_11,
                                     self.dir / "test_results.json")
        return out.read_text(encoding="utf-8")

    def test_static_page_requirements(self):
        html = self.page()
        self.assertIn('<meta http-equiv="refresh" content="30">', html)
        self.assertIn("bootstrap@5", html)
        self.assertNotIn("<script", html.lower())
        self.assertIn("Bot Status: NO DATA", html)
        self.assertIn("no cycle data yet", html)
        self.assertIn("No closed trades yet", html)
        self.assertIn("No open positions", html)

    def test_trades_colours_and_escaping(self):
        html = self.page([
            row("10:10", "AAPL", "BUY", 1, 100.0, 1, "95.0"), row("10:40", "AAPL", "SELL", 1, 110.0, 2),
            row("10:15", "<b>X", "BUY", 1, 200.0, 3), row("10:50", "<b>X", "SELL", 1, 190.0, 4),
        ], heartbeat={"time": MON_11.isoformat(), "source": "cycle.py", "mode": "SUBMIT",
                      "status": "ok", "positions": []})
        self.assertIn("Bot Status: ACTIVE", html)
        self.assertIn('class="text-success fw-bold">+2.00R', html)
        self.assertIn('class="text-danger fw-bold">-5.00R <small>(est.)</small>', html)
        self.assertIn("&lt;B&gt;X", html)  # symbols are upper-cased, then escaped
        self.assertNotIn("<B>X", html)
        self.assertIn("+0.00 USD", html)  # today's P&L: +10 and -10

    def test_write_is_atomic_and_ledger_untouched(self):
        rows = [row("10:10", "AAPL", "BUY", 1, 100.0, 1), row("10:40", "AAPL", "SELL", 1, 110.0, 2)]
        trades = self.dir / "trades.csv"
        self.page(rows)
        before = trades.read_bytes()
        perf.refresh_dashboard(self.dir / "dashboard" / "index.html", trades,
                               self.dir / "none.json", MON_11, self.dir / "none.json")
        self.assertEqual(sorted(p.name for p in (self.dir / "dashboard").iterdir()), ["index.html"])
        self.assertEqual(trades.read_bytes(), before)  # the ledger is never written

    def test_no_test_results_yet(self):
        self.assertIn("No test results yet", self.page())

    def test_test_results_section(self):
        def case(module, name, status, warnings=(), detail=""):
            return {"module": module, "module_doc": f"Tests for {module}", "class": "T",
                    "class_doc": "", "name": name, "description": name.replace("_", " "),
                    "status": status, "detail": detail, "duration_s": 0.01,
                    "warnings": list(warnings)}
        log = {"kind": "log", "category": "WARNING", "message": "ALERT <stop>", "location": "paperbot"}
        results = {"time": MON_11.isoformat(), "duration_s": 0.5, "status": "failed",
                   "summary": {"total": 3, "passed": 2, "failed": 1, "error": 0, "skipped": 0,
                               "python_warnings": 0, "log_warnings": 1, "tests_with_warnings": 1},
                   "import_warnings": [],
                   "tests": [case("test_a", "test_ok", "passed", [log]),
                             case("test_a", "test_also_ok", "passed"),
                             case("test_b", "test_broken", "failed", detail="AssertionError: 1 != 2")]}
        (self.dir / "test_results.json").write_text(json.dumps(results))
        html = self.page()
        self.assertIn("Tests FAILED: 2 of 3 passed", html)
        self.assertIn("1 log warnings", html)
        self.assertIn("ALERT &lt;stop&gt;", html)  # warning text is escaped
        self.assertIn("AssertionError: 1 != 2", html)
        self.assertIn('<details class="mb-2" open><summary><code>test_b</code>', html)  # failures open
        self.assertIn('<details class="mb-2"><summary><code>test_a</code>', html)
        self.assertIn("2/2 passed", html)
        self.assertNotIn("<script", html.lower())


class HeartbeatTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "heartbeat.json"
        mock.patch.object(runstate, "HEARTBEAT_FILE", self.path).start()

    def tearDown(self):
        mock.patch.stopall()
        self.tmp.cleanup()

    def test_positions_from_portfolio(self):
        items = [NS(contract=NS(secType="STK", symbol="AAPL"), position=3.0, averageCost=100.0,
                    marketPrice=104.0),
                 NS(contract=NS(secType="STK", symbol="MU"), position=1.0, averageCost=50.0,
                    marketPrice=float("nan")),
                 NS(contract=NS(secType="OPT", symbol="SPY"), position=1.0, averageCost=1.0,
                    marketPrice=1.0)]
        ib = mock.Mock(portfolio=lambda account: items)
        with mock.patch("paperbot.trading.strategy.load_state", return_value={
                "positions": {"AAPL": {"stop": 98.0, "initial_stop": 95.0}}, "entered": {}}):
            positions = bot.heartbeat_positions(ib, "DU1")
        self.assertEqual([p["symbol"] for p in positions], ["AAPL", "MU"])
        self.assertEqual((positions[0]["stop"], positions[0]["managed"]), (98.0, True))
        self.assertIsNone(positions[1]["market_price"])

    def test_after_pass_writes_heartbeat_and_never_raises(self):
        ib = mock.Mock(portfolio=lambda account: [])
        with mock.patch("paperbot.reporting.compute_perf.refresh_dashboard", side_effect=OSError("disk full")), \
                mock.patch("paperbot.reporting.telemetry.record_pass") as recorded, \
                mock.patch.object(bot.log, "warning") as warned:
            bot.after_pass(ib, "DU1", "cycle.py", True)
        data = json.loads(self.path.read_text())
        self.assertEqual((data["status"], data["mode"], data["source"]), ("ok", "SUBMIT", "cycle.py"))
        recorded.assert_called_once_with(ib, "DU1", "cycle.py", True)
        warned.assert_called_once()  # the dashboard failure was only logged

    def test_after_pass_survives_a_telemetry_failure(self):
        ib = mock.Mock(portfolio=lambda account: [])
        with mock.patch("paperbot.reporting.telemetry.record_pass", side_effect=ValueError("bad line")), \
                mock.patch("paperbot.reporting.compute_perf.refresh_dashboard") as refreshed, \
                mock.patch.object(bot.log, "warning") as warned:
            bot.after_pass(ib, "DU1", "cycle.py", True)
        self.assertIn("telemetry skipped", warned.call_args.args[0])
        refreshed.assert_called_once()  # the dashboard is still rebuilt

    def test_crash_keeps_last_positions(self):
        runstate.write_heartbeat({"time": "x", "status": "ok", "positions": [{"symbol": "AAPL"}]})
        runstate.mark_heartbeat("crashed", "ValueError: boom")
        data = json.loads(self.path.read_text())
        self.assertEqual((data["status"], data["error"]), ("crashed", "ValueError: boom"))
        self.assertEqual(data["positions"], [{"symbol": "AAPL"}])


if __name__ == "__main__":
    unittest.main()
