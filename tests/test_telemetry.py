"""Offline tests for the monitoring telemetry (telemetry.py) and dashboard builder (dashboard.py).

Run: python -m unittest discover -s tests -p test_telemetry.py -v
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

from paperbot.reporting import dashboard as dash
from paperbot.reporting import telemetry as tm

ET = tm.ET
RULES = {"time_filter": {"earliest_entry_et": "10:05", "latest_entry_et": "15:30",
                         "force_close_et": "15:51"},
         "exit": {"partial_profit_trigger_R": 0.75, "breakeven_trigger_R": 1.0},
         "risk": {"max_concurrent_positions": 5, "max_order_value": 2500.0,
                  "max_orders_per_day": 5, "max_open_orders": 2, "max_daily_loss": 1000.0}}
CONFIG = {"limits": {"allowed_symbols": ["*"], "limit_orders_only": True}}


def at(hour, minute, second=0, day=19):
    return dt.datetime(2026, 10, day, hour, minute, second, tzinfo=ET)  # 19 = Monday


class PhaseTest(unittest.TestCase):
    def test_phases_follow_rules_times(self):
        times = tm.session_times(RULES)
        cases = {at(9, 0): "pre_open", at(9, 30): "opening", at(10, 4, 59): "opening",
                 at(10, 5): "entry_window", at(15, 30): "entry_window",
                 at(15, 30, 59): "entry_window", at(15, 31): "manage_only",
                 at(15, 51): "force_close", at(16, 0): "post_close", at(16, 5): "post_close",
                 at(16, 10): "closed", at(11, 0, day=18): "closed"}
        for when, expected in cases.items():
            self.assertEqual(tm.phase(when, times), expected, when)


class ClassifyTest(unittest.TestCase):
    def test_price_check_distance(self):
        out = tm.classify_failure("I2 price not above today's high 101.00", 100.0,
                                  ["D2 prior close 90.00 > SMA200 80.00", "D1 above prior-day high 95.00"])
        self.assertEqual((out["check"], out["static"], out["threshold"]), ("I2", False, 101.0))
        self.assertAlmostEqual(out["distance_pct"], 1.0)
        self.assertEqual(out["passed_steps"], tm.STEP_INDEX["I2"])
        self.assertEqual(out["levels"], {"prior_close": 90.0, "sma200": 80.0, "prior_high": 95.0})

    def test_static_and_rvol_failures(self):
        self.assertTrue(tm.classify_failure("D3 gap 1.2% < 3.0%", 50.0, [])["static"])
        rvol = tm.classify_failure("I3 RVOL 1.50 < 2.0", 50.0, [])
        self.assertEqual((rvol["current"], rvol["threshold"]), (1.5, 2.0))
        na = tm.classify_failure("I3 RVOL n/a < 2.0", 50.0, [])
        self.assertIsNone(na["current"])
        self.assertIsNone(na["distance_pct"])

    def test_pass_and_precheck(self):
        self.assertEqual(tm.classify_failure(None, 10.0, [])["passed_steps"], len(tm.STEPS))
        self.assertEqual(tm.classify_failure("already entered today", 0, [])["check"], "precheck")


def trade(order_id, symbol, ref, action="SELL", kind="STP", qty=1, filled=0, aux=95.0, lmt=0.0,
          status="Submitted", parent=0):
    return NS(order=NS(orderId=order_id, parentId=parent, orderRef=ref, action=action, orderType=kind,
                       totalQuantity=qty, lmtPrice=lmt, auxPrice=aux, tif="GTC"),
              orderStatus=NS(status=status, filled=filled, remaining=qty - filled),
              contract=NS(symbol=symbol), isDone=lambda: False)


class FakeIB:
    """Only the cached readers telemetry may use. Anything else is recorded as a violation."""
    ALLOWED = {"accountValues", "pnl", "portfolio", "trades", "openTrades"}

    def __init__(self, values=(), positions=(), trades=(), pnl=NS(dailyPnL=-50.0, unrealizedPnL=-50.0,
                                                                     realizedPnL=0.0)):
        self.calls, self._values, self._positions, self._trades, self._pnl = [], values, positions, trades, pnl

    def __getattr__(self, name):
        self.calls.append(name)
        if name not in self.ALLOWED:
            raise AssertionError(f"telemetry touched ib.{name}")
        data = {"accountValues": self._values, "pnl": [self._pnl], "portfolio": self._positions,
                "trades": self._trades, "openTrades": self._trades}[name]
        return lambda *a, **k: list(data)


class RecordPassTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.logs = root / "logs"
        self.logs.mkdir()
        (root / "rules.json").write_text(json.dumps(RULES))
        (root / "config.json").write_text(json.dumps(CONFIG))
        (self.logs / "strategy_state.json").write_text(json.dumps(
            {"positions": {"AAPL": {"initial_stop": 95.0, "stop": 95.0, "breakeven": False,
                                    "partial_done": False}}, "entered": {}}))
        (self.logs / "state.json").write_text(json.dumps(
            {"date": "2026-10-19", "orders_today": 1, "halted": False, "halt_reason": ""}))
        patches = {"TELEMETRY_FILE": self.logs / "telemetry.jsonl", "STATE_FILE": self.logs / "tstate.json",
                   "BOT_LOG": self.logs / "bot.log", "SAFETY_LOG": self.logs / "safety_log.jsonl",
                   "ORDERS_LOG": self.logs / "orders.jsonl", "RISK_STATE": self.logs / "state.json",
                   "STRATEGY_STATE": self.logs / "strategy_state.json"}
        self.patchers = [mock.patch.object(tm, k, v) for k, v in patches.items()]
        self.patchers += [mock.patch.object(tm.paths, "RULES_FILE", root / "rules.json"),
                          mock.patch.object(tm.paths, "CONFIG_FILE", root / "config.json"),
                          mock.patch.object(tm, "_watchlist", return_value=("watchlist", ["AAPL", "MSFT"], True)),
                          mock.patch.object(tm.runstate, "stop_requested", return_value=False),
                          mock.patch.object(tm, "fx_conversion", return_value=None),
                          mock.patch.object(tm, "pnl_decision", return_value=None),
                          mock.patch.object(tm, "saved_conversion", return_value=None)]
        for p in self.patchers:
            p.start()

    def tearDown(self):
        for p in reversed(self.patchers):
            p.stop()
        self.tmp.cleanup()

    def append(self, name, *lines):
        with open(self.logs / name, "a", encoding="utf-8") as f:
            for line in lines:
                f.write((json.dumps(line) if isinstance(line, dict) else line) + "\n")

    def ib(self, **kwargs):
        position = NS(contract=NS(secType="STK", symbol="AAPL"), position=3.0, averageCost=100.0,
                      marketPrice=103.0, unrealizedPNL=9.0)
        stop = trade(7, "AAPL", tm.REF_STOP, qty=3)
        return FakeIB(positions=[position], trades=[stop], **kwargs)

    def test_record_reads_only_new_lines_and_groups_passes(self):
        self.append("bot.log", "[2026-10-19 10:59:00 ET] INFO cycle start (run, SUBMIT) on paper account DU1",
                    "[2026-10-19 11:00:00 ET] INFO cycle start (run, SUBMIT) on paper account DU1",
                    "[2026-10-19 11:00:01 ET] INFO signal MSFT: trend join",
                    "[2026-10-19 11:00:02 ET] ERROR ALERT AAPL has no protective stop at IBKR; placing one at 95.00")
        self.append("safety_log.jsonl", {"symbol": "MSFT", "time": "2026-10-19T11:00:01-04:00", "pass": False,
                                         "reasons": ["FAIL I2 price not above today's high 101.00"], "price": 100.0})
        self.append("orders.jsonl", {"time": "2026-10-19 11:00:02 ET", "event": "stop", "orderId": 7,
                                     "stop": {"symbol": "AAPL", "quantity": 3, "stop_price": 95.0,
                                              "reason": "restore protective stop"}},
                    {"time": "2026-10-19 11:00:03 ET", "event": "status", "orderId": 7,
                     "status": "Submitted", "filled": 0})
        ib = self.ib()
        first = tm.record_pass(ib, "DU0000000", "cycle.py", True, now=at(11, 0, 4))
        self.assertTrue(set(ib.calls) <= FakeIB.ALLOWED)
        self.assertEqual(first["started"], "2026-10-19T11:00:00-04:00")  # last start line, not 10:59
        self.assertEqual(first["duration_s"], 4.0)
        self.assertEqual(first["phase"], "entry_window")
        self.assertEqual(first["signals"], ["MSFT: trend join"])
        self.assertEqual(first["evaluations"][0]["check"], "I2")
        self.assertEqual([e["symbol"] for e in first["order_events"]], ["AAPL", "AAPL"])  # status mapped
        self.assertEqual(first["order_events"][1]["role"], "stop")
        self.assertEqual(first["requests"], 1)
        pos = first["positions"][0]
        self.assertEqual((pos["r_now"], pos["triggers"]), (0.6, {"partial": 103.75, "breakeven": 105.0}))
        checks = {v["key"]: v["status"] for v in first["validations"]}
        self.assertEqual((checks["stop_coverage"], checks["alerts"], checks["daily_loss"]), ("pass", "warn", "pass"))
        self.assertEqual(first["account"]["id_masked"], "DU…000")

        second = tm.record_pass(ib, "DU0000000", "cycle.py", True, now=at(11, 0, 34))
        self.assertEqual((second["run_id"], second["pass_in_run"]), (first["run_id"], 2))
        self.assertEqual((second["evaluations"], second["order_events"], second["log"]), ([], [], []))
        lines = (self.logs / "telemetry.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])["v"], tm.SCHEMA_VERSION)

    def test_non_usd_account_is_reported_not_hidden(self):
        values = [NS(tag="NetLiquidation", currency="SGD", value="1000013.88"),
                  NS(tag="BuyingPower", currency="SGD", value="6666759.20")]
        record = tm.record_pass(self.ib(values=values, pnl=NS(dailyPnL=float("nan"), unrealizedPnL=None,
                                                              realizedPnL=None)),
                                "DU0000000", "cycle.py", True, now=at(11, 5))
        acct = record["account"]
        self.assertIsNone(acct["net_liquidation"])
        self.assertEqual((acct["base_currency"], acct["net_liquidation_base"]), ("SGD", 1000013.88))
        checks = {v["key"]: v for v in record["validations"]}
        self.assertEqual(checks["net_liq"]["status"], "fail")
        self.assertIn("SGD", checks["net_liq"]["detail"])
        self.assertEqual(checks["pnl_available"]["status"], "fail")  # NaN daily P&L

    def test_converted_account_value_is_reported_with_its_source(self):
        values = [NS(tag="NetLiquidation", currency="SGD", value="1000013.88"),
                  NS(tag="BuyingPower", currency="SGD", value="6666759.20")]
        conv = {"usd": 781260.84, "currency": "SGD", "base": 1000013.88, "rate": 1.28, "source": "Yahoo 2026-10-19"}
        with mock.patch.object(tm, "fx_conversion", return_value=conv):
            record = tm.record_pass(self.ib(values=values), "DU0000000", "cycle.py", True, now=at(11, 5))
        acct = record["account"]
        self.assertEqual((acct["net_liquidation"], acct["buying_power"]), (781260.84, round(6666759.20 / 1.28, 2)))
        self.assertEqual(acct["fx"], {"currency": "SGD", "rate": 1.28, "source": "Yahoo 2026-10-19"})
        net_liq = next(v for v in record["validations"] if v["key"] == "net_liq")
        self.assertEqual(net_liq["status"], "pass")
        self.assertIn("converted from SGD", net_liq["detail"])

    def test_flat_account_pnl_shows_the_value_the_guard_used(self):
        decision = {"value": 0.0, "source": "flat today (no position, no execution); IBKR sends none"}
        with mock.patch.object(tm, "pnl_decision", return_value=decision):
            record = tm.record_pass(self.ib(pnl=NS(dailyPnL=float("nan"), unrealizedPnL=None, realizedPnL=None)),
                                    "DU0000000", "cycle.py", True, now=at(11, 5))
        check = next(v for v in record["validations"] if v["key"] == "pnl_available")
        self.assertEqual((record["account"]["daily_pnl"], check["status"]), (0.0, "pass"))
        self.assertIn("flat today", check["detail"])

    def test_rotated_file_is_read_from_the_start(self):
        self.append("bot.log", "[2026-10-19 11:00:00 ET] INFO cycle start (run, SUBMIT) on paper account DU1")
        tm.record_pass(self.ib(), "DU1", "cycle.py", True, now=at(11, 0, 5))
        (self.logs / "bot.log").write_text("[2026-10-19 11:05:00 ET] WARNING x\n")  # shorter: rotated
        record = tm.record_pass(self.ib(), "DU1", "cycle.py", True, now=at(11, 5, 5))
        self.assertEqual([l["msg"] for l in record["log"]], ["x"])


class TaskInfoTest(unittest.TestCase):
    ROW = {"Last Result": "1", "Last Run Time": "10/19/2026 11:10:00 PM",
           "Next Run Time": "10/19/2026 11:15:00 PM", "Scheduled Task State": "Enabled",
           "Status": "Ready", "Task To Run": "C:\\x\\python.exe -m paperbot.runtime.cycle --submit"}

    def test_failed_result_is_flagged(self):
        info = dash.task_info("HT_Cycle", self.ROW)
        self.assertEqual((info["last_result"], info["result_ok"], info["enabled"]), (1, False, True))
        self.assertEqual(info["last_result_text"], "exit code 1 (error)")
        self.assertFalse(dash.task_info("HT_Cycle", None)["registered"])
        never = dash.task_info("HT_Cycle", {**self.ROW, "Last Result": "267011",
                                            "Last Run Time": "11/30/1999 12:00:00 AM"})
        self.assertTrue(never["result_ok"])
        self.assertIsNone(never["last_run"])

    def test_interpreter_missing_packages(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp, "venv", "Scripts", "python.exe")
            exe.parent.mkdir(parents=True)
            exe.write_text("")
            site = Path(tmp, "venv", "Lib", "site-packages")
            (site / "ib_async").mkdir(parents=True)
            (site / "pandas").mkdir()
            info = dash._interpreter(f'"{exe}" -m paperbot.runtime.cycle --submit')
            self.assertEqual((info["exists"], info["missing"], info["ok"]), (True, ["tzdata"], False))
            (site / "tzdata").mkdir()
            self.assertTrue(dash._interpreter(f"{exe} -m x")["ok"])
        self.assertFalse(dash._interpreter("C:\\nope\\python.exe -m x")["exists"])

    def test_scheduler_status(self):
        hb = {"time": at(11, 0).isoformat(), "status": "ok", "source": "cycle.py"}
        ok = {"available": True, "cycle": {"registered": True, "enabled": True, "result_ok": True}}
        bad = {"available": True, "cycle": {"registered": True, "enabled": True, "result_ok": False,
                                            "last_result_text": "exit code 1 (error)"}}
        self.assertEqual(dash.scheduler_status(at(11, 3), ok, hb, "entry_window", False, None)[0], "ACTIVE")
        self.assertEqual(dash.scheduler_status(at(11, 3), bad, hb, "entry_window", False, None)[0], "DEGRADED")
        self.assertEqual(dash.scheduler_status(at(11, 30), ok, hb, "entry_window", False, None)[0], "STALE")
        self.assertEqual(dash.scheduler_status(at(11, 3), ok, hb, "entry_window", True, None)[0], "PAUSED")
        self.assertEqual(dash.scheduler_status(at(20, 0), ok, hb, "closed", False, None)[0], "IDLE")


class MissedRunsTest(unittest.TestCase):
    TIMES = tm.session_times(RULES)

    @staticmethod
    def log(*times, msg="cycle start (run, SUBMIT) on paper account DU0000000"):
        return [{"time": f"2026-10-19T{t}:02-04:00", "level": "INFO", "msg": msg} for t in times]

    def test_gap_after_the_pc_slept(self):
        slots = [f"15:{m:02d}" for m in range(0, 40, 5)]  # 15:00 ... 15:35, then nothing
        missed = dash.missed_runs(at(19, 14), self.log(*slots), self.TIMES)
        self.assertEqual(missed, ["15:40", "15:45", "15:50", "15:55", "16:00"])
        self.assertEqual(dash._ranges(missed), "15:40-16:00 (5 runs)")

    def test_skips_and_failed_connects_count_as_a_run(self):
        log = (self.log("11:00") + self.log("11:05", msg="cycle skipped: STOP file present")
               + self.log("11:10", msg="cannot connect to IB Gateway at 127.0.0.1:4002") + self.log("11:20"))
        self.assertEqual(dash.missed_runs(at(11, 23), log, self.TIMES), ["11:15"])

    def test_current_slot_and_quiet_days_not_flagged(self):
        self.assertEqual(dash.missed_runs(at(11, 6), self.log("11:00"), self.TIMES), [])  # 11:05 may still start
        self.assertEqual(dash.missed_runs(at(11, 0), [], self.TIMES), [])
        self.assertEqual(dash.missed_runs(at(11, 0, day=18), self.log("11:00"), self.TIMES), [])  # Sunday

    def test_ranges(self):
        self.assertEqual(dash._ranges(["10:50", "11:15", "11:20"]), "10:50, 11:15-11:20 (2 runs)")


class SavedConversionTest(unittest.TestCase):
    def test_saved_rate_used_without_network(self):
        from paperbot.core import account
        with tempfile.TemporaryDirectory() as tmp:
            rate_file = Path(tmp) / "fx_rate.json"
            today = dt.datetime.now(ET).date().isoformat()
            rate_file.write_text(json.dumps({"currency": "SGD", "rate": 1.28, "date": today}))
            with mock.patch.object(account, "RATE_FILE", rate_file),                     mock.patch.object(account, "fallback_mode", return_value="auto"),                     mock.patch.object(account, "fetch_yahoo_rate") as fetch:
                conv = tm.saved_conversion("SGD", 1280.0)
                self.assertEqual((conv["usd"], conv["rate"]), (1000.0, 1.28))
                self.assertIn("not needed by the bot", conv["source"])
                self.assertIsNone(tm.saved_conversion("EUR", 1280.0))  # no rate for that currency
                fetch.assert_not_called()
            with mock.patch.object(account, "RATE_FILE", rate_file),                     mock.patch.object(account, "fallback_mode", return_value="off"):
                self.assertIsNone(tm.saved_conversion("SGD", 1280.0))


class LifecycleTest(unittest.TestCase):
    def test_bracket_partial_fill_and_stop_modification(self):
        events = [
            {"time": "2026-10-19T10:05:03-04:00", "event": "sent", "order_ids": [10, 11], "symbol": "NVDA",
             "intent": {"action": "BUY", "quantity": 4, "limit_price": 189.58, "stop_price": 183.55,
                        "reason": "trend join"}, "detail": "BUY 4 @ 189.58"},
            {"time": "2026-10-19T11:15:00-04:00", "event": "stop", "order_ids": [11], "symbol": "NVDA",
             "stop": {"quantity": 4, "stop_price": 190.35, "reason": "sync"}, "detail": "STP 4 @ 190.35"},
            {"time": "2026-10-19T15:55:03-04:00", "event": "sent", "order_ids": [12], "symbol": "NVDA",
             "intent": {"action": "SELL", "quantity": 4, "limit_price": 199.4, "reason": "force close (attempt 1)",
                        "closing": True}, "detail": "SELL 4"},
            {"time": "2026-10-19T15:56:05-04:00", "event": "status", "order_ids": [12], "status": "Cancelled",
             "filled": 1, "detail": "Cancelled (filled 1)"},
        ]
        ledger = [{"order_id": "10", "symbol": "NVDA", "side": "BUY", "size": "4", "fill_price": "189.35",
                   "timestamp_iso": "2026-10-19T10:05:04-04:00", "order_ref": "paperbot:entry"},
                  {"order_id": "12", "symbol": "NVDA", "side": "SELL", "size": "1", "fill_price": "199.78",
                   "timestamp_iso": "2026-10-19T15:55:30-04:00", "order_ref": "paperbot:exit"}]
        orders = {o["order_id"]: o for o in dash.order_lifecycle(events, ledger, [])["orders"]}
        self.assertEqual((orders[10]["kind"], orders[10]["state"], orders[10]["avg_fill"]),
                         ("Entry", "Filled", 189.35))
        self.assertEqual([e["kind"] for e in orders[11]["events"]], ["placed", "modified"])
        self.assertEqual(orders[11]["stop"], 190.35)
        self.assertEqual((orders[12]["kind"], orders[12]["state"], orders[12]["partial"]),
                         ("EOD liquidation", "Partially filled, rest cancelled", True))
        self.assertTrue(orders[12]["liquidation"])


class ScannerTest(unittest.TestCase):
    def test_statuses(self):
        def ev(symbol, fail=None, price=100.0, passed=False):
            return tm.evaluation({"symbol": symbol, "time": "2026-10-19T11:00:00-04:00", "pass": passed,
                                  "reasons": [f"FAIL {fail}"] if fail else [], "price": price})
        runs = [{"evaluations": [ev("A", "I2 price not above today's high 100.30"),
                                 ev("B", "D3 gap 1.0% < 3.0%"), ev("C", "D1 price not above prior-day high 110"),
                                 ev("D", passed=True)]}]
        watch = {"symbols": [{"symbol": s, "gap_pct": 4.0} for s in "ABCDEF"]}
        rows = {r["symbol"]: r["status"] for r in dash.scanner_view(
            watch, runs, "watchlist", list("ABCDEF"), {"entered": {"F": "2026-10-19"}}, {"E"}, "2026-10-19")}
        self.assertEqual(rows, {"A": "NEAR", "B": "BLOCKED TODAY", "C": "WAITING", "D": "SIGNAL",
                                "E": "IN POSITION", "F": "ENTERED TODAY"})


class BuildTest(unittest.TestCase):
    def test_build_writes_pages_and_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            logs = root / "logs"
            logs.mkdir()
            record = {"v": 1, "run_id": "cycle-1-110000", "pass_in_run": 1, "source": "cycle.py",
                      "ended": "2026-10-19T11:00:04-04:00", "phase": "entry_window", "requests": 0,
                      "account": {"daily_pnl": -12.5}, "risk": {}, "positions": [], "orders": [],
                      "evaluations": [], "order_events": [], "validations": [], "log": [],
                      "scan": {"source": "watchlist", "evaluated": 0},
                      "note": "</script><script>alert(1)</script>"}
            (logs / "telemetry.jsonl").write_text(json.dumps(record) + "\n")
            (logs / "bot.log").write_text(
                "[2026-10-19 10:55:00 ET] INFO cycle skipped: STOP file present\n"
                "[2026-10-19 11:00:00 ET] INFO cycle start (run, SUBMIT) on paper account DU1\n")
            runner = mock.Mock(return_value=NS(returncode=1, stdout=""))  # no tasks registered
            with mock.patch.multiple(tm, TELEMETRY_FILE=logs / "telemetry.jsonl", BOT_LOG=logs / "bot.log",
                                     ORDERS_LOG=logs / "orders.jsonl", RISK_STATE=logs / "state.json",
                                     STRATEGY_STATE=logs / "strategy_state.json"), \
                    mock.patch.multiple(dash, HEARTBEAT=logs / "hb.json", CYCLE_ERRORS=logs / "err.log",
                                        TEST_RESULTS=logs / "tests.json", SCHEDULER_CACHE=logs / "cache.json"), \
                    mock.patch.object(dash.paths, "TRADES_CSV", root / "trades.csv"), \
                    mock.patch.object(dash.paths, "WATCHLIST_FILE", root / "watchlist.txt"), \
                    mock.patch.object(dash.sys, "platform", "win32"), \
                    mock.patch("paperbot.runtime.runstate.stop_requested", return_value=False), \
                    mock.patch("paperbot.runtime.runstate.running_pid", return_value=None):
                index = dash.build(root / "dashboard", now=at(11, 2), runner=runner)
            out = root / "dashboard"
            self.assertTrue(index.exists())
            for page in ("logs.html", "strategy.html", "assets/core.js", "assets/overview.js"):
                self.assertTrue((out / page).exists(), page)
            text = (out / "data" / "telemetry.js").read_text(encoding="utf-8")
            self.assertTrue(text.startswith("window.PAPERBOT = "))
            self.assertNotIn("</script>", text)  # cannot break out of the <script> tag
            data = json.loads(text[len("window.PAPERBOT = "):-2])
        self.assertEqual(data["kpis"]["runs"], {"executed": 1, "scheduled_runs": 1, "bot_loops": 0,
                                                "passes": 1, "skipped": 1, "failed": 0,
                                                "by_hour": {"11": 1}})
        self.assertEqual(data["kpis"]["pnl"]["daily"], -12.5)
        self.assertEqual(data["scheduler"]["status"], "NOT SCHEDULED")
        self.assertEqual(data["timeline_extras"][0]["kind"], "skipped")
        self.assertEqual(data["scanner"]["source"], "universe")  # no watchlist today
        days = [s["date"] for s in data["model"]["sessions"]]
        self.assertEqual(days, ["2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23",
                                "2026-10-26", "2026-10-27"])  # weekdays only, a week ahead

    def test_sync_site_rewrites_only_changed_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            self.assertIn("index.html", dash.sync_site(out))
            self.assertEqual(dash.sync_site(out), [])


if __name__ == "__main__":
    unittest.main()
