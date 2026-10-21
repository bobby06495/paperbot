"""Backtest data: validation of bars, the on-disk cache, bar aggregation and the synthetic
provider. SYNTHETIC test bars only.
Run: python -m unittest discover -s tests -p test_backtest_data.py -v
"""
import os

os.environ["PAPERBOT_NOTIFY"] = "off"

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

import backtest_fixtures as F

from paperbot.backtesting.data import (Bar, DataCache, DataQuality, InMemoryProvider, MarketData,
                                       SyntheticProvider, aggregate, check_session, clean_series)
from paperbot.core.common import ET as CORE_ET

DAY = F.TRADE_DAY


def bar(h, m, price=100.0, **kw):
    v = {"open": price, "high": price, "low": price, "close": price, "volume": 100.0, **kw}
    return Bar(F.t(DAY, h, m), v["open"], v["high"], v["low"], v["close"], v["volume"])


class ValidationTest(unittest.TestCase):
    def test_duplicates_removed_and_counted(self):
        q = DataQuality("test")
        out = clean_series("X", [bar(9, 30), bar(9, 30, 101.0), bar(9, 35)], q, intraday=True)
        self.assertEqual(len(out), 2)
        self.assertEqual(q.duplicate_bars, 1)

    def test_unordered_series_sorted_and_reported(self):
        q = DataQuality("test")
        out = clean_series("X", [bar(9, 40), bar(9, 30), bar(9, 35)], q, intraday=True)
        self.assertEqual([b.date for b in out], sorted(b.date for b in out))
        self.assertEqual(q.unordered_series, 1)
        self.assertTrue(q.warnings)

    def test_invalid_prices_dropped(self):
        q = DataQuality("test")
        bad = [bar(9, 30, open=-1), bar(9, 35, high=90, low=95), bar(9, 40, close=float("nan")),
               bar(9, 45, volume=-5), bar(9, 50)]
        self.assertEqual(len(clean_series("X", bad, q, intraday=True)), 1)
        self.assertEqual(q.invalid_bars, 4)

    def test_off_grid_timestamp_dropped(self):
        q = DataQuality("test")
        odd = Bar(F.t(DAY, 9, 31), 100, 100, 100, 100, 10)
        self.assertEqual(clean_series("X", [odd, bar(9, 30)], q, intraday=True), [bar(9, 30)])
        self.assertEqual(q.off_grid_bars, 1)
        q1 = DataQuality("test")  # a 1-minute bar at 09:31 is on its own grid
        self.assertEqual(len(clean_series("X", [odd], q1, intraday=True, bar_minutes=1)), 1)

    def test_missing_session_bars_reported(self):
        q = DataQuality("test")
        check_session("X", DAY, [bar(9, 30), bar(9, 35)], q)
        self.assertEqual(q.incomplete_sessions, [["X", DAY.isoformat(), 2]])
        self.assertEqual(q.status, "warn")

    def test_missing_days_reported_not_filled(self):
        """A day the provider has no data for stays empty and is listed as missing."""
        md = MarketData(InMemoryProvider({}, {"X": {}}), None)
        self.assertEqual(md.day_bars("X", DAY), [])
        self.assertEqual(md.quality.intraday_days_missing, [["X", DAY.isoformat()]])

    def test_unknown_symbol_is_a_warning_not_a_failure(self):
        """A delisted/renamed symbol the provider cannot find is reported and skipped."""
        from paperbot.backtesting.data import SymbolNotFound

        class Picky(InMemoryProvider):
            def daily(self, symbol, start, end):
                if symbol == "GONE":
                    raise SymbolNotFound("IBKR could not qualify GONE")
                return super().daily(symbol, start, end)

        md = MarketData(Picky({"X": [Bar(DAY, 1, 2, 0.5, 1.5, 10)]}, {}), None)
        md.load_daily(["GONE", "X"], DAY, DAY)
        self.assertEqual(len(md.daily("X")), 1)
        self.assertTrue(any("GONE" in w for w in md.quality.warnings))

    def test_bars_are_kept_in_the_core_eastern_zone(self):
        md = MarketData(InMemoryProvider({}, {"X": {DAY: [bar(9, 30)]}}), None)
        self.assertIs(md.day_bars("X", DAY)[0].date.tzinfo, CORE_ET)

    def test_synthetic_provider_always_labelled(self):
        md = MarketData(InMemoryProvider({}, {}), None)
        self.assertTrue(md.quality.synthetic)
        self.assertEqual(md.quality.status, "warn")


class AggregationTest(unittest.TestCase):
    def test_one_minute_to_five_minutes(self):
        ones = [Bar(F.t(DAY, 9, 30 + i), 100 + i, 101 + i, 99 + i, 100.5 + i, 10) for i in range(10)]
        fives = aggregate(ones, 5)
        self.assertEqual(len(fives), 2)
        self.assertEqual((fives[0].open, fives[0].high, fives[0].low, fives[0].close, fives[0].volume),
                         (100, 105, 99, 104.5, 50))

    def test_synthetic_daily_matches_its_intraday(self):
        """Daily OHLC of the synthetic provider is built from its own intraday path."""
        prov = SyntheticProvider()
        daily = prov.daily("AAA", DAY, DAY)[0]
        rth = [b for b in prov.intraday("AAA", [DAY], "5m")[DAY] if b.date.astimezone(F.ET).time() >= dt.time(9, 30)]
        self.assertEqual(len(rth), 78)
        self.assertEqual((daily.open, daily.close), (rth[0].open, rth[-1].close))
        self.assertEqual(daily.high, max(b.high for b in rth))


class RealLikeProvider(InMemoryProvider):
    """In-memory bars that the cache treats like a real provider (cached on disk)."""
    name = "fakefeed"
    synthetic = False


class CacheTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.bars = [bar(9, 30), bar(9, 35, 101.0)]
        self.provider = RealLikeProvider({"X": [Bar(DAY, 1, 2, 0.5, 1.5, 10)]}, {"X": {DAY: self.bars}})

    def test_round_trip_and_no_refetch(self):
        cache = DataCache(self.dir, self.provider)
        got, hashes = cache.intraday("X", [DAY])
        self.assertEqual(got[DAY], self.bars)
        calls = len(self.provider.requests)
        cache.intraday("X", [DAY])
        self.assertEqual(len(self.provider.requests), calls)  # served from disk
        daily, h = cache.daily("X", DAY, DAY)
        self.assertEqual(daily[0].close, 1.5)
        self.assertTrue(h)

    def test_bar_sizes_cached_separately(self):
        DataCache(self.dir, self.provider, bar="5m").intraday("X", [DAY])
        DataCache(self.dir, self.provider, bar="1m").intraday("X", [DAY])
        self.assertTrue((self.dir / "fakefeed" / "5m" / "X" / "_index.json").exists())
        self.assertTrue((self.dir / "fakefeed" / "1m" / "X" / "_index.json").exists())

    def test_corrections_detected_on_refresh(self):
        DataCache(self.dir, self.provider).intraday("X", [DAY])
        self.provider._intraday["X"][DAY] = [bar(9, 30), bar(9, 35, 102.0)]  # provider corrected a bar
        DataCache(self.dir, self.provider, refresh=True).intraday("X", [DAY])
        changes = (self.dir / "fakefeed" / "changes.jsonl").read_text().splitlines()
        self.assertEqual(json.loads(changes[0])["day"], DAY.isoformat())

    def test_today_never_cached(self):
        today = dt.datetime.now(F.ET).date()
        got, _ = DataCache(self.dir, self.provider).intraday("X", [today])
        self.assertEqual(got, {})


if __name__ == "__main__":
    unittest.main()
