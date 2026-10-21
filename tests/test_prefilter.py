"""Offline tests for sp500_tickers.py, universe.py, morning_prefilter.py and the watchlist merge.

No network: Wikipedia and Yahoo Finance are replaced with fixed data.
Run: python -m unittest discover -s tests -p test_prefilter.py -v
"""
import os

os.environ["PAPERBOT_NOTIFY"] = "off"  # tests must never send real notifications

import contextlib
import datetime as dt
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from paperbot import paths
from paperbot.market import morning_prefilter as mp
from paperbot.market import sp500_tickers, universe
from paperbot.trading import strategy
from paperbot.trading.risk import ConfigError

ET = mp.ET
TODAY = dt.date(2026, 10, 19)
NOW = dt.datetime(2026, 10, 19, 9, 45, tzinfo=ET)


def frame(rows: dict[str, tuple]) -> pd.DataFrame:
    """yf.download(group_by="ticker")-shaped data: {yahoo: (prev_close, open, last)}."""
    index = pd.DatetimeIndex([pd.Timestamp(TODAY - dt.timedelta(days=3)), pd.Timestamp(TODAY)])
    return pd.concat({sym: pd.DataFrame({"Open": [prev, opn], "Close": [prev, last]}, index=index)
                      for sym, (prev, opn, last) in rows.items()}, axis=1)


class TickerFormatTest(unittest.TestCase):
    def test_conversions(self):
        self.assertEqual(sp500_tickers.to_ibkr("BRK.B"), "BRK B")
        self.assertEqual(sp500_tickers.to_ibkr("bf-b"), "BF B")
        self.assertEqual(sp500_tickers.to_yahoo("BRK B"), "BRK-B")
        self.assertEqual(sp500_tickers.to_yahoo("AAPL"), "AAPL")

    def test_parses_wikipedia_table(self):
        rows = "".join(f"<tr><td>{s}</td><td>x</td></tr>"
                       for s in ["MSFT", "BRK.B", "AAPL"] + [f"Z{i:03d}" for i in range(500)])
        html = f"<table><tr><th>Symbol</th><th>Security</th></tr>{rows}</table>"
        response = mock.Mock(text=html, raise_for_status=lambda: None)
        with mock.patch.object(sp500_tickers.requests, "get", return_value=response) as get:
            symbols = sp500_tickers.get_live_sp500_tickers()
        self.assertIn("User-Agent", get.call_args.kwargs["headers"])
        self.assertEqual(symbols[:3], ["AAPL", "BRK B", "MSFT"])
        self.assertEqual(symbols, sorted(symbols))

    def test_rejects_short_table(self):
        html = "<table><tr><th>Symbol</th></tr><tr><td>AAPL</td></tr></table>"
        response = mock.Mock(text=html, raise_for_status=lambda: None)
        with mock.patch.object(sp500_tickers.requests, "get", return_value=response):
            with self.assertRaises(ValueError):
                sp500_tickers.get_live_sp500_tickers()


class IndexListTest(unittest.TestCase):
    def page(self, html):
        response = mock.Mock(text=html, raise_for_status=lambda: None)
        return mock.patch.object(sp500_tickers.requests, "get", return_value=response)

    def test_nasdaq100_members_from_their_own_table(self):
        rows = "".join(f"<tr><td>{s}</td><td>x</td></tr>" for s in
                       ["NVDA", "GOOGL", "GOOG"] + [f"N{i:03d}" for i in range(98)])
        html = ("<table><tr><th>Year</th><th>Level</th></tr><tr><td>1</td><td>2</td></tr></table>"
                f"<table><tr><th>Ticker</th><th>Company</th></tr>{rows}</table>")
        with self.page(html) as get:
            symbols = sp500_tickers.get_live_index_tickers("Nasdaq-100")
        self.assertEqual(get.call_args.args[0], sp500_tickers.NASDAQ100_URL)
        self.assertEqual(len(symbols), 101)
        self.assertEqual(symbols[:3], ["GOOG", "GOOGL", "N000"])

    def test_nasdaq100_short_or_missing_table_is_rejected(self):
        short = "<table><tr><th>Ticker</th></tr><tr><td>NVDA</td></tr></table>"
        other = "<table><tr><th>Symbol</th></tr><tr><td>NVDA</td></tr></table>"
        for html in (short, other):
            with self.page(html), self.assertRaises(ValueError):
                sp500_tickers.get_live_index_tickers("Nasdaq-100")

    def test_every_configurable_index_has_a_page(self):
        self.assertEqual(sorted(sp500_tickers.PAGES), sorted(universe.INDEXES))
        with self.assertRaises(KeyError):
            sp500_tickers.get_live_index_tickers("Russell 2000")


class UniverseSettingsTest(unittest.TestCase):
    RULES = {"universe_filters": {"index": "Nasdaq-100", "min_price_usd": 5.0, "top_n": 50,
                                  "fallback_file": "universe_nasdaq100.txt"},
             "daily_filters": {"D3_min_gap_pct_from_prior_close": 2.0}}

    @classmethod
    def with_uf(cls, **changes):
        return {**cls.RULES, "universe_filters": {**cls.RULES["universe_filters"], **changes}}

    def test_settings_from_rules(self):
        setup = universe.settings(self.RULES)
        self.assertEqual((setup.index, setup.top_n, setup.min_price, setup.min_gap),
                         ("Nasdaq-100", 50, 5.0, 2.0))
        self.assertEqual(setup.fallback_file, paths.CONFIG_DIR / "universe_nasdaq100.txt")
        self.assertEqual(setup.describe(), "Nasdaq-100 (top 50), gap >= 2%")

    def test_defaults_match_the_original_screen(self):
        setup = universe.settings({})
        self.assertEqual((setup.index, setup.top_n, setup.min_price, setup.min_gap,
                          setup.fallback_file.name), ("S&P 500", 100, 3.0, 3.0, "universe.txt"))

    def test_live_rules_still_screen_the_sp500_top_100_at_3pct(self):
        setup = universe.settings(json.loads(paths.RULES_FILE.read_text()))
        self.assertEqual((setup.index, setup.top_n, setup.min_gap, setup.min_price),
                         ("S&P 500", 100, 3.0, 3.0))
        self.assertTrue(setup.fallback_file.exists())

    def test_bad_values_fail_closed(self):
        cases = [(self.with_uf(index="QQQ"), "index 'QQQ' is not supported"),
                 (self.with_uf(top_n=0), "top_n must be a number greater than 0"),
                 (self.with_uf(top_n=10.5), "top_n must be a whole number"),
                 (self.with_uf(top_n=True), "top_n must be a number"),
                 (self.with_uf(fallback_file="../secrets.txt"), "fallback_file must be a file name"),
                 (self.with_uf(indx="S&P 500"), "unknown keys ['indx']"),
                 ({**self.RULES, "daily_filters": {"D3_min_gap_pct_from_prior_close": -1}},
                  "D3_min_gap_pct_from_prior_close must be a number 0 or more")]
        for rules, message in cases:
            with self.subTest(message=message), self.assertRaises(ConfigError) as caught:
                universe.settings(rules)
            self.assertIn(message, str(caught.exception))
        zero_gap = {**self.RULES, "daily_filters": {"D3_min_gap_pct_from_prior_close": 0}}
        self.assertEqual(universe.settings(zero_gap).min_gap, 0.0)  # gap filter switched off

    def test_require_refuses_to_start_with_an_alert(self):
        with self.assertLogs("paperbot", "ERROR") as logs, self.assertRaises(SystemExit):
            universe.require(self.with_uf(index="QQQ"))
        self.assertIn("ALERT refusing to start", logs.output[0])

    def test_nasdaq100_fallback_file_is_shipped(self):
        path = paths.CONFIG_DIR / "universe_nasdaq100.txt"
        symbols = strategy.read_symbols(path)
        self.assertGreaterEqual(len(symbols), 95)
        self.assertEqual(len(symbols), len(set(symbols)))
        self.assertIn("NVDA", symbols)


class PrefilterRunTest(unittest.TestCase):
    """main() end to end with Wikipedia and Yahoo replaced by fixed data."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.rules, self.watchlist = root / "rules.json", root / "watchlist.txt"
        fake_dt = mock.Mock(wraps=dt.datetime)
        fake_dt.now = lambda tz=None: NOW
        self.members = mock.Mock(return_value=["AAA", "BBB", "CCC"])
        data = frame({"AAA": (100, 102.5, 103),  # +2.5% gap
                      "BBB": (100, 104, 104),    # +4%
                      "CCC": (100, 100.5, 101)})  # +0.5%
        self.patches = [mock.patch.object(mp, "RULES_FILE", self.rules),
                        mock.patch.object(mp, "WATCHLIST_PATH", self.watchlist),
                        mock.patch.object(mp.dt, "datetime", fake_dt),
                        mock.patch.object(mp, "get_live_index_tickers", self.members),
                        mock.patch.object(mp, "market_caps",
                                          lambda symbols: {s: 1e9 - i for i, s in enumerate(symbols)}),
                        mock.patch.object(mp, "download", return_value=data),
                        mock.patch.object(mp, "push_summary")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def run_main(self, rules, *argv):
        self.rules.write_text(json.dumps(rules))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = mp.main(list(argv))
        return code, json.loads(out.getvalue()), err.getvalue()

    def test_index_and_gap_come_from_rules(self):
        code, summary, _ = self.run_main(UniverseSettingsTest.RULES)
        self.assertEqual(code, 0)
        self.members.assert_called_once_with("Nasdaq-100")
        self.assertEqual((summary["index"], summary["min_gap_pct"], summary["min_price"]),
                         ("Nasdaq-100", 2.0, 5.0))
        self.assertEqual(summary["top_20_survivors"], ["BBB (+4.00%)", "AAA (+2.50%)"])
        text = self.watchlist.read_text()
        self.assertIn("# Index: Nasdaq-100\n# Filters: gap >= 2%, price >= $5\n", text)
        self.assertEqual(strategy.watchlist_index(self.watchlist), "Nasdaq-100")
        self.assertEqual(strategy.watchlist_date(self.watchlist), TODAY)

    def test_command_line_overrides_the_rules_for_one_run(self):
        code, summary, _ = self.run_main(UniverseSettingsTest.RULES, "--min-gap", "3", "--dry-run")
        self.assertEqual((code, summary["min_gap_pct"]), (0, 3.0))
        self.assertEqual(summary["top_20_survivors"], ["BBB (+4.00%)"])
        self.assertFalse(self.watchlist.exists())  # --dry-run writes nothing

    def test_top_n_limits_the_screen(self):
        code, summary, _ = self.run_main(UniverseSettingsTest.with_uf(top_n=2), "--dry-run")
        self.assertEqual((code, summary["total_screened"]), (0, 2))

    def test_invalid_rules_stop_before_any_download(self):
        code, summary, err = self.run_main(UniverseSettingsTest.with_uf(index="QQQ"), "--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("not supported", summary["error"])
        self.assertIn("ALERT: prefilter refused", err)
        self.members.assert_not_called()
        self.assertFalse(self.watchlist.exists())


class ScreenTest(unittest.TestCase):
    def test_buckets_ranking_and_open_based_gap(self):
        data = frame({
            "AAA": (100, 105, 104),   # gap +5% -> survivor
            "BBB": (100, 110, 99),    # gap +10% from the open (faded since) -> survivor, ranked 1st
            "CCC": (100, 101, 120),   # +20% on the day but only +1% gap -> below_gap
            "DDD": (2.0, 2.5, 2.6),   # price < $3 -> below_price
            "BRK-B": (100, 104, 104),  # share class, Yahoo format in the data
        })
        result = mp.screen(data, ["AAA", "BBB", "CCC", "DDD", "BRK B", "EEE"], 3.0, 3.0, TODAY)
        self.assertEqual([s["symbol"] for s in result["survivors"]], ["BBB", "AAA", "BRK B"])
        self.assertEqual((result["below_gap"], result["below_price"]), (1, 1))
        self.assertEqual(len(result["failed"]), 1)  # EEE not in the data: KeyError -> failed
        self.assertTrue(result["failed"][0].startswith("EEE"))

    def test_stale_or_nan_rows_fail_without_crashing(self):
        data = frame({"AAA": (100, float("nan"), 104)})
        self.assertEqual(len(mp.screen(data, ["AAA"], 3, 3, TODAY)["failed"]), 1)
        self.assertEqual(len(mp.screen(frame({"AAA": (100, 105, 105)}), ["AAA"], 3, 3,
                                       TODAY + dt.timedelta(days=1))["failed"]), 1)

    def test_caps_at_max_survivors(self):
        data = frame({f"S{i:02d}": (100, 104 + i * 0.1, 105) for i in range(30)})
        result = mp.screen(data, [f"S{i:02d}" for i in range(30)], 3, 3, TODAY)
        self.assertEqual(len(result["survivors"]), mp.MAX_SURVIVORS)
        self.assertEqual(result["passed"], 30)
        self.assertEqual(result["survivors"][0]["symbol"], "S29")

    def test_degradation_alerts(self):
        self.assertEqual(mp.alerts(10, 100), [])
        self.assertIn("degradation (failed 30/100)", mp.alerts(30, 100)[0])
        self.assertIn("connection failure", mp.alerts(95, 100)[0])


class WatchlistMergeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.watchlist = Path(self.tmp.name) / "watchlist.txt"
        self.universe = Path(self.tmp.name) / "universe.txt"
        self.universe.write_text("AAPL\nMSFT\n")
        self.patches = [mock.patch.object(strategy, "WATCHLIST_FILE", self.watchlist),
                        mock.patch.object(strategy, "UNIVERSE_FILE", self.universe)]
        for p in self.patches:
            p.start()
        strategy._warned.clear()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def write(self, now=NOW):
        survivors = [{"symbol": "BRK B", "gap_pct": 4.2, "open": 480.0, "prev_close": 460.7}]
        mp.write_watchlist(self.watchlist, survivors, 1, 100, 3.0, 3.0, now)

    def test_watchlist_format_and_date(self):
        self.write()
        text = self.watchlist.read_text()
        self.assertTrue(text.startswith("# Auto-generated by morning_prefilter.py at "
                                        "2026-10-19 09:45 EDT"))
        self.assertIn("BRK B  # gap +4.20%  open $480.00  prev $460.70", text)
        self.assertEqual(strategy.watchlist_date(self.watchlist), TODAY)
        self.assertEqual(strategy.read_symbols(self.watchlist), ["BRK B"])

    def test_bot_scans_todays_watchlist(self):
        self.write()
        self.assertEqual(strategy._universe({}, TODAY), ["BRK B"])

    def test_stale_or_missing_watchlist_falls_back_to_universe(self):
        rules = {"universe_filters": {"index": "S&P 500"}}
        self.assertEqual(strategy._universe(rules, TODAY), ["AAPL", "MSFT"])
        self.write(now=NOW - dt.timedelta(days=1))
        self.assertEqual(strategy._universe(rules, TODAY), ["AAPL", "MSFT"])

    def test_watchlist_for_another_index_is_not_scanned(self):
        survivors = [{"symbol": "ARM", "gap_pct": 4.2, "open": 150.0, "prev_close": 143.9}]
        mp.write_watchlist(self.watchlist, survivors, 1, 100, 3.0, 3.0, NOW, "Nasdaq-100")
        sp500 = {"universe_filters": {"index": "S&P 500"}}
        with self.assertLogs("paperbot", "WARNING") as logs:
            self.assertEqual(strategy._universe(sp500, TODAY), ["AAPL", "MSFT"])
        self.assertIn("generated for Nasdaq-100 but rules.json screens S&P 500", logs.output[0])
        nasdaq = {"universe_filters": {"index": "Nasdaq-100"}}
        self.assertEqual(strategy._universe(nasdaq, TODAY), ["ARM"])

    def test_watchlist_without_an_index_line_counts_as_sp500(self):
        self.watchlist.write_text("# Auto-generated by morning_prefilter.py at 2026-10-19 09:45 EDT\n"
                                  "# Filters: gap >= 3%, price >= $3\nAMD\n")
        self.assertEqual(strategy.watchlist_index(self.watchlist), "S&P 500")
        self.assertEqual(strategy._universe({}, TODAY), ["AMD"])
        self.assertEqual(strategy.watchlist_index(Path(self.tmp.name) / "missing.txt"), "S&P 500")

    def test_configured_fallback_file_is_used(self):
        (Path(self.tmp.name) / "universe_nasdaq100.txt").write_text("# QQQ\nNVDA\nASML\n")
        rules = {"universe_filters": {"index": "Nasdaq-100",
                                      "fallback_file": "universe_nasdaq100.txt"}}
        with self.assertLogs("paperbot", "WARNING"):
            self.assertEqual(strategy._universe(rules, TODAY), ["NVDA", "ASML"])

    def test_missing_fallback_file_scans_nothing_with_a_warning(self):
        rules = {"universe_filters": {"index": "Nasdaq-100", "fallback_file": "nope.txt"}}
        with self.assertLogs("paperbot", "WARNING") as logs:
            self.assertEqual(strategy._universe(rules, TODAY), [])
        self.assertTrue(any("nope.txt missing" in line for line in logs.output))


if __name__ == "__main__":
    unittest.main()
