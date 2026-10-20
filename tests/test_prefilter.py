"""Offline tests for sp500_tickers.py, morning_prefilter.py and the watchlist merge.

No network: Wikipedia and Yahoo Finance are replaced with fixed data.
Run: python -m unittest discover -s tests -p test_prefilter.py -v
"""
import os

os.environ["PAPERBOT_NOTIFY"] = "off"  # tests must never send real notifications

import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

from paperbot.market import morning_prefilter as mp
from paperbot.market import sp500_tickers
from types import SimpleNamespace

from paperbot.core import common
from paperbot.strategies import rule_strategy as strategy

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
        self.patches = [mock.patch.object(strategy, "WATCHLIST_FILE", self.watchlist)]
        for p in self.patches:
            p.start()
        common._warned.clear()
        self.profile = SimpleNamespace(universe={"source": "watchlist", "index": "S&P 500",
                                                 "fallback_file": str(self.universe)})

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
        self.assertEqual(strategy._universe(self.profile, TODAY), ["BRK B"])

    def test_stale_or_missing_watchlist_falls_back_to_universe(self):
        self.assertEqual(strategy._universe(self.profile, TODAY), ["AAPL", "MSFT"])
        self.write(now=NOW - dt.timedelta(days=1))
        self.assertEqual(strategy._universe(self.profile, TODAY), ["AAPL", "MSFT"])


if __name__ == "__main__":
    unittest.main()
