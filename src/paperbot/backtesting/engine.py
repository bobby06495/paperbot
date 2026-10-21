"""Historical simulator that runs paperbot's REAL strategy code against recorded bars.

Nothing about a strategy is re-implemented here. Every simulated pass calls

    paperbot.strategies.rule_strategy.decide(ib, account)

exactly as the live runner does: the profile's entry blocks (blocks/entry.py), its
stop block, the shared position machinery in core/orders.py (stop reconcile,
breakeven, swing-low trail, partial profit with retries, force close, signal exits)
and core/sizing.py. Every request it returns goes through the live
core/risk.RiskGuard (daily entry cap, open-order cap, daily-loss halt, drawdown
breaker, concurrency, buying power, commission drag). Any strategy in rules.json
can be simulated: 1m/5m/15m/1h intraday or 1d multi-day.

What this module adds is what a backtest must model instead of IBKR:

  SimIB         the `ib` object decide() reads: positions, open orders, historical
                bars (only bars that existed at the simulated time), live quotes
                (bid/ask proxy from the last completed bar). Its order methods raise.
  the clock     inside a simulation, the strategy modules' `dt` is a shim whose
                datetime.now() returns the simulated time (patched only in this
                process, only while a simulation runs; restored afterwards)
  the broker    requests become simulated orders, filled against the next bars

Timing: one pass per bar of the tape (the strategy's timeframe; 5m for a 1d
strategy), at bar end + decision_delay_seconds, plus one pass at force_close_et
when it is not on the bar grid. A pass sees only completed bars. Orders it sends
fill only in bars that start after it (SimOrder.eligible): each bar is processed
as its open (orders already working), then the passes inside it, then its range
(orders still working); a stop raised by a pass applies from the next bar.

Fill model (bps = 1/100 %; quote proxy: ask/bid = last close +/- half_spread):
  BUY LMT (entry)   open <= limit: min(limit, open x (1 + half_spread + slippage));
                    else a later bar trading BELOW the limit fills at the limit;
                    at most participation_rate x bar volume per bar; GTD expiry
  SELL LMT (exits)  open >= limit: max(limit, open x (1 - costs)); else high > limit:
                    the limit; otherwise it keeps working until the strategy
                    cancels / re-prices it (as live)
  STP (stops)       open <= stop: open x (1 - costs) (gap); else low <= stop:
                    stop x (1 - costs). Active once the parent entry has filled.
  OCA               a filled exit reduces its OCA stop and vice versa (ocaType 2)
  ambiguous bar     stop and a working exit both reachable, order unknown:
                    the stop is assumed first (conservative), flagged
  commission        IBKR Fixed per order (core/risk.round_trip_commission / 2)
"""
import bisect
import contextlib
import datetime as dt
import json
import logging
import re
import tempfile
import types
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

from paperbot.backtesting.data import BAR_MINUTES, ET, UTC, MarketData
from paperbot.core import common as core_common
from paperbot.core import market_data as md
from paperbot.core import orders as core_orders
from paperbot.core import risk as core_risk
from paperbot.core import sizing as core_sizing
from paperbot.core.common import RTH_CLOSE, RTH_OPEN
from paperbot.core.risk import (AccountState, CancelRequest, OrderIntent, RiskGuard, StopUpdate,
                                round_trip_commission)
from paperbot.runtime import runstate
from paperbot.strategies import rule_strategy

SIM_ACCOUNT = "SIMULATED"
PRICE_NEAR = 0.10  # an open within 10 % of min_price may be screened differently live
log = logging.getLogger("paperbot.backtest")


class BacktestError(RuntimeError):
    pass


def _bps(x: float) -> float:
    return x / 10_000


def commission_per_side(quantity: int, price: float) -> float:
    """IBKR Fixed commission for one order (core/risk's round-trip estimate / 2)."""
    return round_trip_commission(quantity, price) / 2


# ---------------------------------------------------------------- simulated broker objects

class SimOrder:
    """A simulated order that also looks like an ib_async Trade to core/orders.py
    (t.order.orderRef / orderId / totalQuantity / auxPrice / ocaGroup / action,
    t.orderStatus.filled, t.log[0].time, t.isDone(), t.contract.symbol)."""

    def __init__(self, order_id: int, symbol: str, role: str, action: str, quantity: int,
                 ref: str, placed_at: dt.datetime, limit: float | None = None,
                 aux: float | None = None, good_till=None, reason: str = "", oca: str = "",
                 parent=None, trade_id=None):
        self.orderId, self.symbol, self.role, self.action = order_id, symbol, role, action
        self.totalQuantity, self.orderRef, self.placed_at = quantity, ref, placed_at
        self.lmtPrice, self.auxPrice, self.good_till = limit, aux, good_till
        self.reason, self.ocaGroup, self.parent, self.trade_id = reason, oca, parent, trade_id
        self.filled, self.fill_value, self.commission = 0, 0.0, 0.0
        self.status, self.closed_at, self.reject_reasons = "working", None, []
        self.contract = NS(symbol=symbol, secType="STK")
        self.log = [NS(time=placed_at.astimezone(UTC))]
        self.first_quantity = quantity
        self.aux_history = [(placed_at, aux)]  # (time set, stop price): a raise is not retroactive

    def eligible(self, bar_start: dt.datetime) -> bool:
        """An order can fill only in bars that start after it was sent (no prices from
        before the order existed)."""
        return self.placed_at <= bar_start

    def set_stop(self, price: float, when: dt.datetime):
        self.auxPrice = price
        self.aux_history.append((when, price))

    def stop_at(self, bar_start: dt.datetime) -> float:
        """The stop price in force when the bar began (a raise sent during the bar
        applies from the next bar)."""
        price = self.aux_history[0][1]
        for when, value in self.aux_history:
            if when <= bar_start:
                price = value
        return price

    @property
    def order(self):
        return self

    @property
    def orderStatus(self):
        return NS(filled=self.filled, status=self.status)

    def isDone(self) -> bool:
        return self.status != "working"

    @property
    def remaining(self) -> int:
        return max(0, int(self.totalQuantity) - self.filled)

    @property
    def avg_fill(self) -> float | None:
        return self.fill_value / self.filled if self.filled else None

    @property
    def active(self) -> bool:
        """A bracket child stop works only once its parent entry has (partly) filled."""
        return self.status == "working" and (self.parent is None or self.parent.filled > 0)


class SimTicker:
    """reqMktData stand-in: bid/ask around the last completed bar's close at the
    simulated time (there is no historical quote data)."""

    def __init__(self, sim, contract):
        self.sim, self.contract, self.halted = sim, contract, 0.0

    def _price(self):
        return self.sim.last_price(self.contract.symbol)

    @property
    def bid(self):
        p = self._price()
        return p * (1 - _bps(self.sim.ex["half_spread_bps"])) if p else float("nan")

    @property
    def ask(self):
        p = self._price()
        return p * (1 + _bps(self.sim.ex["half_spread_bps"])) if p else float("nan")


class SimIB:
    """The subset of ib_async.IB that rule_strategy.decide() and core/ read."""

    def __init__(self, sim):
        self.sim = sim

    def positions(self, account=None):
        return [NS(contract=NS(symbol=s, secType="STK"), position=float(p.qty), avgCost=p.avg_cost)
                for s, p in self.sim.positions.items() if p.qty > 0]

    def openTrades(self):
        return [o for o in self.sim.orders if o.status == "working"]

    reqAllOpenOrders = openTrades

    def qualifyContracts(self, *contracts):
        for c in contracts:
            c.conId = 1 if self.sim.data.daily(c.symbol) else 0
        return list(contracts)

    def reqHistoricalData(self, contract, endDateTime, durationStr, barSizeSetting, whatToShow,
                          useRTH, formatDate=2, timeout=0, **kwargs):
        return self.sim.history(contract.symbol, durationStr, barSizeSetting, useRTH)

    def reqMktData(self, contract, *args, **kwargs):
        return SimTicker(self.sim, contract)

    def cancelMktData(self, contract):
        pass

    def sleep(self, seconds=0):
        pass

    def _refuse(self, *args, **kwargs):
        raise RuntimeError("SimIB never sends orders to a broker")

    placeOrder = cancelOrder = reqGlobalCancel = _refuse


# ---------------------------------------------------------------- ledger records

@dataclass
class SimTrade:
    trade_id: int
    symbol: str
    signal_time: dt.datetime
    initial_stop: float
    conditions: list
    gap_pct: float | None
    rvol: float | None
    entry_reason: str
    bought: int = 0
    buy_value: float = 0.0
    sold: int = 0
    sell_value: float = 0.0
    commission: float = 0.0
    slippage: float = 0.0
    first_fill: dt.datetime | None = None
    exit_time: dt.datetime | None = None
    exit_reasons: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    stop_history: list = field(default_factory=list)
    order_ids: list = field(default_factory=list)

    def warn(self, text: str):
        if text not in self.warnings:
            self.warnings.append(text)

    def reason(self, text: str):
        if text not in self.exit_reasons:
            self.exit_reasons.append(text)


@dataclass
class SimPosition:
    symbol: str
    trade_id: int
    qty: int = 0
    cost: float = 0.0  # fills + buy commissions (IBKR avgCost includes commission)
    last_close: float | None = None

    @property
    def avg_cost(self) -> float:
        return self.cost / self.qty if self.qty else 0.0


@dataclass
class SimResult:
    orders: list
    fills: list
    trades: list
    open_trades: list
    equity: list
    daily: list
    events: list
    trade_context: dict
    funnel: dict
    watchlists: dict
    days: list
    warnings: list
    strategy_log: list
    final_equity: float
    final_cash: float
    risk_state: dict


class _CaptureHandler(logging.Handler):
    """Keeps the strategy's own log lines (WARNING and above) of a simulation."""

    def __init__(self, limit: int = 500):
        super().__init__(logging.WARNING)
        self.lines, self.limit, self.dropped = [], limit, 0

    def emit(self, record):
        if len(self.lines) < self.limit:
            self.lines.append(f"{record.levelname} {record.getMessage()}")
        else:
            self.dropped += 1


# ---------------------------------------------------------------- the simulator

class Simulator:
    def __init__(self, profile, data: MarketData, cfg: dict, symbols: list[str],
                 start: dt.date, end: dt.date):
        self.profile, self.data, self.cfg, self.ex = profile, data, cfg, cfg["execution"]
        self.symbols, self.start, self.end = list(symbols), start, end
        self.tape = data.bar
        self.bar_len = dt.timedelta(minutes=BAR_MINUTES[self.tape])
        self.capital = float(cfg["starting_capital"])
        self.cash = self.capital
        self.now = dt.datetime.combine(start, dt.time(0, 0), ET)
        self.ib = SimIB(self)
        self.positions: dict[str, SimPosition] = {}
        self.orders: list[SimOrder] = []
        self.rejected: list[SimOrder] = []
        self.fills: list[dict] = []
        self.trades: dict[int, SimTrade] = {}
        self.closed: list[SimTrade] = []
        self.open_trade: dict[str, int] = {}
        self.equity_rows, self.daily_rows, self.events = [], [], []
        self.funnel: dict[str, int] = {}
        self.watchlists: dict[str, list] = {}
        self.price_near_threshold: list = []  # [day, symbol, open] (see watchlist())
        self.warnings: list[str] = []
        self.trade_context: dict = {}
        self._signals: dict[str, dict] = {}
        self._next_id = 1
        self._next_trade = 1
        self._day_start_equity = self.capital
        self._realized_today = 0.0
        self._bars: dict[tuple[str, dt.datetime], object] = {}
        self._dates: dict[str, list] = {}
        self.guard = None
        self.capture = _CaptureHandler()
        self._rejected_keys: set = set()
        self._bar_volume_used: dict = {}

    # ------------------------------------------------------------ helpers
    def _id(self) -> int:
        self._next_id += 1
        return self._next_id - 1

    def event(self, name: str, **data):
        self.events.append({"time": self.now.isoformat(timespec="seconds"), "event": name, **data})

    def warn(self, text: str):
        if text not in self.warnings:
            self.warnings.append(text)
            log.warning(text)

    def _block(self, kind: str, type_name: str):
        if kind == "stop":
            return self.profile.stop if self.profile.stop.type == type_name else None
        return next((b for b in getattr(self.profile, kind) if b.type == type_name), None)

    def _before(self, symbol: str, day: dt.date) -> int:
        dates = self._dates.setdefault(symbol, [b.date for b in self.data.daily(symbol)])
        return bisect.bisect_left(dates, day)

    def _prior_days(self, symbol: str, day: dt.date, n: int) -> list[dt.date]:
        i = self._before(symbol, day)
        return [b.date for b in self.data.daily(symbol)[max(0, i - n):i]] if n > 0 else []

    def _market_days(self) -> list[dt.date]:
        days = set()
        for s in self.symbols:
            days.update(b.date for b in self.data.daily(s) if self.start <= b.date <= self.end)
        return sorted(days)

    def _bar(self, symbol: str, start: dt.datetime):
        key = (symbol, start)
        if key not in self._bars:
            for b in self.data.day_bars(symbol, start.date()):
                self._bars[(symbol, b.date.astimezone(ET))] = b
            self._bars.setdefault(key, None)
        return self._bars[key]

    def _equity(self) -> float:
        return self.cash + sum(p.qty * (p.last_close or p.avg_cost) for p in self.positions.values())

    def working(self, role: str | None = None, symbol: str | None = None) -> list[SimOrder]:
        return [o for o in self.orders if o.status == "working" and (role is None or o.role == role)
                and (symbol is None or o.symbol == symbol)]

    def buying_power(self) -> float:
        committed = sum(o.remaining * o.lmtPrice for o in self.working("entry"))
        return self.cash * self.ex["buying_power_multiplier"] - committed

    def _account(self) -> AccountState:
        longs = {s: float(p.qty) for s, p in self.positions.items() if p.qty > 0}
        buys = {o.symbol for o in self.working("entry")}
        return AccountState(self._equity(), longs, buys, self.buying_power())

    # ------------------------------------------------------------ what decide() sees
    def history(self, symbol: str, duration: str, size: str, rth: bool) -> list:
        """reqHistoricalData at the simulated time: never a bar that had not started."""
        now, today = self.now, self.now.date()
        if size == "1 day":
            return self.data.daily(symbol)[:self._before(symbol, today)]
        wanted = {"1 min": "1m", "5 mins": "5m", "15 mins": "15m", "1 hour": "1h"}.get(size)
        if wanted != self.tape:
            self.warn(f"strategy asked for {size} bars but the simulation tape is {self.tape}; "
                      "returned none")
            return []
        match = re.fullmatch(r"(\d+) D", duration)
        n = int(match.group(1)) if match else 1
        out = []
        for d in self._prior_days(symbol, today, n - 1) + [today]:
            for b in self.data.day_bars(symbol, d):
                t = b.date.astimezone(ET)
                if t >= now:
                    break
                if rth and not (RTH_OPEN <= t.time() < RTH_CLOSE):
                    continue
                out.append(b)
        return out

    def last_price(self, symbol: str) -> float | None:
        """Close of the last bar that had completed at the simulated time."""
        now = self.now
        for b in reversed(self.data.day_bars(symbol, now.date())):
            if b.date.astimezone(ET) + self.bar_len <= now:
                return b.close
        prior = self.data.daily(symbol)[:self._before(symbol, now.date())]
        return prior[-1].close if prior else None

    # ------------------------------------------------------------ the patched environment
    @contextlib.contextmanager
    def environment(self):
        """Point the live strategy modules at this simulation, then restore them."""
        clock = self

        class SimDateTime(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return clock.now.astimezone(tz) if tz else clock.now.replace(tzinfo=None)

        shim = types.ModuleType("datetime_sim")
        shim.__dict__.update({k: getattr(dt, k) for k in dir(dt) if not k.startswith("__")})
        shim.datetime = SimDateTime
        strat_log = logging.getLogger("paperbot.backtest.strategy")
        strat_log.propagate = False
        strat_log.setLevel(logging.INFO)
        strat_log.addHandler(self.capture)
        original_evaluate = rule_strategy.evaluate

        def evaluate(ib, symbol, profile, now):
            result = original_evaluate(ib, symbol, profile, now)
            failure = next((r[5:] for r in result["reasons"] if r.startswith("FAIL ")), None)
            key = "pass" if failure is None else check_key(failure)
            self.funnel[key] = self.funnel.get(key, 0) + 1
            if result["pass"]:
                self._signals[symbol] = result
            return result

        with tempfile.TemporaryDirectory(prefix="paperbot-bt-") as name, contextlib.ExitStack() as stack:
            tmp = Path(name)
            patches = [
                mock.patch.object(rule_strategy, "dt", shim), mock.patch.object(core_orders, "dt", shim),
                mock.patch.object(core_risk, "dt", shim), mock.patch.object(md, "dt", shim),
                mock.patch.object(rule_strategy, "STATE", tmp / "strategy_state.json"),
                mock.patch.object(rule_strategy, "SAFETY_LOG", tmp / "safety_log.jsonl"),
                mock.patch.object(rule_strategy, "WATCHLIST_FILE", tmp / "watchlist.txt"),
                mock.patch.object(rule_strategy, "ACTIVE", None),
                mock.patch.object(rule_strategy, "SCAN_BUDGET_SECONDS", 1e9),
                mock.patch.object(rule_strategy, "net_liquidation_usd",
                                  lambda ib, account, fetch=True: self._equity()),
                mock.patch.object(rule_strategy, "buying_power_usd", lambda ib, account: self.buying_power()),
                mock.patch.object(rule_strategy, "evaluate", evaluate),
                mock.patch.object(runstate, "stop_requested", lambda: False),
                mock.patch.object(md, "QUOTE_TIMEOUT", 0.0),
                mock.patch.object(core_orders, "push", lambda *a, **k: None),
                mock.patch.object(core_common, "push", lambda *a, **k: None),
                *[mock.patch.object(m, "log", strat_log)
                  for m in (rule_strategy, core_orders, md, core_sizing, core_common)],
            ]
            patches += [mock.patch.object(m, "write_json_atomic", _write_json_fast)
                        for m in (rule_strategy, core_risk)]
            saved_profile = rule_strategy._profile
            for p in patches:
                stack.enter_context(p)
            md.clear_caches()
            core_orders._orphans.clear()
            rule_strategy.configure(self.profile)
            self.guard = RiskGuard(self.profile.limits, tmp / "state.json",
                                   today=lambda: self.now.date(),
                                   drawdown_path=tmp / "drawdown_state.json")
            try:
                yield
            finally:
                md.clear_caches()
                core_orders._orphans.clear()
                rule_strategy._profile = saved_profile
                strat_log.removeHandler(self.capture)

    # ------------------------------------------------------------ the run
    def plan(self, days: list[dt.date]) -> dict[str, set]:
        """Which symbol-days of intraday data the run needs."""
        rvol = self._block("entry", "min_rvol")
        lookback = (rvol.lookback_days + 6) if rvol else 0
        needed: dict[str, set] = {}
        watchlist = self.profile.universe.get("source") == "watchlist"
        for day in days:
            symbols = self.watchlist(day) if watchlist else self.symbols
            if watchlist:
                self.watchlists[day.isoformat()] = symbols
            for s in symbols:
                needed.setdefault(s, set()).add(day)
                needed[s].update(self._prior_days(s, day, lookback))
        return needed

    def watchlist(self, day: dt.date) -> list[str]:
        """What morning_prefilter.py would have written for a watchlist strategy: symbols
        whose daily OPEN gapped at least the min_gap_pct block's threshold above the prior
        close (and open >= the min_price block), largest gap first, max_watchlist of them.
        Only the open is used (known at 09:30). The live prefilter checks the price
        against its latest print when it runs (~09:30-10:05), so a symbol whose open is
        near min_price may be screened differently: those symbol-days are counted
        (price_near_threshold) and reported as a data-quality limitation."""
        gap_block, price_block = self._block("entry", "min_gap_pct"), self._block("entry", "min_price")
        min_gap = gap_block.min_pct if gap_block else 0.0
        min_price = price_block.min_usd if price_block else 0.0
        gaps = []
        for s in self.symbols:
            bars, i = self.data.daily(s), self._before(s, day)
            if i == 0 or i >= len(bars) or bars[i].date != day:
                continue
            gap = (bars[i].open / bars[i - 1].close - 1) * 100
            if gap >= min_gap and min_price > 0 and abs(bars[i].open / min_price - 1) <= PRICE_NEAR:
                self.price_near_threshold.append([day.isoformat(), s, round(bars[i].open, 4)])
            if gap >= min_gap and bars[i].open >= min_price:
                gaps.append((gap, s))
        gaps.sort(key=lambda x: (-x[0], x[1]))
        return [s for _, s in gaps[: int(self.cfg["max_watchlist"])]]

    def run(self, progress=None) -> SimResult:
        days = self._market_days()
        if not days:
            raise BacktestError("no trading days with daily data in the requested period")
        self.data.quality.trading_days = len(days)
        self.data.prefetch(self.plan(days))
        price_block = self._block("entry", "min_price")
        if self.profile.universe.get("source") == "watchlist" and price_block and price_block.min_usd > 0:
            n = len(self.price_near_threshold)
            self.data.quality.limit(
                f"Watchlist price check: the simulated prefilter applies the ${price_block.min_usd:g} minimum "
                f"price to each day's open; the live prefilter uses the latest price when it runs "
                f"(~09:30-10:05 ET). {n} gapping symbol-day(s) opened within {PRICE_NEAR:.0%} of "
                f"${price_block.min_usd:g}, where the two could disagree"
                + (": " + ", ".join(f"{s} {d}" for d, s, _ in self.price_near_threshold[:10])
                   + (" ..." if n > 10 else "") if n else "") + ".")
        with self.environment():
            for i, day in enumerate(days, 1):
                self._run_day(day)
                if progress and (i % 20 == 0 or i == len(days)):
                    progress(i, len(days))
            open_trades = [self.trades[p.trade_id] for p in self.positions.values() if p.qty > 0]
            for t in open_trades:
                t.warn("open_at_end_of_test")
                t.reason("open at end of test")
                self._context(t)
            return self._result(days, open_trades)

    def _pass_times(self, day: dt.date) -> list[dt.datetime]:
        delay = dt.timedelta(seconds=self.ex["decision_delay_seconds"])
        open_ = dt.datetime.combine(day, RTH_OPEN, ET)
        n = 390 // BAR_MINUTES[self.tape]
        times = [open_ + k * self.bar_len + delay for k in range(n)]
        fc = self.profile.time_filter.force_close_time()
        if fc is not None:
            fct = dt.datetime.combine(day, fc, ET)
            if all(abs((t - fct).total_seconds()) > self.ex["decision_delay_seconds"] for t in times):
                times.append(fct)
        return sorted(t for t in times if t.time() < RTH_CLOSE)

    def _run_day(self, day: dt.date):
        self.now = dt.datetime.combine(day, RTH_OPEN, ET)
        if self.ex["reset_drawdown_breaker_next_day"] and self.guard.breaker and self.guard.breaker.tripped:
            self.guard.breaker.reset()
            self.event("drawdown_breaker_reset", note="auto reset (backtest setting)")
        if self.profile.universe.get("source") == "watchlist":
            symbols = self.watchlists.get(day.isoformat(), [])
            rule_strategy.WATCHLIST_FILE.write_text(
                f"# Auto-generated by paperbot backtest at {day.isoformat()} 09:55 ET\n"
                + "".join(f"{s}\n" for s in symbols), encoding="utf-8")
        self._day_start_equity = self._equity()
        self._realized_today = 0.0
        start_trades, start_fills, start_orders = len(self.closed), len(self.fills), len(self.orders)
        self._bars.clear()
        passes = self._pass_times(day)
        delay = dt.timedelta(seconds=self.ex["decision_delay_seconds"])
        open_ = dt.datetime.combine(day, RTH_OPEN, ET)
        low_equity = self._day_start_equity
        for k in range(390 // BAR_MINUTES[self.tape]):
            bar_start = open_ + k * self.bar_len
            # the open, then this bar's passes (the scheduled one a few seconds in and any
            # off-grid one such as a 15:51 force close), then the rest of the bar. What a
            # pass sends fills from the first bar that starts after it (SimOrder.eligible).
            self.now = bar_start
            self._execute(bar_start, "open")
            while passes and passes[0] < bar_start + self.bar_len:
                self._pass(passes.pop(0))
            self.now = bar_start
            self._execute(bar_start, "range")
            equity = self._equity()
            low_equity = min(low_equity, equity)
            self.equity_rows.append(self._mark(bar_start + self.bar_len, equity))
        for t in passes:
            self._pass(t)
        close = dt.datetime.combine(day, RTH_CLOSE, ET)
        self.now = close
        for o in self.working():
            if o.role in ("entry", "exit"):  # DAY / GTD orders die at the close
                self._end(o, "expired" if not o.filled else "partially_filled_expired", close)
                if o.role == "entry":
                    self._entry_ended(o, close)
        if self.profile.holding == "intraday" and any(p.qty for p in self.positions.values()):
            self.warn(f"{day}: position(s) held overnight by an intraday strategy (no data to close)")
            for p in self.positions.values():
                self.trades[p.trade_id].warn("held_overnight")
        self.data.release(day - dt.timedelta(days=45))
        closed = self.closed[start_trades:]
        nets = [_net(t) for t in closed]
        end_equity = self._equity()
        self.daily_rows.append({
            "date": day.isoformat(), "start_equity": round(self._day_start_equity, 2),
            "end_equity": round(end_equity, 2), "pnl": round(end_equity - self._day_start_equity, 2),
            "realized_pnl": round(self._realized_today, 2), "trades_closed": len(closed),
            "wins": sum(1 for n in nets if n > 0.005), "losses": sum(1 for n in nets if n < -0.005),
            "commission": round(sum(f["commission"] for f in self.fills[start_fills:]), 2),
            "orders": len(self.orders) - start_orders,
            "watchlist_size": len(self.watchlists.get(day.isoformat(), [])),
            "intraday_low_equity": round(low_equity, 2), "halted": bool(self.guard.halted),
            "open_positions_at_close": sum(1 for p in self.positions.values() if p.qty)})

    def _mark(self, when: dt.datetime, equity: float) -> dict:
        value = sum(p.qty * (p.last_close or p.avg_cost) for p in self.positions.values())
        return {"time_et": when.isoformat(timespec="seconds"), "equity": round(equity, 2),
                "cash": round(self.cash, 2), "positions_value": round(value, 2),
                "open_positions": sum(1 for p in self.positions.values() if p.qty),
                "exposure_pct": round(100 * value / equity, 3) if equity > 0 else None}

    # ------------------------------------------------------------ one pass of the real strategy
    def _expire_entries(self, when: dt.datetime):
        """GTD entries die at their good-till time at IBKR, whatever the bot does."""
        for e in self.working("entry"):
            if e.good_till is not None and when >= e.good_till:
                self._end(e, "expired" if not e.filled else "partially_filled_expired", e.good_till)
                self._entry_ended(e, e.good_till)

    def _pass(self, when: dt.datetime):
        self._expire_entries(when)
        self.now = when
        daily = self._equity() - self._day_start_equity
        was_halted = self.guard.halted
        self.guard.update_daily_pnl(daily)
        if self.guard.halted and not was_halted:
            for o in self.working():
                if o.role in ("entry", "exit"):
                    self._cancel(o, f"halt: {self.guard.halt_reason}")
            self.event("halt", reason=self.guard.halt_reason)
        try:
            requests = rule_strategy.decide(self.ib, SIM_ACCOUNT)
        except Exception as e:  # noqa: BLE001 - the live runner records a failed pass and continues
            self.warn(f"{when:%Y-%m-%d %H:%M} strategy pass failed: {e!r}")
            return
        for request in requests:
            self._handle(request, daily)

    def _handle(self, request, daily: float):
        if isinstance(request, CancelRequest):
            order = next((o for o in self.orders if o.orderId == request.order_id), None)
            if order is not None and order.status == "working":
                self._cancel(order, request.reason)
            return
        refs = self.profile.refs
        if isinstance(request, StopUpdate):
            problems = self.guard.check_stop(request, self._account())
            if problems:
                self._reject(request.symbol, "SELL", "stop", request.quantity, request.stop_price,
                             request.reason, problems)
                return
            stop = next(iter(self.working("stop", request.symbol)), None)
            trade_id = self.open_trade.get(request.symbol)
            if stop is not None:
                if stop.auxPrice != request.stop_price and trade_id in self.trades:
                    self.trades[trade_id].stop_history.append(
                        {"time": self.now.isoformat(timespec="seconds"), "event": request.reason,
                         "old": stop.auxPrice, "new": request.stop_price})
                if stop.auxPrice != request.stop_price:
                    stop.set_stop(request.stop_price, self.now)
                stop.totalQuantity = stop.filled + request.quantity
            else:
                self._new(request.symbol, "stop", "SELL", request.quantity, refs[1],
                          aux=request.stop_price, reason=request.reason,
                          oca=self._oca(request.symbol), trade_id=trade_id)
            return
        intent: OrderIntent = request
        open_orders = sum(1 for o in self.working() if o.role != "stop")
        problems = self.guard.check(intent, open_orders, daily, self._account())
        if problems:
            self._reject(intent.symbol, intent.action, "entry" if intent.action == "BUY" else "exit",
                         intent.quantity, intent.limit_price, intent.reason, problems)
            return
        if intent.action == "BUY":
            signal = self._signals.get(intent.symbol, {})
            trade_id = self._next_trade
            self._next_trade += 1
            self.trades[trade_id] = SimTrade(
                trade_id, intent.symbol, self.now, intent.stop_price, list(signal.get("reasons", [])),
                signal.get("gap"), signal.get("rvol"), intent.reason)
            entry = self._new(intent.symbol, "entry", "BUY", intent.quantity, refs[0],
                              limit=intent.limit_price, good_till=intent.good_till,
                              reason=intent.reason, trade_id=trade_id)
            self._new(intent.symbol, "stop", "SELL", intent.quantity, refs[1], aux=intent.stop_price,
                      reason="bracket stop", oca=self._oca(intent.symbol), parent=entry,
                      trade_id=trade_id)
            self.guard.record_order()
            self.event("entry_sent", symbol=intent.symbol, qty=intent.quantity,
                       limit=intent.limit_price, stop=intent.stop_price)
        else:
            self._new(intent.symbol, "exit", "SELL", intent.quantity, refs[2],
                      limit=intent.limit_price, reason=intent.reason, oca=intent.oca_group or "",
                      trade_id=self.open_trade.get(intent.symbol))
        rule_strategy.on_sent(intent)

    def _oca(self, symbol: str) -> str:
        return f"sim:{symbol}:{self._next_id}"

    def _new(self, symbol, role, action, qty, ref, **kw) -> SimOrder:
        order = SimOrder(self._id(), symbol, role, action, int(qty), ref, self.now, **kw)
        self.orders.append(order)
        if order.trade_id in self.trades and order.orderId not in self.trades[order.trade_id].order_ids:
            self.trades[order.trade_id].order_ids.append(order.orderId)
        return order

    def _reject(self, symbol, action, role, qty, price, reason, problems):
        """A request the live risk guard blocked. The strategy repeats a blocked request
        every pass, so each distinct one is recorded once a day and the repeats counted."""
        key = (symbol, role, self.now.date(), tuple(problems))
        self.funnel[f"blocked: {problems[0][:60]}"] = self.funnel.get(f"blocked: {problems[0][:60]}", 0) + 1
        if key in self._rejected_keys:
            return
        self._rejected_keys.add(key)
        order = SimOrder(self._id(), symbol, role, action, int(qty or 0), "", self.now,
                         limit=price, reason=reason)
        order.status, order.closed_at, order.reject_reasons = "rejected", self.now, list(problems)
        self.rejected.append(order)
        self.event("rejected", symbol=symbol, role=role, problems=problems)

    def _end(self, order: SimOrder, status: str, when: dt.datetime):
        order.status, order.closed_at = status, when

    def _cancel(self, order: SimOrder, reason: str):
        self._end(order, "cancelled", self.now)
        order.reason = f"{order.reason} | cancelled: {reason}" if order.reason else f"cancelled: {reason}"
        if order.role == "entry":
            self._entry_ended(order, self.now)

    def _entry_ended(self, entry: SimOrder, when: dt.datetime):
        """An entry stopped working: IBKR cancels an unfilled bracket's child stop and
        sizes a partly filled one to the shares bought."""
        for child in [o for o in self.working("stop", entry.symbol) if o.parent is entry]:
            if entry.filled == 0:
                self._end(child, "cancelled", when)
            else:
                child.totalQuantity = child.filled + min(child.remaining, entry.filled)
        trade = self.trades.get(entry.trade_id)
        if entry.filled == 0 and trade is not None and not trade.bought:
            self.trades.pop(entry.trade_id)  # never filled: an order, not a trade

    # ------------------------------------------------------------ execution in one bar
    def _execute(self, bar_start: dt.datetime, phase: str):
        """phase "open": the bar's first print, before the pass at bar_start + delay;
        phase "range": the rest of the bar, after that pass."""
        symbols = {o.symbol for o in self.working()} | {s for s, p in self.positions.items() if p.qty}
        for symbol in sorted(symbols):
            bar = self._bar(symbol, bar_start)
            if bar is None:
                pos = self.positions.get(symbol)
                if phase == "range" and pos and pos.qty:
                    self.trades[pos.trade_id].warn("missing_bar")
                continue
            if phase == "open":
                self._fill_open(symbol, bar, bar_start)
                continue
            self._fill_range(symbol, bar, bar_start)
            pos = self.positions.get(symbol)
            if pos and pos.qty:
                pos.last_close = bar.close

    def _eligible(self, role: str, symbol: str, start: dt.datetime) -> list[SimOrder]:
        return [o for o in self.working(role, symbol) if o.eligible(start)]

    def _fill_open(self, symbol: str, bar, start: dt.datetime):
        """Orders working when the bar began and marketable at its open fill at the open
        (this happens before the pass that starts a few seconds into the bar)."""
        cost = _bps(self.ex["half_spread_bps"] + self.ex["slippage_bps"])
        self._expire_entries(start)
        self._bar_volume_used = {}
        for e in self._eligible("entry", symbol, start):
            if bar.open <= e.lmtPrice:
                self._buy(e, bar, min(e.lmtPrice, bar.open * (1 + cost)), bar.open, start, "at open")
        for x in self._eligible("exit", symbol, start):
            if bar.open >= x.lmtPrice:
                self._sell(x, max(x.lmtPrice, bar.open * (1 - cost)), bar.open, start, "at open")
        self._stop(symbol, bar, start, cost, at_open=True)  # a gap through the stop

    def _fill_range(self, symbol: str, bar, start: dt.datetime):
        """The bar's range, for orders that were working when it began and are still working
        after the pass (one sent by the pass waits for the next bar; a stop raised by it
        applies from the next bar)."""
        cost = _bps(self.ex["half_spread_bps"] + self.ex["slippage_bps"])
        # 1. stops (a working exit that could also have filled: stop assumed first, and
        #    that exit is then NOT filled in the same bar - the conservative outcome)
        ambiguous = self._stop(symbol, bar, start, cost)
        # 2. exit limits reached inside the bar
        for x in ([] if ambiguous else self._eligible("exit", symbol, start)):
            if x.status == "working" and bar.high > x.lmtPrice:
                self._sell(x, x.lmtPrice, x.lmtPrice, start, "limit reached intrabar")
        # 3. entry limits traded through inside the bar (its stop may then trigger)
        for e in self._eligible("entry", symbol, start):
            open_only = e.good_till is not None and start + self.bar_len > e.good_till
            if e.status == "working" and not open_only and bar.low < e.lmtPrice:
                if self._buy(e, bar, e.lmtPrice, e.lmtPrice, start, "limit traded through intrabar"):
                    self._stop(symbol, bar, start, cost, after_entry=True)

    def _stop(self, symbol, bar, start, cost, after_entry=False, at_open=False) -> bool:
        """Fill the active stop if the bar reached it (at_open: only a gap through it at the
        open). True when the bar was ambiguous (a working exit's limit was also reached
        and the order of events is unknown)."""
        pos = self.positions.get(symbol)
        if not pos or pos.qty <= 0:
            return False
        stop = next((o for o in self.working("stop", symbol) if o.active and o.eligible(start)), None)
        if stop is None:
            return False
        level = stop.stop_at(start)
        if bar.low > level or (at_open and bar.open > level):
            return False
        trade = self.trades[pos.trade_id]
        ambiguous = not after_entry and not at_open and any(bar.high > x.lmtPrice for x in self.working("exit", symbol)
                                            if x.eligible(start))
        if ambiguous:
            trade.warn("ambiguous_bar")
            self.event("ambiguous_bar", symbol=symbol, policy=self.ex["ambiguous_bar_policy"])
        if bar.open <= level:
            ref, note = bar.open, "gap through stop: filled at the open"
            trade.warn("entry_filled_below_stop" if after_entry else "gap_through_stop")
        else:
            ref, note = level, "stop triggered"
        trade.reason(self._stop_label(symbol))
        self._sell(stop, ref * (1 - cost), ref, start, note, quantity=min(stop.remaining, pos.qty))
        return ambiguous

    def _stop_label(self, symbol: str) -> str:
        try:
            st = rule_strategy.load_state()["positions"].get(symbol) or {}
        except Exception:  # noqa: BLE001
            st = {}
        if not st or st.get("stop", 0) <= st.get("initial_stop", 0) + 1e-9:
            return "initial stop"
        pos = self.positions.get(symbol)
        if st.get("breakeven") and pos and abs(st["stop"] - pos.avg_cost) < 0.02:
            return "breakeven stop"
        return "trailing stop" if st.get("breakeven") else "raised stop"

    def _buy(self, order: SimOrder, bar, price, ref, when, note) -> bool:
        """Fill an entry up to participation_rate x the bar's volume (per order, per bar)."""
        cap = int(bar.volume * self.ex["participation_rate"]) - self._bar_volume_used.get(order.orderId, 0)
        qty = min(order.remaining, cap)
        trade = self.trades.get(order.trade_id)
        if qty < 1:
            if trade:
                trade.warn("volume_capped_no_fill")
            return False
        if qty < order.remaining and trade:
            trade.warn("partial_entry_fill")
        self._bar_volume_used[order.orderId] = self._bar_volume_used.get(order.orderId, 0) + qty
        self._fill(order, qty, price, ref, when, note)
        return True

    def _sell(self, order: SimOrder, price, ref, when, note, quantity=None):
        pos = self.positions.get(order.symbol)
        qty = min(quantity if quantity is not None else order.remaining, pos.qty if pos else 0)
        if qty < 1:
            return
        if order.role == "exit":
            self.trades[pos.trade_id].reason(exit_label(order.reason))
        self._fill(order, qty, price, ref, when, note)

    def _fill(self, order: SimOrder, qty: int, price: float, ref: float, when: dt.datetime, note: str):
        price = round(price, 4)
        when = max(when, order.placed_at)  # a fill is never earlier than its order
        before = order.commission
        order.filled += qty
        order.fill_value += qty * price
        total = commission_per_side(order.filled, order.fill_value / order.filled)
        fee = round(max(0.0, total - before), 4)
        order.commission += fee
        slip = round(abs(price - ref) * qty, 4)
        realized = 0.0
        if order.action == "BUY":
            pos = self.positions.get(order.symbol)
            if pos is None or pos.qty == 0:
                pos = self.positions[order.symbol] = SimPosition(order.symbol, order.trade_id)
                self.open_trade[order.symbol] = order.trade_id
            trade = self.trades[pos.trade_id]
            pos.qty += qty
            pos.cost += qty * price + fee
            pos.last_close = pos.last_close or price
            self.cash -= qty * price + fee
            trade.bought += qty
            trade.buy_value += qty * price
            trade.first_fill = trade.first_fill or when
        else:
            pos = self.positions[order.symbol]
            trade = self.trades[pos.trade_id]
            realized = qty * (price - pos.avg_cost) - fee
            pos.cost -= pos.avg_cost * qty
            pos.qty -= qty
            self.cash += qty * price - fee
            trade.sold += qty
            trade.sell_value += qty * price
            self._realized_today += realized
            self._oca_reduce(order, qty)
        trade.commission += fee
        trade.slippage += slip
        if order.orderId not in trade.order_ids:
            trade.order_ids.append(order.orderId)
        self.fills.append({"fill_id": len(self.fills) + 1, "order_id": order.orderId,
                           "trade_id": trade.trade_id, "time_et": when.isoformat(timespec="seconds"),
                           "symbol": order.symbol, "side": order.action, "kind": order.role,
                           "quantity": qty, "price": price, "reference_price": round(ref, 4),
                           "commission": fee, "slippage_cost": slip,
                           "realized_pnl": round(realized, 4), "note": note,
                           "order_reason": order.reason})
        if order.remaining == 0:
            self._end(order, "filled", when)
            if order.role == "entry":
                self._entry_ended(order, when)
        if order.action == "SELL" and pos.qty == 0:
            self._close_trade(order.symbol, when)

    def _oca_reduce(self, filled: SimOrder, qty: int):
        """ocaType 2 (reduce with block): a fill shrinks the other orders of its OCA group."""
        if not filled.ocaGroup:
            return
        for other in self.working(symbol=filled.symbol):
            if other is filled or other.ocaGroup != filled.ocaGroup:
                continue
            other.totalQuantity = max(other.filled, int(other.totalQuantity) - qty)
            if other.remaining == 0:
                self._end(other, "cancelled", self.now)
                other.reason += " | reduced to 0 by OCA"

    def _close_trade(self, symbol: str, when: dt.datetime):
        pos = self.positions.pop(symbol)
        self.open_trade.pop(symbol, None)
        trade = self.trades[pos.trade_id]
        trade.exit_time = when
        self.closed.append(trade)
        self._context(trade)
        self.event("trade_closed", symbol=symbol, trade_id=trade.trade_id)

    def _context(self, trade: SimTrade):
        """Market data shown with the trade: the intraday tape from the entry day to the exit
        day (intraday strategies) or daily bars around the holding period (multi-day)."""
        start = (trade.first_fill or trade.signal_time).date()
        end = (trade.exit_time or self.now).date()
        if self.profile.holding == "multi_day":
            daily = self.data.daily(trade.symbol)
            i, j = self._before(trade.symbol, start), self._before(trade.symbol, end)
            rows = daily[max(0, i - 40): j + 1]
            self.trade_context[str(trade.trade_id)] = [
                [int(dt.datetime.combine(b.date, dt.time(9, 30), ET).timestamp() * 1000),
                 b.open, b.high, b.low, b.close, b.volume] for b in rows]
            return
        bars = []
        for d in sorted({start, end}):
            bars += self.data.day_bars(trade.symbol, d)
        self.trade_context[str(trade.trade_id)] = [
            [int(b.date.timestamp() * 1000), b.open, b.high, b.low, b.close, b.volume] for b in bars]

    def _result(self, days: list[dt.date], open_trades: list[SimTrade]) -> SimResult:
        breaker = self.guard.breaker
        risk_state = {"orders_today": self.guard.state["orders_today"],
                      "halted_last_day": self.guard.state["halted"],
                      "drawdown_breaker_tripped": bool(breaker and breaker.tripped),
                      "drawdown_breaker_reason": breaker.reason if breaker else ""}
        orders = sorted(self.orders + self.rejected, key=lambda o: o.orderId)
        strategy_log = list(self.capture.lines)
        if self.capture.dropped:
            strategy_log.append(f"... {self.capture.dropped} more lines not kept")
        return SimResult(
            orders=[_order_row(o) for o in orders], fills=self.fills,
            trades=[_trade_row(t, None, self.profile.key) for t in self.closed],
            open_trades=[_trade_row(t, self.positions[t.symbol], self.profile.key) for t in open_trades],
            equity=self.equity_rows, daily=self.daily_rows, events=self.events,
            trade_context=self.trade_context, funnel=self.funnel, watchlists=self.watchlists,
            days=[d.isoformat() for d in days], warnings=self.warnings, strategy_log=strategy_log,
            final_equity=round(self._equity(), 2), final_cash=round(self.cash, 2),
            risk_state=risk_state)


def _write_json_fast(path: Path, data: dict):
    """runstate.write_json_atomic without fsync: the simulation's state files live in a
    temporary folder that is thrown away, so durability does not matter, speed does."""
    path.write_text(json.dumps(data), encoding="utf-8")


# ---------------------------------------------------------------- labels and rows

LABEL_RE = re.compile(r"^[A-Z]{1,3}\d{1,2}$")


def check_key(failure: str) -> str:
    """Short name of the check that failed: the block label (D3, I1, T1 ...) or the
    first words of the reason before any number."""
    words = failure.split()
    if words and LABEL_RE.match(words[0]):
        return words[0]
    out = []
    for w in words[:4]:
        if any(c.isdigit() for c in w) or w in ("<", ">", "not"):
            break
        out.append(w.strip(":"))
    return " ".join(out) or "other"


def exit_label(reason: str) -> str:
    r = (reason or "").split(" | ")[0]
    if r.startswith("force close"):
        return "force close"
    if r.startswith("partial profit"):
        return "partial profit"
    if r.startswith("exit"):
        signal = r.split(": ", 1)[1] if ": " in r else ""
        text = re.sub(r"\s+", " ", re.sub(r"[0-9][0-9.]*", "", signal)).replace("( ", "(").replace(" )", ")")
        return f"exit signal: {text.strip()}" if signal else "exit signal"
    return r or "exit"


def _net(t: SimTrade) -> float:
    return t.sell_value - (t.buy_value / t.bought) * t.sold - t.commission if t.bought else 0.0


def _order_row(o: SimOrder) -> dict:
    return {"order_id": o.orderId, "trade_id": o.trade_id or "", "symbol": o.symbol,
            "side": o.action, "kind": o.role, "quantity": int(o.first_quantity),
            "final_quantity": int(o.totalQuantity),
            "limit_price": o.lmtPrice if o.lmtPrice is not None else "",
            "stop_price": o.auxPrice if o.auxPrice is not None else "",
            "placed_at_et": o.placed_at.isoformat(timespec="seconds"),
            "good_till_et": o.good_till.isoformat(timespec="seconds") if o.good_till else "",
            "status": o.status, "filled": o.filled,
            "avg_fill_price": round(o.avg_fill, 4) if o.avg_fill else "",
            "commission": round(o.commission, 4),
            "closed_at_et": o.closed_at.isoformat(timespec="seconds") if o.closed_at else "",
            "oca_group": o.ocaGroup, "parent_id": o.parent.orderId if o.parent else "",
            "reason": o.reason, "reject_reasons": "; ".join(o.reject_reasons)}


def _trade_row(t: SimTrade, pos: SimPosition | None, strategy: str) -> dict:
    entry_price = t.buy_value / t.bought if t.bought else None
    exit_price = t.sell_value / t.sold if t.sold else None
    gross_realized = (t.sell_value - entry_price * t.sold) if entry_price is not None else 0.0
    unrealized = None
    if pos is not None and pos.qty:
        unrealized = round(pos.qty * ((pos.last_close or entry_price) - entry_price), 2)
    net = gross_realized - t.commission + (unrealized or 0.0)
    risk_per_share = (entry_price - t.initial_stop) if entry_price and t.initial_stop else None
    r_multiple = round(net / (risk_per_share * t.bought), 3) \
        if risk_per_share and risk_per_share > 0 and t.bought else None
    hold = ((t.exit_time - t.first_fill).total_seconds() / 60) if t.exit_time and t.first_fill else None
    return {
        "trade_id": t.trade_id, "symbol": t.symbol, "strategy": strategy, "direction": "long",
        "status": "open" if pos is not None else "closed",
        "signal_time_et": t.signal_time.isoformat(timespec="seconds"),
        "entry_time_et": t.first_fill.isoformat(timespec="seconds") if t.first_fill else "",
        "exit_time_et": t.exit_time.isoformat(timespec="seconds") if t.exit_time else "",
        "entry_price": round(entry_price, 4) if entry_price else "",
        "exit_price": round(exit_price, 4) if exit_price else "",
        "quantity": t.bought, "initial_stop": t.initial_stop,
        "exit_reason": " + ".join(t.exit_reasons),
        "gross_pnl": round(gross_realized + (unrealized or 0.0), 2),
        "commission": round(t.commission, 2), "slippage_cost": round(t.slippage, 2),
        "net_pnl": round(net, 2), "unrealized_pnl": unrealized if unrealized is not None else "",
        "return_on_position_pct": round(100 * net / t.buy_value, 3) if t.buy_value else "",
        "r_multiple": r_multiple if r_multiple is not None else "",
        "holding_minutes": round(hold, 1) if hold is not None else "",
        "gap_pct": round(t.gap_pct, 3) if t.gap_pct is not None else "",
        "rvol": round(t.rvol, 3) if t.rvol is not None else "",
        "entry_conditions": " | ".join(t.conditions), "entry_reason": t.entry_reason,
        "warnings": ";".join(t.warnings), "order_ids": ";".join(map(str, t.order_ids)),
        "stop_history": json.dumps(t.stop_history),
    }
