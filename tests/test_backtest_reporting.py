"""Backtest metrics, run folders, reports, experiments and dashboard links.

Runs use SYNTHETIC test data in a temporary folder; nothing is written to
backtesting/results/. Run: python -m unittest discover -s tests -p test_backtest_reporting.py -v
"""
import os

os.environ["PAPERBOT_NOTIFY"] = "off"

import csv
import json
import re
import tempfile
import unittest
from pathlib import Path

import backtest_fixtures as F

from paperbot import paths
from paperbot.backtesting import experiments, metrics, report, runner, runs
from paperbot.backtesting.strategies import discover

WEB = Path(report.__file__).resolve().parent.parent / "reporting" / "web"


def eq(values, day="2025-06-02"):
    return [{"time_et": f"{day}T10:{i:02d}:00-04:00", "equity": v, "open_positions": 0,
             "exposure_pct": 0.0} for i, v in enumerate(values)]


def tearDownModule():
    os.environ.pop("PAPERBOT_BACKTEST", None)  # run_one marks the process; don't leak into other tests


class MetricsTest(unittest.TestCase):
    def test_empty_run_reports_na_not_zero(self):
        m = metrics.compute([], [], eq([1000.0, 1000.0]), [], 1000.0)
        self.assertEqual(m["completed_trades"], 0)
        for key in ("win_rate_pct", "profit_factor", "avg_win", "expectancy", "mean_trade_ci95"):
            self.assertIsNone(m[key], key)
            self.assertIn("N/A", m["na_reasons"][key])

    def test_profit_factor_and_win_rate(self):
        trades = [{"net_pnl": v, "gross_pnl": v} for v in (100.0, 50.0, -25.0, 0.0)]
        m = metrics.compute(trades, [], eq([1000, 1125]), [], 1000.0)
        self.assertEqual(m["profit_factor"], 6.0)
        self.assertEqual((m["winning_trades"], m["losing_trades"], m["breakeven_trades"]), (2, 1, 1))
        self.assertEqual(m["win_rate_pct"], 50.0)
        self.assertIsNone(metrics.compute(trades[:2], [], eq([1000, 1150]), [], 1000.0)["profit_factor"])

    def test_drawdown_by_hand(self):
        self.assertEqual(metrics.drawdown([100, 120, 90, 130, 117]), (25.0, 30.0, 1, 2))

    def test_unrealized_separated(self):
        m = metrics.compute([], [{"unrealized_pnl": 40.0, "commission": 1.0}], eq([1000, 1039]), [], 1000.0)
        self.assertEqual((m["unrealized_pnl"], m["realized_pnl"], m["open_positions_end"]), (40.0, -1.0, 1))

    def test_periods_split_chronologically(self):
        days = [f"2025-01-{d:02d}" for d in range(1, 11)]
        split = metrics.split_days(days, {"in_sample": 0.6, "validation": 0.2, "out_of_sample": 0.2})
        self.assertEqual(split["in_sample"], days[:6])
        self.assertEqual(split["out_of_sample"], days[8:])


class RunFolderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.rules_before = paths.RULES_FILE.read_bytes()
        cls.spec = discover()[F.TJL]
        after = {"AAA": [(105.5, 105.6, 105.4, 105.5), (104, 104, 101.5, 102)]}
        cls.provider = F.scenario(symbols=("AAA", "BBB"), after=after, scale={"BBB": 0.5})
        cls.kw = dict(start=F.TRADE_DAY, end=F.TRADE_DAY, results_dir=cls.tmp / "results",
                      cache_dir=None, publish=False, config=F.LIMITS)
        cls.run_dir = runner.run_one(cls.spec, F.cfg(), cls.provider, symbols=["AAA", "BBB"], **cls.kw)

    def test_all_artifacts_written(self):
        for name in ("run_metadata.json", "config_snapshot.json", "trades.csv", "orders.csv",
                     "fills.csv", "equity_curve.csv", "daily_performance.csv", "metrics.json",
                     "validation.json", "execution.log", "trade_context.json"):
            self.assertTrue((self.run_dir / name).exists(), name)
        meta = json.loads((self.run_dir / "run_metadata.json").read_text())
        for key in ("run_id", "strategy_id", "strategy_version", "dataset_id", "started_at", "tape",
                    "starting_capital", "execution_assumptions", "cost_assumptions", "software", "code"):
            self.assertIn(key, meta)
        self.assertTrue(meta["synthetic_data"])
        self.assertEqual(meta["period"]["actual_start"], F.TRADE_DAY.isoformat())
        snap = json.loads((self.run_dir / "config_snapshot.json").read_text())
        self.assertEqual(snap["rules"], json.loads(self.rules_before)["strategies"][F.TJL])

    def test_trade_ledger_reconciles(self):
        checks = {c["key"]: c for c in json.loads((self.run_dir / "validation.json").read_text())["checks"]}
        for key in ("equity_reconciles", "fills_match_trades", "no_short", "rules_unchanged", "isolated"):
            self.assertEqual(checks[key]["status"], "pass", checks[key])
        with open(self.run_dir / "trades.csv", newline="") as f:
            trades = list(csv.DictReader(f))
        m = json.loads((self.run_dir / "metrics.json").read_text())
        self.assertEqual(len(trades), m["completed_trades"])
        self.assertAlmostEqual(sum(float(t["net_pnl"]) for t in trades), m["net_pnl"], delta=0.05)
        self.assertTrue(any(r["key"] == "stop" for r in m["rules_explained"]))

    def test_runs_never_overwrite(self):
        other = runner.run_one(self.spec, F.cfg(), self.provider, symbols=["AAA"], stress=False, **self.kw)
        self.assertNotEqual(other, self.run_dir)
        self.assertTrue((self.run_dir / "trades.csv").exists())
        self.assertTrue(any(len(u["runs"]) >= 1 for u in runs.usage(self.tmp / "results").values()))

    def test_failed_run_is_recorded(self):
        with self.assertRaises(runner.BacktestFailed) as ctx:
            runner.run_one(self.spec, F.cfg(), F.scenario(), symbols=["NODATA"], **self.kw)
        meta = json.loads((ctx.exception.run_dir / "run_metadata.json").read_text())
        self.assertEqual(meta["status"], "failed")
        self.assertIn("no daily data", meta["errors"][0])

    def test_html_report_and_dashboard_data(self):
        html = report.render_html("t", report.run_payload(self.run_dir), "run")
        self.assertIn('PBT.standalone("run"', html)
        self.assertNotIn("</script><", html.split("window.PAPERBOT_BT_PAYLOAD")[1].split(";</script>")[0])
        data = report.dashboard_data(self.tmp / "results")
        self.assertEqual({s["strategy_id"] for s in data["strategies"]}, set(discover()))
        self.assertTrue(data["details"])
        self.assertEqual(data["portfolio"]["status"], "simulated")

    def test_reports_contain_no_secrets_or_account_ids(self):
        text = "".join(p.read_text(encoding="utf-8", errors="ignore") for p in self.run_dir.iterdir() if p.is_file())
        self.assertIsNone(re.search(r"\bD[UF]\d{5,}\b", text))
        for secret in ("TELEGRAM", "NOTIFY_URL", '"host"', '"port"', '"connections"'):
            self.assertNotIn(secret, text)

    def test_production_rules_untouched(self):
        self.assertEqual(paths.RULES_FILE.read_bytes(), self.rules_before)


class ExperimentTest(unittest.TestCase):
    def test_apply_returns_copy_and_validates(self):
        rules = F.rules()
        section = rules["strategies"][F.TJL]
        rvol = next(i for i, b in enumerate(section["entry"]) if b["type"] == "min_rvol")
        variant = experiments.apply(section, {f"entry.{rvol}.min": 1.5})
        self.assertEqual(variant["entry"][rvol]["min"], 1.5)
        self.assertNotEqual(section["entry"][rvol]["min"], 1.5)
        with self.assertRaises(experiments.ExperimentError):
            experiments.apply(section, {"entry.99.min": 1})
        with self.assertRaises(experiments.ExperimentError):
            experiments.apply(section, {"time_filter.no_such_key": "10:00"})
        with self.assertRaises(experiments.ExperimentError):
            experiments.validate(rules, F.TJL, {"stop.type": "nonsense"}, F.LIMITS)

    def test_proposals_for_every_strategy_saved_outside_production(self):
        folder = Path(tempfile.mkdtemp())
        before = paths.RULES_FILE.read_bytes()
        rules = json.loads(before)
        for key in discover():
            proposals = experiments.propose(key, rules)
            self.assertTrue(proposals, key)
            for exp in proposals:
                self.assertEqual(exp["strategy_id"], key)
                experiments.save(exp, folder)
        self.assertTrue(list(folder.glob("EXP-*.json")))
        self.assertEqual(paths.RULES_FILE.read_bytes(), before)
        with self.assertRaises(experiments.ExperimentError):
            experiments.save({"id": "EXP-x", "strategy_id": F.TJL, "hypothesis": "x", "reason": "x",
                              "changes": {"a": 1}}, paths.CONFIG_DIR)

    def test_comparison_uses_same_dataset(self):
        tmp = Path(tempfile.mkdtemp())
        spec, prov = discover()[F.TJL], F.scenario()
        kw = dict(start=F.TRADE_DAY, end=F.TRADE_DAY, results_dir=tmp, cache_dir=None,
                  publish=False, config=F.LIMITS, stress=False)
        # the baseline asked for a symbol without data; the variant reuses the baseline's
        # usable symbols (as run_suite does): both must still count as the same dataset
        base = runner.run_one(spec, F.cfg(), prov, symbols=["AAA", "NODATA"], **kw)
        section = F.rules()["strategies"][F.TJL]
        be = next(i for i, b in enumerate(section["manage"]) if b["type"] == "breakeven")
        exp = {"id": "EXP-test", "strategy_id": F.TJL, "hypothesis": "h", "reason": "r",
               "changes": {f"manage.{be}.at_R": 1.5}}
        var = runner.run_one(spec, F.cfg(), prov, symbols=["AAA"], experiment=exp, kind="experiment", **kw)
        data = runs.read_json(runner.compare_runs(base, [var], F.cfg()))
        self.assertTrue(data["same_dataset"])
        self.assertEqual(data["labels"], ["Baseline", "EXP-test"])
        self.assertIn("approval", data)
        snap = json.loads((var / "config_snapshot.json").read_text())
        self.assertEqual(snap["rules"]["manage"][be]["at_R"], 1.5)


class DashboardLinksTest(unittest.TestCase):
    def test_every_backtesting_link_has_a_page(self):
        js = (WEB / "assets" / "bt.js").read_text(encoding="utf-8")
        nav = re.findall(r"\['(b[\w-]+\.html)', '[^']+'\]", js)
        self.assertGreaterEqual(len(nav), 10)
        for page in nav:
            html = (WEB / page).read_text(encoding="utf-8")
            self.assertIn(f"PBT.page('{page}')", html)
            self.assertIn("data/backtest.js", html)
            self.assertIn(f"PAGES['{page}']", js)

    def test_main_nav_has_backtesting(self):
        core = (WEB / "assets" / "core.js").read_text(encoding="utf-8")
        self.assertIn("['backtest.html', 'BACKTESTING']", core)


if __name__ == "__main__":
    unittest.main()
