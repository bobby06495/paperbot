"""Index switching for watchlist strategies: S&P 500 or Nasdaq-100 (rules.json universe).

Covers the Wikipedia member lists, the rules.json validation (fail closed), the
prefilter settings taken from the strategy, the watchlist header and the bot
ignoring a watchlist built for another index. Offline: network calls are mocked.
Run: python -m unittest discover -s tests -p test_index_switching.py -v
"""
import os

os.environ["PAPERBOT_NOTIFY"] = "off"

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock


from paperbot import paths
from paperbot.core import common
from paperbot.core import profile as profiles
from paperbot.core.risk import ConfigError
from paperbot.market import morning_prefilter as mp
from paperbot.market import sp500_tickers, universe
from paperbot.strategies import rule_strategy as strategy

ET = mp.ET
NOW = dt.datetime(2026, 10, 19, 9, 58, tzinfo=ET)
TODAY = NOW.date()
LIMITS = {"limits": {"allowed_symbols": ["*"], "limit_orders_only": True}}
NASDAQ_PAGE = """<table><tr><th>Company</th><th>Ticker</th></tr>{rows}</table>
<table><tr><th>Year</th><th>Change</th></tr><tr><td>2024</td><td>x</td></tr></table>"""


def rules_with(**universe_keys) -> dict:
    rules = json.loads(paths.RULES_FILE.read_text(encoding="utf-8"))
    rules["strategies"]["trend_join_long"]["universe"].update(universe_keys)
    return rules


def load(rules: dict, key: str = "trend_join_long"):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "rules.json"
        path.write_text(json.dumps(rules), encoding="utf-8")
        return profiles.load(key, path, LIMITS)


class TickersTest(unittest.TestCase):
    def test_nasdaq_page_parsed_from_its_ticker_table(self):
        rows = "".join(f"<tr><td>Co {i}</td><td>T{i:03d}</td></tr>" for i in range(100)) + \
            "<tr><td>Alphabet</td><td>GOOG.L</td></tr>"
        response = NS(text=NASDAQ_PAGE.format(rows=rows), raise_for_status=lambda: None)
        with mock.patch.object(sp500_tickers.requests, "get", return_value=response) as get:
            symbols = sp500_tickers.get_live_index_tickers("Nasdaq-100")
        self.assertIn("List_of_NASDAQ-100_companies", get.call_args.args[0])
        self.assertEqual(len(symbols), 101)
        self.assertIn("GOOG L", symbols)  # share class in IBKR format

    def test_short_or_unknown_index_rejected(self):
        response = NS(text=NASDAQ_PAGE.format(rows="<tr><td>A</td><td>AAPL</td></tr>"),
                      raise_for_status=lambda: None)
        with mock.patch.object(sp500_tickers.requests, "get", return_value=response):
            with self.assertRaises(ValueError):
                sp500_tickers.get_live_index_tickers("Nasdaq-100")
        with self.assertRaises(KeyError):
            sp500_tickers.get_live_index_tickers("Dow Jones")


class RulesValidationTest(unittest.TestCase):
    def test_defaults_filled_in(self):
        profile = load(rules_with())
        self.assertEqual(profile.universe["index"], "S&P 500")
        self.assertEqual(profile.universe["top_n"], 100)
        self.assertEqual(profile.universe["fallback_file"], "universe.txt")

    def test_nasdaq_accepted(self):
        profile = load(rules_with(index="Nasdaq-100", top_n=101, fallback_file="universe_nasdaq100.txt"))
        setup = universe.settings(profile)
        self.assertEqual((setup.index, setup.top_n, setup.fallback_file.name),
                         ("Nasdaq-100", 101, "universe_nasdaq100.txt"))
        self.assertEqual((setup.min_gap, setup.min_price), (3.0, 3.0))  # the strategy's own blocks
        self.assertTrue(setup.fallback_file.exists())

    def test_invalid_settings_fail_closed(self):
        for bad in ({"index": "Dow Jones"}, {"top_n": 0}, {"top_n": True}, {"top_n": 2.5},
                    {"fallback_file": ""}, {"indx": "S&P 500"}):
            with self.subTest(bad=bad), self.assertRaises(ConfigError):
                load(rules_with(**bad))

    def test_prefilter_serves_the_watchlist_strategy(self):
        """Even when the saved (running) strategy does not use the watchlist."""
        with mock.patch("paperbot.core.selector.saved", return_value="macro_swing_etf"):
            profile = universe.prefilter_profile(config=LIMITS)
        self.assertEqual(profile.key, "trend_join_long")


class WatchlistIndexTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.watchlist = Path(self.tmp.name) / "watchlist.txt"
        fallback = Path(self.tmp.name) / "fallback.txt"
        fallback.write_text("AAPL\nMSFT\n")
        self.patch = mock.patch.object(strategy, "WATCHLIST_FILE", self.watchlist)
        self.patch.start()
        common._warned.clear()
        self.nasdaq = NS(universe={"source": "watchlist", "index": "Nasdaq-100",
                                   "fallback_file": str(fallback)})
        self.sp500 = NS(universe={"source": "watchlist", "index": "S&P 500",
                                  "fallback_file": str(fallback)})

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def write(self, index):
        survivors = [{"symbol": "NVDA", "gap_pct": 4.2, "open": 180.0, "prev_close": 172.7}]
        mp.write_watchlist(self.watchlist, survivors, 1, 101, 3.0, 3.0, NOW, index)

    def test_header_records_the_index(self):
        self.write("Nasdaq-100")
        self.assertIn("# Index: Nasdaq-100", self.watchlist.read_text())
        self.assertEqual(strategy.watchlist_index(self.watchlist), "Nasdaq-100")
        self.assertEqual(strategy.watchlist_date(self.watchlist), TODAY)

    def test_older_watchlist_without_index_counts_as_sp500(self):
        self.watchlist.write_text(f"# Auto-generated by morning_prefilter.py at {NOW:%Y-%m-%d %H:%M %Z}\nNVDA\n")
        self.assertEqual(strategy.watchlist_index(self.watchlist), "S&P 500")

    def test_bot_uses_watchlist_only_for_its_own_index(self):
        self.write("Nasdaq-100")
        self.assertEqual(strategy._universe(self.nasdaq, TODAY), ["NVDA"])
        self.assertEqual(strategy._universe(self.sp500, TODAY), ["AAPL", "MSFT"])  # fallback


class PrefilterRunTest(unittest.TestCase):
    def test_screens_the_strategy_index_and_top_n(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        watchlist = Path(tmp.name) / "watchlist.txt"
        setup = universe.Settings("trend_join_long", "Nasdaq-100", 2, paths.CONFIG_DIR / "universe_nasdaq100.txt",
                                  4.0, 5.0)
        data = {"MSFT": [mp.DailyBar(NOW.date(), 1.0, 1.0)]}
        screened = {"survivors": [{"symbol": "MSFT", "gap_pct": 4.5, "open": 1.0, "prev_close": 1.0}],
                    "passed": 1, "below_gap": 1, "below_price": 0, "failed": []}
        fake_now = mock.MagicMock(wraps=dt.datetime)
        fake_now.now.return_value = NOW
        with mock.patch.object(mp, "setup_for", return_value=setup), \
                mock.patch.object(mp, "get_live_index_tickers", return_value=["AAPL", "MSFT", "NVDA"]) as members, \
                mock.patch.object(mp, "market_caps", return_value={"AAPL": 3.0, "MSFT": 2.0, "NVDA": 1.0}), \
                mock.patch.object(mp, "ibkr_daily_bars", return_value=(data, {})) as download, \
                mock.patch.object(mp, "screen", return_value=screened) as screen, \
                mock.patch.object(mp, "WATCHLIST_PATH", watchlist), \
                mock.patch.object(mp.dt, "datetime", fake_now), \
                mock.patch("builtins.print"):
            code = mp.main([])
        self.assertEqual(code, 0)
        members.assert_called_once_with("Nasdaq-100")
        self.assertEqual(download.call_args.args[0], ["AAPL", "MSFT"])  # top_n = 2 by market cap
        self.assertEqual(screen.call_args.args[2:4], (4.0, 5.0))  # thresholds from the strategy
        self.assertIn("# Index: Nasdaq-100", watchlist.read_text())

    def test_refuses_when_rules_are_invalid(self):
        with mock.patch.object(mp, "setup_for", side_effect=ConfigError("bad index")), \
                mock.patch("builtins.print"):
            self.assertEqual(mp.main(["--dry-run"]), 1)


if __name__ == "__main__":
    unittest.main()
