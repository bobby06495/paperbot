"""Offline tests for sp500_tickers.py, morning_prefilter.py and the watchlist merge.

No network: Wikipedia, Yahoo Finance and IB Gateway are replaced with fixed data.
Run: python -m unittest discover -s tests -p test_prefilter.py -v
"""
import os

os.environ["PAPERBOT_NOTIFY"] = "off"  # tests must never send real notifications

import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from paperbot.core.risk import ConfigError
from paperbot.market import morning_prefilter as mp
from paperbot.market import universe
from paperbot.market import sp500_tickers
from types import SimpleNamespace

from paperbot.core import common
from paperbot.strategies import rule_strategy as strategy

ET = mp.ET
TODAY = dt.date(2026, 10, 19)
NOW = dt.datetime(2026, 10, 19, 9, 45, tzinfo=ET)


PREV_SESSION = TODAY - dt.timedelta(days=3)  # the Friday before


def frame(rows: dict[str, tuple]) -> dict[str, list]:
    """ibkr_daily_bars-shaped data: {ibkr symbol: (prev_close, open, last)}."""
    return {sym: [mp.DailyBar(PREV_SESSION, prev, prev), mp.DailyBar(TODAY, opn, last)]
            for sym, (prev, opn, last) in rows.items()}


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
            "BRK B": (100, 104, 104),  # share class, IBKR format
        })
        result = mp.screen(data, ["AAA", "BBB", "CCC", "DDD", "BRK B", "EEE"], 3.0, 3.0, TODAY,
                           {"EEE": "contract not found at IBKR"})
        self.assertEqual([s["symbol"] for s in result["survivors"]], ["BBB", "AAA", "BRK B"])
        self.assertEqual((result["below_gap"], result["below_price"]), (1, 1))
        self.assertEqual(result["failed"], ["EEE: contract not found at IBKR"])

    def test_stale_or_nan_rows_fail_without_crashing(self):
        data = frame({"AAA": (100, float("nan"), 104)})
        self.assertEqual(len(mp.screen(data, ["AAA"], 3, 3, TODAY)["failed"]), 1)
        self.assertEqual(len(mp.screen(frame({"AAA": (100, 105, 105)}), ["AAA"], 3, 3,
                                       TODAY + dt.timedelta(days=1))["failed"]), 1)
        only_today = {"AAA": [mp.DailyBar(TODAY, 105, 105)]}
        self.assertIn("no previous session", mp.screen(only_today, ["AAA"], 3, 3, TODAY)["failed"][0])

    def test_gap_uses_the_last_session_before_today(self):
        """A missing session in between must not matter: the previous bar is used."""
        bars = {"AAA": [mp.DailyBar(PREV_SESSION - dt.timedelta(days=1), 50, 50),
                        mp.DailyBar(TODAY, 104, 104), mp.DailyBar(PREV_SESSION, 100, 100)]}
        survivor = mp.screen(bars, ["AAA"], 3, 3, TODAY)["survivors"][0]
        self.assertEqual(survivor["prev_close"], 100)
        self.assertAlmostEqual(survivor["gap_pct"], 4.0)

    def test_caps_at_max_survivors(self):
        data = frame({f"S{i:02d}": (100, 104 + i * 0.1, 105) for i in range(30)})
        result = mp.screen(data, [f"S{i:02d}" for i in range(30)], 3, 3, TODAY)
        self.assertEqual(len(result["survivors"]), mp.MAX_SURVIVORS)
        self.assertEqual(result["passed"], 30)
        self.assertEqual(result["survivors"][0]["symbol"], "S29")

    def test_degradation_alerts_and_failure_threshold(self):
        self.assertEqual(mp.alerts(10, 100), [])
        self.assertIn("degraded (failed 30/100)", mp.alerts(30, 100)[0])
        self.assertFalse(mp.failure(94, 100))
        self.assertTrue(mp.failure(95, 100))
        self.assertEqual(mp.alerts(95, 100), [])  # a failed run, not a degraded one


class PrefilterFailureTest(unittest.TestCase):
    """A failed run names the failing step and never overwrites the watchlist."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.watchlist = Path(tmp.name) / "watchlist.txt"
        self.watchlist.write_text("# earlier list\nAAPL\n")
        self.pushed = []
        setup = universe.Settings("trend_join_long", "S&P 500", 3, Path("universe.txt"), 3.0, 3.0)
        fake_now = mock.MagicMock(wraps=dt.datetime)
        fake_now.now.return_value = NOW
        for p in (mock.patch.object(mp, "setup_for", return_value=setup),
                  mock.patch.object(mp, "get_live_index_tickers", return_value=["A", "B", "C"]),
                  mock.patch.object(mp, "market_caps", return_value={"A": 3.0, "B": 2.0, "C": 1.0}),
                  mock.patch.object(mp, "WATCHLIST_PATH", self.watchlist),
                  mock.patch.object(mp.dt, "datetime", fake_now),
                  mock.patch.object(mp, "notify", side_effect=lambda *a: self.pushed.append(a)),
                  mock.patch("builtins.print")):
            p.start()
            self.addCleanup(p.stop)

    def run_and_check(self, needle: str):
        self.assertEqual(mp.main([]), 1)
        self.assertEqual(self.watchlist.read_text(), "# earlier list\nAAPL\n")
        (title, body, priority), = self.pushed
        self.assertTrue(title.startswith("Prefilter FAILED"))
        self.assertIn(needle, body)
        self.assertIn("previous list kept", body)
        self.assertEqual(priority, "high")

    def test_gateway_unreachable(self):
        with mock.patch.object(mp, "ibkr_daily_bars",
                               side_effect=mp.IbkrUnavailable("IB Gateway not reachable at h:1")):
            self.run_and_check("IB Gateway not reachable")

    def test_bad_connection_config(self):
        with mock.patch.object(mp, "ibkr_daily_bars", side_effect=ConfigError("bad port")):
            self.run_and_check("bad port")

    def test_price_data_missing_for_almost_every_symbol(self):
        errors = {s: "IBKR request timed out" for s in "ABC"}
        data = {"Z": [mp.DailyBar(TODAY, 1, 1)]}  # today's bar exists, just not for these
        with mock.patch.object(mp, "ibkr_daily_bars", return_value=(data, errors)):
            self.run_and_check("IBKR price data missing for 3/3 symbols (e.g. A: IBKR request timed out")

    def test_no_data_at_all_is_reported_as_missing_data(self):
        errors = {s: "no daily bars from IBKR" for s in "ABC"}
        with mock.patch.object(mp, "ibkr_daily_bars", return_value=({}, errors)):
            self.run_and_check("IBKR price data missing for 3/3 symbols (e.g. A: no daily bars")

    def test_no_bar_for_today_yet(self):
        data = {"A": [mp.DailyBar(PREV_SESSION, 1, 1)]}
        with mock.patch.object(mp, "ibkr_daily_bars", return_value=(data, {})):
            self.run_and_check("no IBKR daily bar for today yet")

    def test_wikipedia_and_yahoo_failures_name_their_step(self):
        with mock.patch.object(mp, "get_live_index_tickers", side_effect=OSError("down")):
            self.run_and_check("Wikipedia S&P 500 member list unavailable")
        self.pushed.clear()
        with mock.patch.object(mp, "market_caps", return_value={}):
            self.run_and_check("Yahoo Finance returned no market caps")

    def test_success_writes_the_watchlist(self):
        data = frame({"A": (100, 105, 105), "B": (100, 101, 101), "C": (100, 104, 104)})
        with mock.patch.object(mp, "ibkr_daily_bars", return_value=(data, {})):
            self.assertEqual(mp.main([]), 0)
        self.assertEqual(strategy.read_symbols(self.watchlist), ["A", "C"])
        self.assertIn("IBKR daily bars", self.watchlist.read_text())


class IbkrFetchTest(unittest.TestCase):
    """ibkr_daily_bars against a fake IB: read-only, own clientId, per-symbol errors."""

    def test_fetch(self):
        import asyncio

        class FakeIB:
            def __init__(self):
                self.connected_with = None
                self.disconnected = False

            def connect(self, host, port, **kw):
                self.connected_with = (host, port, kw)

            def run(self, coro):
                return asyncio.run(coro)

            def disconnect(self):
                self.disconnected = True

            async def qualifyContractsAsync(self, contract):
                contract.conId = 0 if contract.symbol == "NOPE" else 1

            async def reqHistoricalDataAsync(self, contract, *args, **kw):
                if contract.symbol == "EMPTY":
                    return []
                return [SimpleNamespace(date=PREV_SESSION, open=10, close=11),
                        SimpleNamespace(date=TODAY, open=12, close=13)]

        fake = FakeIB()
        cfg = {"mode": "paper", "connections": {"paper": {"host": "127.0.0.1", "port": 4002,
                                                          "client_id": 21}}}
        with mock.patch("ib_async.IB", return_value=fake):
            bars, errors = mp.ibkr_daily_bars(["AAPL", "NOPE", "EMPTY"], cfg)
        host, port, kw = fake.connected_with
        self.assertEqual((host, port, kw["clientId"], kw["readonly"]), ("127.0.0.1", 4002, 25, True))
        self.assertTrue(fake.disconnected)
        self.assertEqual(bars["AAPL"][-1], mp.DailyBar(TODAY, 12.0, 13.0))
        self.assertEqual(errors, {"NOPE": "contract not found at IBKR",
                                  "EMPTY": "no daily bars from IBKR (timed out or no data permission)"})

    def test_unreachable_gateway_raises(self):
        fake = mock.Mock()
        fake.connect.side_effect = ConnectionRefusedError("refused")
        cfg = {"mode": "paper", "connections": {"paper": {"host": "127.0.0.1", "port": 4002,
                                                          "client_id": 21}}}
        with mock.patch("ib_async.IB", return_value=fake), self.assertRaises(mp.IbkrUnavailable):
            mp.ibkr_daily_bars(["AAPL"], cfg)


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
