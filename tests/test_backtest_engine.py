"""Backtest simulator: the REAL strategy engine (rule_strategy.decide + blocks + core/orders +
risk guard) driven over recorded bars; fills, accounting and risk controls.

Uses hand-built SYNTHETIC scenarios (tests/backtest_fixtures.py) whose expected fills are
worked out in each docstring. Default costs: half-spread 2 bps + slippage 2 bps = 4 bps
on market-like fills.
Run: python -m unittest discover -s tests -p "test_backtest_*.py" -v
"""
import os

os.environ["PAPERBOT_NOTIFY"] = "off"

import datetime as dt
import unittest
from unittest import mock

import backtest_fixtures as F

from paperbot.backtesting.data import MarketData, SyntheticProvider
from paperbot.backtesting.engine import Simulator, check_key, commission_per_side, exit_label
from paperbot.core import market_data as md
from paperbot.core import orders as core_orders
from paperbot.core import risk as core_risk
from paperbot.strategies import rule_strategy

LIMIT = 105.74  # to_tick(105.50 x 1.0002 x 1.002, BUY): marketable limit through the ask proxy
ENTRY = round(105.5 * 1.0004, 4)  # the 10:10 bar opens 105.50: fills at open + 4 bps
STOP = 101.97  # to_tick(LOD 103.00 x 0.99, STOP)
QTY = 283  # floor(30,000 / 105.74): the order-value cap binds
N = (105.5, 105.6, 105.4, 105.5)  # a quiet bar (e.g. the 10:05 bar, in which the entry is sent)


def by(rows, **kw):
    return [r for r in rows if all(r.get(k) == v for k, v in kw.items())]


class EntryTest(unittest.TestCase):
    def test_signal_fill_size_stop_and_commission(self):
        """The 10:05:03 pass sees the completed 10:00 breakout bar; the order (sent after the
        10:05 bar began) fills at the NEXT bar's open (10:10) plus 4 bps, sized by the
        $30,000 order cap."""
        r = F.simulate(F.scenario())
        self.assertEqual(len(r.trades), 1)
        t = r.trades[0]
        self.assertEqual(t["signal_time_et"], "2025-06-02T10:05:03-04:00")
        self.assertEqual(t["entry_time_et"], "2025-06-02T10:10:00-04:00")
        self.assertAlmostEqual(t["entry_price"], ENTRY, places=4)
        self.assertEqual(t["quantity"], QTY)
        self.assertEqual(t["initial_stop"], STOP)
        entry = by(r.orders, kind="entry")[0]
        self.assertEqual(entry["limit_price"], LIMIT)
        self.assertAlmostEqual(by(r.fills, side="BUY")[0]["commission"], commission_per_side(QTY, ENTRY))
        self.assertIn("I3 RVOL 3.00", t["entry_conditions"])
        self.assertEqual(r.funnel, {"pass": 1})

    def test_bracket_stop_is_child_of_entry(self):
        r = F.simulate(F.scenario())
        stop = by(r.orders, kind="stop")[0]
        self.assertEqual(stop["parent_id"], by(r.orders, kind="entry")[0]["order_id"])
        self.assertEqual(stop["stop_price"], STOP)

    def test_no_look_ahead_breakout_in_forming_bar(self):
        """A breakout in the 10:05 bar is only visible to the 10:10 pass (bar completed),
        so the entry fills at the 10:15 open, never earlier."""
        r = F.simulate(F.scenario(breakout={"AAA": 7}))
        self.assertEqual(r.trades[0]["entry_time_et"], "2025-06-02T10:15:00-04:00")

    def test_no_fill_from_prices_before_the_order(self):
        """The 10:05 bar, in which the order is sent (10:05:03), opens 105.00 and trades
        down to 104.00: those prices came before the order existed, so it cannot fill
        there; it fills at the 10:10 open (105.50)."""
        r = F.simulate(F.scenario(after={"AAA": [(105.0, 105.2, 104.0, 105.5)]}))
        buy = by(r.fills, side="BUY")[0]
        self.assertEqual(buy["time_et"], "2025-06-02T10:10:00-04:00")
        self.assertAlmostEqual(buy["price"], ENTRY, places=4)

    def test_entry_not_filled_when_price_stays_above_limit(self):
        """Price gaps above the limit and never trades below it: the GTD entry expires
        at 15:31 unfilled; no trade, no cash used, the child stop is cancelled with it."""
        after = [(106.5, 107, 106.0, 106.5)] * 70
        r = F.simulate(F.scenario(after={"AAA": after}))
        self.assertEqual(r.trades, [])
        self.assertEqual(by(r.orders, kind="entry")[0]["status"], "expired")
        self.assertEqual(by(r.orders, kind="stop")[0]["status"], "cancelled")
        self.assertEqual(r.final_equity, F.cfg()["starting_capital"])

    def test_limit_fills_only_when_traded_through(self):
        """10:10 bar opens 106.00 (above the 105.74 limit) and trades down to 105.70:
        the resting limit fills at exactly 105.74, with no extra slippage."""
        r = F.simulate(F.scenario(after={"AAA": [(106.0, 106.2, 105.9, 106.0), (106.0, 106.2, 105.7, 106.0)]}))
        self.assertEqual(by(r.fills, side="BUY")[0]["price"], LIMIT)

    def test_partial_entry_fill_under_volume_cap(self):
        """10:10 bar volume 1,000 x 10% participation = 100 shares; the remaining 183
        fill in the next bar. One order, so the $1 minimum applies once: 0.005 x 283."""
        r = F.simulate(F.scenario(after={"AAA": [N, (105.5, 105.6, 105.4, 105.5, 1000)]}))
        buys = by(r.fills, side="BUY")
        self.assertEqual([f["quantity"] for f in buys], [100, 183])
        self.assertIn("partial_entry_fill", r.trades[0]["warnings"])
        self.assertAlmostEqual(sum(f["commission"] for f in buys), 1.415, places=3)


class ExitTest(unittest.TestCase):
    def test_stop_fill_with_slippage(self):
        """Entry at the 10:10 open; the 10:15 bar trades down to 101.50: the 101.97 stop
        fills at 101.97 less 4 bps."""
        r = F.simulate(F.scenario(after={"AAA": [N, N, (104, 104, 101.5, 102)]}))
        t = r.trades[0]
        self.assertEqual(t["exit_reason"], "initial stop")
        self.assertAlmostEqual(t["exit_price"], round(STOP * 0.9996, 4), places=4)
        self.assertEqual(t["exit_time_et"], "2025-06-02T10:15:00-04:00")

    def test_gap_through_stop_fills_at_open(self):
        """10:15 bar OPENS at 101.00, below the stop: the fill is the (worse) open."""
        r = F.simulate(F.scenario(after={"AAA": [N, N, (101, 101.2, 100.5, 101)]}))
        t = r.trades[0]
        self.assertAlmostEqual(t["exit_price"], round(101.0 * 0.9996, 4), places=4)
        self.assertIn("gap_through_stop", t["warnings"])

    def test_force_close_sent_at_1551_fills_at_the_next_open(self):
        """The 15:51 pass sends the force-close SELL (limit from the 15:45 bar's close). The
        15:50 bar began before it, so its prices are not used: the order fills at the 15:55
        open (105.50) less 4 bps, before the 15:55:03 pass would replace it."""
        r = F.simulate(F.scenario())
        t = r.trades[0]
        self.assertEqual(t["exit_reason"], "force close")
        self.assertEqual(t["exit_time_et"], "2025-06-02T15:55:00-04:00")
        self.assertAlmostEqual(t["exit_price"], round(105.5 * 0.9996, 4), places=4)
        self.assertEqual(by(r.orders, kind="exit")[0]["placed_at_et"], "2025-06-02T15:51:00-04:00")

    def test_partial_profit_then_force_close(self):
        """10:15 bar closes 108.50 >= +0.75R (avg cost incl. fee 105.5472, R 3.5772): the
        10:20:03 pass shrinks the stop to 189 and sends SELL split_shares(283, 0.3333) = 94;
        it fills at the 10:25 open less 4 bps."""
        after = [N, N, (105.5, 108.6, 105.4, 108.5), (108.5, 108.6, 108.3, 108.5)]
        r = F.simulate(F.scenario(after={"AAA": after}))
        partial = [f for f in by(r.fills, kind="exit") if f["order_reason"].startswith("partial profit")]
        self.assertEqual(partial[0]["quantity"], 94)
        self.assertEqual(partial[0]["time_et"], "2025-06-02T10:25:00-04:00")
        self.assertAlmostEqual(partial[0]["price"], round(108.5 * 0.9996, 4), places=4)
        self.assertEqual(r.trades[0]["exit_reason"], "partial profit + force close")
        self.assertEqual(sum(f["quantity"] for f in by(r.fills, side="SELL")), QTY)

    def test_ambiguous_bar_assumes_stop_first(self):
        """The partial limit (sent 10:20:03) is still working when the 10:25 bar opens
        108.00, spikes to 109 and falls to 101: the order of high and low is unknown, so
        the (shrunk, 189-share) stop fills first and the partial does NOT fill in that bar
        (conservative). The bot replaces an exit after 60 s; that is switched off here so
        the partial is still working when the bar's range is reached."""
        after = [N, N, (105.5, 108.6, 105.4, 108.5), (108.0, 108.2, 107.9, 108.0), (108.0, 109.0, 101.0, 101.5)]
        with mock.patch.object(core_orders, "EXIT_STALE_SECONDS", 10 ** 6):
            r = F.simulate(F.scenario(after={"AAA": after}))
        t = (r.trades + r.open_trades)[0]  # the 94 partial shares stay open (no replacement)
        self.assertIn("ambiguous_bar", t["warnings"])
        self.assertEqual(by(r.fills, kind="exit"), [])
        stop = by(r.fills, kind="stop")
        self.assertEqual([(f["time_et"], f["quantity"]) for f in stop], [("2025-06-02T10:25:00-04:00", 189)])
        self.assertAlmostEqual(stop[0]["price"], round(STOP * 0.9996, 4), places=4)

    def test_breakeven_then_trailing_stop(self):
        """Price runs to 110 (> +1R): the real core/orders.raise_stops moves the stop to
        breakeven (105.54), then to the 5-minute swing low 108.00; the trail is hit."""
        after = [N, N, (105.5, 108.6, 105.4, 108.5), (108.5, 109.0, 108.3, 108.8),
                 (108.8, 110.0, 108.7, 110.0), (110, 110.2, 108.0, 108.4), (108.4, 109.0, 108.3, 108.9),
                 (108.9, 109.1, 108.5, 109.0), (109.0, 109.2, 108.6, 109.1), (109.1, 109.2, 107.5, 107.8)]
        r = F.simulate(F.scenario(after={"AAA": after}))
        t = r.trades[0]
        self.assertEqual(t["exit_reason"], "partial profit + trailing stop")
        self.assertIn('"new": 105.54', t["stop_history"])
        self.assertIn('"new": 108.0', t["stop_history"])
        self.assertGreater(t["net_pnl"], 0)


class AccountingTest(unittest.TestCase):
    def test_ledger_reconciles_with_equity(self):
        after = [N, N, (105.5, 108.6, 105.4, 108.5), (108.5, 108.6, 108.3, 108.5)]
        r = F.simulate(F.scenario(symbols=("AAA", "BBB"), after={"AAA": after}, scale={"BBB": 0.5}),
                       symbols=("AAA", "BBB"))
        capital = F.cfg()["starting_capital"]
        self.assertEqual(len(r.trades), 2)
        self.assertAlmostEqual(capital + sum(t["net_pnl"] for t in r.trades), r.final_equity, delta=0.05)
        self.assertAlmostEqual(sum(t["commission"] for t in r.trades),
                               sum(f["commission"] for f in r.fills), delta=0.02)
        self.assertEqual(r.equity[-1]["equity"], r.final_equity)

    def test_trade_pnl_by_hand(self):
        """Bought at the 10:10 open, force-closed at the 15:55 open, both 105.50 +/- 4 bps:
        gross = 283 x (105.4578 - 105.5422) = -23.885; fees 2 x 1.415 = 2.83; net -26.72."""
        t = F.simulate(F.scenario()).trades[0]
        self.assertAlmostEqual(t["gross_pnl"], -23.89, places=2)
        self.assertAlmostEqual(t["commission"], 2.83, places=2)
        self.assertAlmostEqual(t["net_pnl"], -26.72, places=2)

    def test_open_position_at_end_is_unrealized(self):
        """No bars after 15:45: the force close (sent 15:51) cannot fill and the position stays open; it is
        listed separately and valued at its last close, not counted as completed."""
        p = F.scenario()
        p._intraday["AAA"][F.TRADE_DAY] = [b for b in p._intraday["AAA"][F.TRADE_DAY]
                                           if b.date.astimezone(F.ET).time() < dt.time(15, 50)]
        r = F.simulate(p)
        self.assertEqual(r.trades, [])
        self.assertEqual(len(r.open_trades), 1)
        o = r.open_trades[0]
        self.assertEqual(o["status"], "open")
        self.assertAlmostEqual(o["unrealized_pnl"], round(QTY * (105.5 - ENTRY), 2), places=2)
        self.assertAlmostEqual(F.cfg()["starting_capital"] + o["net_pnl"], r.final_equity, delta=0.05)


class RiskControlTest(unittest.TestCase):
    def test_max_concurrent_positions(self):
        r = F.simulate(F.scenario(symbols=("AAA", "BBB"), scale={"BBB": 0.5}), symbols=("AAA", "BBB"),
                       prof=F.profile(max_concurrent_positions=1))
        self.assertEqual(len(r.trades), 1)

    def test_max_orders_per_day_blocks_by_the_live_guard(self):
        r = F.simulate(F.scenario(symbols=("AAA", "BBB"), scale={"BBB": 0.5}), symbols=("AAA", "BBB"),
                       prof=F.profile(max_orders_per_day=1))
        self.assertEqual(len(r.trades), 1)
        rejected = by(r.orders, status="rejected")
        self.assertTrue(any("already sent 1 entries today" in o["reject_reasons"] for o in rejected))

    def test_cash_limits_quantity(self):
        """$20,000 cash, buying power x1, size/risk caps lifted: floor(20,000 / 105.74) = 189."""
        r = F.simulate(F.scenario(), prof=F.profile(max_position_size_pct_of_portfolio=100,
                                                    max_risk_per_trade_pct=100),
                       c=F.cfg(starting_capital=20000.0))
        self.assertEqual(r.trades[0]["quantity"], 189)

    def test_daily_loss_halt_blocks_later_entries(self):
        """AAA stops out (~ -1,000 USD) at 10:15; with max_daily_loss 500 the guard halts, so
        BBB's 11:00 breakout is rejected."""
        after = {"AAA": [N, N, (104, 104, 101.5, 102)]}
        r = F.simulate(F.scenario(symbols=("AAA", "BBB"), after=after, breakout={"BBB": 18}),
                       symbols=("AAA", "BBB"), prof=F.profile(max_daily_loss=500.0))
        self.assertEqual([t["symbol"] for t in r.trades], ["AAA"])
        self.assertTrue(any("trading halted" in o["reject_reasons"] for o in by(r.orders, status="rejected")))
        self.assertTrue(r.daily[0]["halted"])

    def test_one_entry_per_symbol_per_day(self):
        """After a stop-out the symbol breaks out again but is not re-entered the same day."""
        after = [N, N, (104, 104, 101.5, 102)] + [(102, 106, 102, 106)] * 3
        r = F.simulate(F.scenario(after={"AAA": after}))
        self.assertEqual(len(r.trades), 1)


class WatchlistTest(unittest.TestCase):
    def test_symbols_near_the_minimum_price_are_reported(self):
        """The simulated prefilter screens min_price ($3) on the open, the live one on its
        latest print: AAA opens 3.12 (within 10 %), so it is counted as a limitation."""
        data = MarketData(F.scenario(scale={"AAA": 0.03}), None)
        data.load_daily(["AAA"], F.TRADE_DAY - dt.timedelta(days=500), F.TRADE_DAY)
        sim = Simulator(F.profile(), data, F.cfg(), ["AAA"], F.TRADE_DAY, F.TRADE_DAY)
        sim.run()
        self.assertEqual(sim.price_near_threshold, [["2025-06-02", "AAA", 3.12]])
        self.assertTrue(any("1 gapping symbol-day(s)" in x and "AAA 2025-06-02" in x
                            for x in data.quality.limitations))

    def test_zero_counted_when_no_open_is_near_the_minimum(self):
        data = MarketData(F.scenario(), None)
        data.load_daily(["AAA"], F.TRADE_DAY - dt.timedelta(days=500), F.TRADE_DAY)
        sim = Simulator(F.profile(), data, F.cfg(), ["AAA"], F.TRADE_DAY, F.TRADE_DAY)
        sim.run()
        self.assertEqual(sim.price_near_threshold, [])
        self.assertTrue(any("0 gapping symbol-day(s)" in x for x in data.quality.limitations))


class StopTimingTest(unittest.TestCase):
    def test_a_raised_stop_applies_from_the_next_bar(self):
        from paperbot.backtesting.engine import SimOrder
        t0 = dt.datetime(2025, 6, 2, 10, 5, tzinfo=F.ET)
        stop = SimOrder(1, "AAA", "stop", "SELL", 100, "", t0 - dt.timedelta(minutes=30), aux=100.0)
        stop.set_stop(102.0, t0 + dt.timedelta(seconds=3))
        self.assertEqual(stop.stop_at(t0), 100.0)  # the bar in which it was raised
        self.assertEqual(stop.stop_at(t0 + dt.timedelta(minutes=5)), 102.0)
        self.assertFalse(stop.eligible(t0 - dt.timedelta(minutes=35)))
        self.assertTrue(stop.eligible(t0))


class EnvironmentTest(unittest.TestCase):
    def test_live_modules_restored_after_a_simulation(self):
        """The clock, state paths and helpers are swapped only during a simulation."""
        before = (rule_strategy.dt, core_orders.dt, core_risk.dt, md.dt, rule_strategy.STATE,
                  rule_strategy.WATCHLIST_FILE, rule_strategy._profile, rule_strategy.log)
        F.simulate(F.scenario())
        after = (rule_strategy.dt, core_orders.dt, core_risk.dt, md.dt, rule_strategy.STATE,
                 rule_strategy.WATCHLIST_FILE, rule_strategy._profile, rule_strategy.log)
        self.assertEqual(before, after)
        self.assertIs(rule_strategy.dt, dt)

    def test_labels(self):
        self.assertEqual(check_key("D3 gap 2.1% < 3.0%"), "D3")
        self.assertEqual(check_key("fewer than 2 completed 5m bars today"), "fewer than")
        self.assertEqual(exit_label("exit (attempt 2): price 55.20 above SMA5 54.90"),
                         "exit signal: price above SMA")
        self.assertEqual(exit_label("force close (attempt 3)"), "force close")


class OtherStrategiesTest(unittest.TestCase):
    """The other rules.json strategies run through the same engine (SYNTHETIC prices:
    structural checks only, never evidence about the strategies)."""

    def run_strategy(self, key, symbols, start, end, bar):
        prov = SyntheticProvider()
        data = MarketData(prov, None, bar=bar)
        data.load_daily(symbols, start - dt.timedelta(days=420), end)
        return Simulator(F.profile(key), data, F.cfg(), symbols, start, end).run()

    def test_macro_swing_holds_overnight_and_never_force_closes(self):
        r = self.run_strategy("macro_swing_etf", ["XLK", "XLF", "XLE"], dt.date(2025, 3, 3),
                              dt.date(2025, 6, 27), "5m")
        trades = r.trades + r.open_trades
        self.assertTrue(trades, "expected at least one synthetic trade")
        self.assertTrue(any(t["exit_time_et"][:10] > t["entry_time_et"][:10] for t in r.trades))
        self.assertFalse(any("force close" in t["exit_reason"] for t in trades))
        capital = F.cfg()["starting_capital"]
        self.assertAlmostEqual(capital + sum(t["net_pnl"] for t in trades), r.final_equity, delta=0.1)

    def test_mean_reversion_is_flat_every_day_on_1m_bars(self):
        r = self.run_strategy("equity_mean_reversion", ["SPY"], dt.date(2025, 3, 3),
                              dt.date(2025, 3, 7), "1m")
        self.assertTrue(r.trades, "expected at least one synthetic trade")
        self.assertEqual(r.open_trades, [])
        for t in r.trades:
            self.assertEqual(t["entry_time_et"][:10], t["exit_time_et"][:10])
        self.assertEqual(len(r.equity), 5 * 390)


if __name__ == "__main__":
    unittest.main()
