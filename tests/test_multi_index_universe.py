"""Offline tests: multi-index universes, index members (live / cached), the broad
Yahoo screen, per-ticker run logs, and the ETF-only index mapping.

No network: Wikipedia, Yahoo Finance and IB Gateway are replaced with fixed data.
Run: python -m unittest discover -s tests -p test_multi_index_universe.py -v
"""
import os

os.environ["PAPERBOT_NOTIFY"] = "off"  # tests must never send real notifications

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from paperbot import paths
from paperbot.core import profile as profiles
from paperbot.core.risk import ConfigError
from paperbot.market import constituents
from paperbot.market import morning_prefilter as mp
from paperbot.market import universe
from paperbot.strategies import rule_strategy as strategy

ET = mp.ET
NOW = dt.datetime(2026, 10, 19, 9, 58, tzinfo=ET)
TODAY = NOW.date()
PREV = TODAY - dt.timedelta(days=3)
LIMITS = {"limits": {"allowed_symbols": ["*"], "limit_orders_only": True}}
FOUR = ["S&P 500", "Nasdaq-100", "S&P MidCap 400", "S&P SmallCap 600"]
SECTOR_ETFS = ["XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLRE", "XLC"]


def live_rules() -> dict:
    return json.loads(paths.RULES_FILE.read_text(encoding="utf-8"))


def load(rules: dict, key: str):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "rules.json"
        path.write_text(json.dumps(rules), encoding="utf-8")
        return profiles.load(key, path, LIMITS)


def with_universe(key: str, uni: dict) -> dict:
    rules = live_rules()
    rules["strategies"][key]["universe"] = uni
    return rules


class ConfigTest(unittest.TestCase):
    def test_live_rules_load_with_the_new_universes(self):
        loaded = profiles.load_all(config=LIMITS)
        self.assertEqual(list(loaded), ["trend_join_long", "macro_swing_etf",
                                        "equity_mean_reversion"])  # identifiers unchanged
        tjl = loaded["trend_join_long"]
        self.assertEqual(universe.indexes_of(tjl.universe), tuple(FOUR))
        self.assertEqual(tjl.universe["source"], "watchlist")
        self.assertEqual(tjl.universe["fallback_file"], "universe.txt")

    def test_all_four_indexes_recognised_in_any_order(self):
        p = load(with_universe("trend_join_long", {"source": "watchlist",
                                                   "indexes": list(reversed(FOUR))}),
                 "trend_join_long")
        self.assertEqual(p.universe["indexes"], FOUR)  # canonical order
        setup = universe.settings(p)
        self.assertEqual(setup.index, " + ".join(FOUR))
        self.assertTrue(setup.multi)

    def test_invalid_index_lists_fail_closed(self):
        for bad in ({"indexes": []}, {"indexes": "S&P 500"}, {"indexes": ["S&P 500", "S&P 500"]},
                    {"indexes": ["Russell 2000"]}, {"indexes": ["Dow Jones"]},
                    {"indexes": ["S&P 500"], "index": "S&P 500"}):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                load(with_universe("trend_join_long", {"source": "watchlist", **bad}),
                     "trend_join_long")

    def test_legacy_single_index_still_works(self):
        p = load(with_universe("trend_join_long", {"source": "watchlist", "index": "Nasdaq-100"}),
                 "trend_join_long")
        setup = universe.settings(p)
        self.assertEqual((setup.index, setup.indexes, setup.multi),
                         ("Nasdaq-100", ("Nasdaq-100",), False))

    def test_macro_swing_etf_gets_index_etfs_never_members(self):
        p = profiles.load("macro_swing_etf", config=LIMITS)
        self.assertEqual(p.universe["symbols"], SECTOR_ETFS + ["SPY", "QQQ", "MDY", "IJR", "IWM"])
        self.assertNotEqual(p.universe.get("source"), "watchlist")  # never the prefilter path
        self.assertEqual(strategy._universe(p, TODAY), p.universe["symbols"])

    def test_etf_mapping_dedupes_and_rejects_unknown(self):
        self.assertEqual(universe.etf_symbols("x", ["SPY", "XLK"], ["S&P 500", "Nasdaq-100"]),
                         ["SPY", "XLK", "QQQ"])
        with self.assertRaises(ConfigError):
            universe.etf_symbols("x", ["XLK"], ["Dow Jones"])

    def test_risk_limits_and_rules_unchanged_by_the_universe(self):
        for key in ("trend_join_long", "macro_swing_etf"):
            rules = live_rules()
            section = rules["strategies"][key]
            p = profiles.load(key, config=LIMITS)
            plain = load(with_universe(key, {"symbols": ["AAPL"]}), key)
            for field in ("limits", "entry", "stop", "manage", "exit", "time_filter",
                          "timeframe", "holding", "order_ref"):
                if field in ("entry", "stop", "manage", "exit"):
                    self.assertEqual(repr(getattr(p, field)).count("object"),
                                     repr(getattr(plain, field)).count("object"), field)
                    continue
                self.assertEqual(getattr(p, field), getattr(plain, field), f"{key}.{field}")
            self.assertEqual(p.raw["risk"], section["risk"])


class WatchlistMatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.watchlist = Path(self.tmp.name) / "watchlist.txt"
        self.fallback = Path(self.tmp.name) / "universe.txt"
        self.fallback.write_text("AAPL\nMSFT\n")
        patches = [mock.patch.object(strategy, "WATCHLIST_FILE", self.watchlist),
                   mock.patch.object(paths, "CONFIG_DIR", Path(self.tmp.name))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.profile = load(with_universe("trend_join_long", {"source": "watchlist",
                                                              "indexes": FOUR}), "trend_join_long")

    def write(self, index: str):
        mp.write_watchlist(self.watchlist, [{"symbol": "NVDA", "gap_pct": 4.0, "open": 1.0,
                                             "prev_close": 1.0}], 1, 100, 1.5, 3.0, NOW, index,
                           "PF-x")

    def test_watchlist_for_the_same_index_set_is_scanned(self):
        self.write(" + ".join(FOUR))
        self.assertEqual(strategy._universe(self.profile, TODAY), ["NVDA"])
        self.assertIn("# Run: PF-x", self.watchlist.read_text())

    def test_watchlist_for_another_index_set_falls_back(self):
        self.write("S&P 500")
        self.assertEqual(strategy._universe(self.profile, TODAY), ["AAPL", "MSFT"])

    def test_no_watchlist_today_uses_the_fallback_file(self):
        self.assertEqual(strategy._universe(self.profile, TODAY), ["AAPL", "MSFT"])


class ConstituentsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name)
        self.lists = {"S&P 500": ["AAPL", "MSFT", "BRK B"], "Nasdaq-100": ["AAPL", "NVDA"],
                      "S&P MidCap 400": ["FIVE", "bad$tick"], "S&P SmallCap 600": ["AAON"]}

    def fetch(self, index):
        value = self.lists[index]
        if isinstance(value, Exception):
            raise value
        return value

    def test_union_dedupes_and_remembers_the_indexes(self):
        found = constituents.resolve(FOUR, self.fetch, self.cache, NOW)
        self.assertEqual(found.symbols, ["AAON", "AAPL", "BRK B", "FIVE", "MSFT", "NVDA"])
        self.assertEqual(found.members["AAPL"], ("S&P 500", "Nasdaq-100"))
        self.assertEqual(list(found.invalid), ["bad$tick"])
        self.assertEqual(found.failed, {})
        self.assertTrue(constituents.cache_file(self.cache, "S&P 500").exists())

    def test_fresh_cache_used_when_live_fails(self):
        constituents.resolve(FOUR, self.fetch, self.cache, NOW - dt.timedelta(days=3))
        self.lists["Nasdaq-100"] = OSError("Wikipedia down")
        found = constituents.resolve(FOUR, self.fetch, self.cache, NOW)
        self.assertIn("NVDA", found.symbols)
        self.assertEqual(found.sources["Nasdaq-100"]["source"], "cache")
        self.assertTrue(any("cached" in a for a in found.alerts))

    def test_stale_cache_leaves_the_index_out_and_continues(self):
        constituents.resolve(FOUR, self.fetch, self.cache, NOW - dt.timedelta(days=8))
        self.lists["Nasdaq-100"] = OSError("Wikipedia down")
        found = constituents.resolve(FOUR, self.fetch, self.cache, NOW)
        self.assertNotIn("NVDA", found.symbols)  # only in the Nasdaq-100
        self.assertIn("AAPL", found.symbols)  # still from the S&P 500
        self.assertIn("too old", found.failed["Nasdaq-100"])

    def test_every_index_failing_returns_nothing(self):
        for index in FOUR:
            self.lists[index] = OSError("down")
        found = constituents.resolve(FOUR, self.fetch, self.cache, NOW)
        self.assertEqual(found.symbols, [])
        self.assertEqual(set(found.failed), set(FOUR))


class BroadScreenTest(unittest.TestCase):
    def test_price_filter_then_top_n_by_gap(self):
        rows = {"A": (100, 110, 110), "B": (100, 105, 105), "C": (1, 1.2, 1.2),
                "D": (100, 101, 101), "E": (100, 104, 104)}
        top, excluded, gaps = mp.broad_screen(rows, ["A", "B", "C", "D", "E", "F"], 3.0, 2)
        self.assertEqual(top, ["A", "B"])
        self.assertIn("below $3", excluded["C"])
        self.assertIn("not in the top 2", excluded["E"])
        self.assertIn("not in the top 2", excluded["D"])
        self.assertNotIn("F", excluded)  # no Yahoo data: reported by yahoo_daily
        self.assertAlmostEqual(gaps["A"], 10.0)


class MultiIndexRunTest(unittest.TestCase):
    """main() for a four-index strategy: members -> Yahoo -> top_n at IBKR -> watchlist."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.watchlist = Path(self.tmp.name) / "watchlist.txt"
        self.watchlist.write_text("# earlier list\nAAPL\n")
        self.setup = universe.Settings("trend_join_long", " + ".join(FOUR), 2,
                                       Path("universe.txt"), 3.0, 3.0, tuple(FOUR))
        self.lists = {"S&P 500": ["AAPL", "MSFT"], "Nasdaq-100": ["AAPL", "NVDA"],
                      "S&P MidCap 400": ["FIVE"], "S&P SmallCap 600": ["AAON", "PENNY"]}
        self.yahoo = {"AAPL": (100, 106, 106), "MSFT": (100, 101, 101), "NVDA": (100, 104, 104),
                      "FIVE": (100, 103, 103), "PENNY": (1, 1.5, 1.5)}
        self.pushed = []
        fake_now = mock.MagicMock(wraps=dt.datetime)
        fake_now.now.return_value = NOW
        self.fake_now = fake_now
        for p in (mock.patch.object(mp, "setup_for", return_value=self.setup),
                  mock.patch.object(mp, "get_live_index_tickers",
                                    side_effect=lambda index: self.lists[index]),
                  mock.patch.object(mp, "yahoo_daily", side_effect=lambda symbols, today: (
                      {s: r for s, r in self.yahoo.items() if s in symbols},
                      {"AAON": "no Yahoo bar for today (latest 2026-10-16)"})),
                  mock.patch.object(mp, "market_caps", side_effect=AssertionError("not used")),
                  mock.patch.object(mp, "WATCHLIST_PATH", self.watchlist),
                  mock.patch.object(mp.dt, "datetime", fake_now),
                  mock.patch.object(mp, "notify", side_effect=lambda *a: self.pushed.append(a)),
                  mock.patch("builtins.print")):
            p.start()
            self.addCleanup(p.stop)

    def run_log(self) -> dict[str, dict]:
        files = list((Path(self.tmp.name) / "prefilter_runs" / TODAY.isoformat()).glob("PF-*.jsonl"))
        self.assertEqual(len(files), 1)
        return {r["symbol"]: r for r in map(json.loads, files[0].read_text().splitlines())}

    def test_success(self):
        bars = {"AAPL": [mp.DailyBar(PREV, 100, 100), mp.DailyBar(TODAY, 106, 106)],
                "NVDA": [mp.DailyBar(PREV, 100, 100), mp.DailyBar(TODAY, 102, 102)]}
        with mock.patch.object(mp, "ibkr_daily_bars", return_value=(bars, {})) as ibkr:
            self.assertEqual(mp.main([]), 0)
        self.assertEqual(ibkr.call_args.args[0], ["AAPL", "NVDA"])  # only top_n reach IBKR
        text = self.watchlist.read_text()
        self.assertEqual(strategy.read_symbols(self.watchlist), ["AAPL"])
        self.assertIn("# Index: " + " + ".join(FOUR), text)
        self.assertRegex(text, r"# Run: PF-20261019-0958-[0-9a-f]{4}")
        rows = self.run_log()
        self.assertEqual(set(rows), {"AAPL", "MSFT", "NVDA", "FIVE", "AAON", "PENNY"})
        self.assertEqual(rows["AAPL"]["status"], "survivor")
        self.assertEqual(rows["AAPL"]["indexes"], ["S&P 500", "Nasdaq-100"])
        self.assertIn("gap +2.00% below 3%", rows["NVDA"]["reason"])
        self.assertIn("not in the top 2", rows["FIVE"]["reason"])
        self.assertIn("below $3", rows["PENNY"]["reason"])
        self.assertIn("no Yahoo bar", rows["AAON"]["reason"])
        run_id = text.split("# Run: ")[1].splitlines()[0]
        self.assertEqual({r["run_id"] for r in rows.values()}, {run_id})

    def test_one_index_down_continues_with_an_alert(self):
        self.lists["S&P SmallCap 600"] = None
        with mock.patch.object(mp, "get_live_index_tickers", side_effect=lambda index: (
                self.lists[index] if self.lists[index] is not None else
                (_ for _ in ()).throw(OSError("down")))), \
                mock.patch.object(mp, "ibkr_daily_bars", return_value=({}, {})) as ibkr:
            mp.main([])
        ibkr.assert_called_once()
        self.assertTrue(any(t.startswith("Prefilter ALERT") and "S&P SmallCap 600" in b
                            for t, b, _ in self.pushed))

    def test_holiday_does_nothing_and_pushes_nothing(self):
        self.fake_now.now.return_value = dt.datetime(2026, 11, 26, 9, 58, tzinfo=ET)
        with mock.patch.object(mp, "ibkr_daily_bars") as ibkr:
            self.assertEqual(mp.main([]), 0)
        ibkr.assert_not_called()
        self.assertEqual(self.pushed, [])
        self.assertEqual(self.watchlist.read_text(), "# earlier list\nAAPL\n")

    def test_failed_run_keeps_the_watchlist_and_still_logs(self):
        with mock.patch.object(mp, "ibkr_daily_bars",
                               side_effect=mp.IbkrUnavailable("IB Gateway not reachable")):
            self.assertEqual(mp.main([]), 1)
        self.assertEqual(self.watchlist.read_text(), "# earlier list\nAAPL\n")
        self.assertIn("IBKR unavailable", self.run_log()["AAPL"]["reason"])

    def test_invalid_symbols_logged_not_sent(self):
        self.lists["S&P 500"] = ["AAPL", "MSFT", "??"]
        with mock.patch.object(mp, "ibkr_daily_bars", return_value=({}, {})) as ibkr:
            mp.main(["--dry-run"])
        self.assertNotIn("??", ibkr.call_args.args[0])
        self.assertIn("not a valid ticker", self.run_log()["??"]["reason"])


if __name__ == "__main__":
    unittest.main()
