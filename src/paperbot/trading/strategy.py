"""Trend Join Long: a rules-driven, long-only strategy on 5-minute bars.

Every parameter comes from rules.json, read on the first decide() call
(restart the bot to pick up edits). decide() is called every loop and returns
requests (see risk.py): OrderIntent, StopUpdate and CancelRequest. The bot
runs each one through the risk guard before anything reaches IBKR.

Each loop, in priority order:
  1. Reconcile: every position this strategy owns must have a protective
     SELL STP at IBKR whose size equals the shares not already covered by a
     working exit. A missing stop is re-created (ALERT); a wrong size or a
     price below the strategy's stop is corrected. A paperbot stop whose
     position is gone is cancelled (it could otherwise open a short). Runs
     at any hour.
  2. Regular hours, before force_close_et: raise stops (breakeven at +1R,
     then 5m swing-low trail) and take the partial profit at +0.75R (the
     stop is shrunk first, so stop + exit never exceed the position). A
     partial order that expires unfilled resets, so the target is
     recalculated and retried (at most PARTIAL_MAX_ATTEMPTS sends).
  3. From force_close_et: cancel stale exits, then send a fresh marketable
     limit SELL for the actual remaining shares, re-priced every loop until
     flat. The SELL joins the protective stop's OCA group, so the stop keeps
     working until IBKR fills the SELL (no unprotected gap); a stop without
     an OCA group (placed by an older version) is cancelled first, as before.
     ALERT if anything is still open at 15:58 or after close.
  4. Between earliest_entry_et and latest_entry_et (inclusive, to the
     minute): scan the universe for entries, within a time budget per loop
     (resuming where it stopped) and checking the STOP file between symbols,
     so exits are never starved. Entries are GTD and expire at IBKR one
     minute after latest_entry_et; any entry still working after that is
     cancelled by the sweep.

Prices: filters and R triggers use completed 5m bars (the strategy's rules);
limit prices use the live top of book from a streaming reqMktData
subscription (BUY from the ask, SELL from the bid). No valid quote: an entry
or partial is skipped, a force close falls back to the last 5m close (ALERT).
Entries also respect buying power (BuyingPower in USD) and are skipped when
the estimated round-trip commission exceeds max_commission_pct_of_risk of R.

evaluate() runs the filters for one symbol and returns pass/reasons/price;
every result is appended to logs/safety_log.jsonl. check_symbol() adds the
position dedupe and entry-window gate in front of it for the single-symbol
tool (bot.py --symbol X --check-only), which never places orders.

Push notifications (notify.py): BE when a stop moves to breakeven, TRAIL when
it is raised to a swing low, and "EOD Force Close" on the first force-close
attempt. Fills (BUY/STOP/PARTIAL/EXIT) are pushed by bot.notify_fills.

Stops are real orders at IBKR (sent with each entry as a bracket), so a
position stays protected if the bot, connection or computer goes down.
Positions this strategy did not open are never touched.

Universe (one IBKR ticker per line, e.g. "BRK B", # for comments):
  * watchlist.txt, written each morning by morning_prefilter.py (today's top
    gappers of rules.json universe_filters.index), when its header shows it
    was generated today (ET) for that same index
  * otherwise the fallback file (universe_filters.fallback_file, default
    universe.txt), with a warning once a day
"""
import datetime as dt
import json
import logging
import math
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from ib_async import IB, Stock, Trade

from paperbot import paths
from paperbot.market import universe as universe_settings
from paperbot.reporting.notify import notify
from paperbot.runtime import runstate
from paperbot.runtime.runstate import StateCorrupt, read_json_state, write_json_atomic
from paperbot.trading.account import buying_power_usd, net_liquidation_usd
from paperbot.trading.risk import (REF_ENTRY, REF_EXIT, REF_STOP, CancelRequest, OrderIntent,
                                   StopUpdate, commission_drag, require_limits)

RULES_FILE = paths.RULES_FILE
CONFIG_FILE = paths.CONFIG_FILE
UNIVERSE_FILE = paths.UNIVERSE_FILE  # static fallback list
WATCHLIST_FILE = paths.WATCHLIST_FILE  # today's gappers from morning_prefilter.py
STATE = paths.LOGS_DIR / "strategy_state.json"
SAFETY_LOG = paths.LOGS_DIR / "safety_log.jsonl"  # one line per symbol evaluation

ET = ZoneInfo("America/New_York")
RTH_OPEN, RTH_CLOSE = dt.time(9, 30), dt.time(16, 0)
ALERT_AT = dt.time(15, 58)  # still holding after this = force close is failing
BAR = dt.timedelta(minutes=5)
LIMIT_SLIPPAGE = 0.002  # marketable limits: 0.2% through the quote (x attempt on force close)
HIST_TIMEOUT = 10  # seconds per historical data request
QUOTE_TIMEOUT = 3.0  # seconds to wait for the first live bid/ask of a new subscription
SCAN_BUDGET_SECONDS = 10  # max entry-scan time per loop; the scan resumes next loop
EXIT_STALE_SECONDS = 60  # an exit order still working after this is cancelled/replaced
PARTIAL_MAX_ATTEMPTS = 3  # partial-profit sends per position before giving up
ENTRY_GRACE = dt.timedelta(minutes=1)  # entries expire this long after latest_entry_et

log = logging.getLogger("paperbot")

_rules: dict | None = None
_caps: dict = {}  # the risk guard's Limits (risk.load_limits), so sizing never exceeds them
_contracts: dict[str, Stock] = {}
_daily_cache: dict[str, tuple[dt.date, dict | None]] = {}
_rvol_cache: dict[str, tuple[dt.date, list]] = {}
_scan_bar: dt.datetime | None = None
_scan_pos = 0
_scan_list: list[str] = []
_warned: set[str] = set()  # one-time warnings, keyed by message
_orphans: dict[str, int] = {}  # SYMBOL -> loops in a row with a stop but no position
_tickers: dict = {}  # SYMBOL -> streaming ib_async Ticker (reqMktData)


@dataclass
class Working:
    """The bot's own open orders for one symbol, by role (orderRef)."""
    stop: Trade | None = None
    exits: list[Trade] = field(default_factory=list)
    entries: list[Trade] = field(default_factory=list)


# ---------------------------------------------------------------- rules / state

def load_rules() -> dict:
    """Parse rules.json once, on the first loop."""
    global _rules, _caps
    if _rules is None:
        rules = json.loads(RULES_FILE.read_text())
        if rules["direction"] != "long_only" or rules["trade_timeframe"] != "5m":
            raise SystemExit("strategy.py only supports direction long_only on 5m bars")
        ex = rules["exit"]
        stop = re.fullmatch(r"lod_minus_([\d.]+)pct", ex["initial_stop_rule"])
        trail = re.fullmatch(r"swing_low_5m_(\d+)_(\d+)", ex["post_breakeven_trail"])
        if not stop or not trail:
            raise SystemExit(f"unsupported exit rules in {RULES_FILE.name}: {ex}")
        rules["_stop_pct"] = float(stop.group(1))
        rules["_swing"] = (int(trail.group(1)), int(trail.group(2)))
        universe_settings.require(rules)  # index, top_n, fallback file: fail closed
        _caps = asdict(require_limits(json.loads(CONFIG_FILE.read_text()), RULES_FILE))
        _rules = rules
        log.info("loaded %s from %s", rules["strategy_name"], RULES_FILE.name)
    return _rules


def _hhmm(text: str) -> dt.time:
    hour, minute = map(int, text.split(":"))
    return dt.time(hour, minute)


def _in_entry_window(now: dt.datetime, tf: dict) -> bool:
    """earliest_entry_et <= now <= latest_entry_et, to the minute: a run that starts
    at 15:30:04 still scans (a process takes seconds to start)."""
    minute = now.time().replace(second=0, microsecond=0)
    return _hhmm(tf["earliest_entry_et"]) <= minute <= _hhmm(tf["latest_entry_et"])


def _entry_cutoff(now: dt.datetime, tf: dict) -> dt.datetime:
    """When unfilled entries die: the end of the latest_entry_et minute (15:31:00 ET)."""
    latest = _hhmm(tf["latest_entry_et"])
    return now.replace(hour=latest.hour, minute=latest.minute, second=0,
                       microsecond=0) + ENTRY_GRACE


def load_state() -> dict:
    """Per-position exit state. Fails closed (StateCorrupt) if the file is corrupt."""
    state = read_json_state(STATE) or {}
    state.setdefault("positions", {})  # SYMBOL -> {initial_stop, stop, breakeven, partial_done}
    state.setdefault("entered", {})  # SYMBOL -> date of last entry (one entry per day)
    if not isinstance(state["positions"], dict) or not isinstance(state["entered"], dict):
        raise StateCorrupt(STATE, "positions/entered must be objects")
    return state


def _save_state(state: dict):
    write_json_atomic(STATE, state)


def _warn_once(key: str, level: int, msg: str, *args):
    if key not in _warned:
        _warned.add(key)
        log.log(level, msg, *args)


def _push(title: str, body, priority: str = "default"):
    """Push notification that can never interfere with trading. `body` is a
    callable so even building the text cannot raise into the strategy."""
    try:
        notify(title, body(), priority)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------- market data

def _contract(ib: IB, symbol: str) -> Stock | None:
    if symbol not in _contracts:
        contract = Stock(symbol, "SMART", "USD")
        ib.qualifyContracts(contract)
        if not contract.conId:
            _warn_once(f"qualify:{symbol}", logging.WARNING,
                       "could not qualify %s, skipping", symbol)
            return None
        _contracts[symbol] = contract
    return _contracts[symbol]


def _bars(ib: IB, contract: Stock, duration: str, size: str, rth: bool) -> list:
    return ib.reqHistoricalData(contract, "", duration, size, "TRADES", useRTH=rth,
                                formatDate=2, timeout=HIST_TIMEOUT) or []


def _et(bar) -> dt.datetime:
    return bar.date.astimezone(ET)


def _daily(ib: IB, contract: Stock, today: dt.date) -> dict | None:
    """Prior-day high/close and the 200-day SMA of closes, cached per day."""
    cached = _daily_cache.get(contract.symbol)
    if cached and cached[0] == today:
        return cached[1]
    bars = [b for b in _bars(ib, contract, "1 Y", "1 day", True) if b.date < today]
    info = None
    if bars:
        closes = [b.close for b in bars]
        info = {"prior_high": bars[-1].high, "prior_close": bars[-1].close,
                "sma200": sum(closes[-200:]) / 200 if len(closes) >= 200 else None}
    _daily_cache[contract.symbol] = (today, info)
    return info


def _today_bars(ib: IB, contract: Stock, now: dt.datetime) -> tuple[list, list]:
    """Today's completed 5m bars, split into (premarket, regular hours)."""
    done = [b for b in _bars(ib, contract, "1 D", "5 mins", False)
            if _et(b).date() == now.date() and _et(b) + BAR <= now]
    pre = [b for b in done if _et(b).time() < RTH_OPEN]
    rth = [b for b in done if RTH_OPEN <= _et(b).time() < RTH_CLOSE]
    return pre, rth


def _rth_bars(ib: IB, symbol: str, now: dt.datetime) -> list:
    contract = _contract(ib, symbol)
    return _today_bars(ib, contract, now)[1] if contract else []


def _rvol(ib: IB, contract: Stock, rth: list, lookback: int, today: dt.date) -> float | None:
    """Today's cumulative volume vs. the average over the last `lookback` days
    at the same time of day."""
    cached = _rvol_cache.get(contract.symbol)
    if not cached or cached[0] != today:
        by_day: dict[dt.date, list] = {}
        for b in _bars(ib, contract, f"{lookback + 6} D", "5 mins", True):
            if _et(b).date() < today:
                by_day.setdefault(_et(b).date(), []).append((_et(b).time(), b.volume))
        cached = (today, [by_day[d] for d in sorted(by_day)[-lookback:]])
        _rvol_cache[contract.symbol] = cached
    history = cached[1]
    if len(history) < lookback or not rth:
        return None
    upto = _et(rth[-1]).time()
    avg = sum(sum(v for t, v in day if t <= upto) for day in history) / len(history)
    today_vol = sum(b.volume for b in rth)
    return today_vol / avg if avg > 0 else None


def _swing_low(bars: list, left: int, right: int) -> float | None:
    """Most recent pivot low: lower than `left` bars before it and no higher
    than `right` bars after it."""
    for i in range(len(bars) - right - 1, left - 1, -1):
        low = bars[i].low
        if (all(low < b.low for b in bars[i - left:i])
                and all(low <= b.low for b in bars[i + 1:i + right + 1])):
            return low
    return None


def _finite(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _quote(ib: IB, symbol: str) -> tuple[float, float] | None:
    """Live (bid, ask) from a streaming reqMktData subscription, kept open between
    loops. None if IBKR sends no valid two-sided quote within QUOTE_TIMEOUT
    (no market-data permission, halted, crossed or one-sided book)."""
    contract = _contract(ib, symbol)
    if contract is None:
        return None
    ticker = _tickers.get(symbol)
    if ticker is None:
        ticker = _tickers[symbol] = ib.reqMktData(contract, "", False, False)
    deadline = time.monotonic() + QUOTE_TIMEOUT
    while True:
        bid, ask = _finite(ticker.bid), _finite(ticker.ask)
        halted = _finite(getattr(ticker, "halted", None))  # NaN unknown, 0 trading, >0 halted
        if bid and ask and 0 < bid <= ask and not halted:
            return bid, ask
        if time.monotonic() >= deadline:
            _warn_once(f"quote:{symbol}:{_now().date()}", logging.WARNING,
                       "%s: no valid live quote from IBKR (bid %s, ask %s, halted %s)",
                       symbol, ticker.bid, ticker.ask, halted)
            return None
        ib.sleep(0.1)


def _release_quotes(ib: IB, keep: set[str]):
    """Cancel streaming quotes no longer needed (IBKR limits market-data lines)."""
    for symbol in [s for s in _tickers if s not in keep]:
        ticker = _tickers.pop(symbol)
        try:
            ib.cancelMktData(ticker.contract)
        except Exception as e:  # noqa: BLE001 - a dropped connection already ended it
            log.debug("cancelMktData %s: %r", symbol, e)


def _net_liquidation(ib: IB, account: str) -> float | None:
    """Net liquidation in USD, converted from the base currency if needed (account.py)."""
    return net_liquidation_usd(ib, account)


def read_symbols(path: Path) -> list[str]:
    """Tickers from a universe/watchlist file: one per line, # starts a comment."""
    lines = (line.split("#")[0].strip().upper() for line in path.read_text().splitlines())
    return [s for s in lines if s]


def watchlist_date(path: Path | None = None) -> dt.date | None:
    """Date in the watchlist header ("# Auto-generated ... at YYYY-MM-DD HH:MM ET")."""
    path = path or WATCHLIST_FILE
    try:
        first = path.read_text().splitlines()[0]
    except (FileNotFoundError, IndexError):
        return None
    match = re.search(r" at (\d{4}-\d{2}-\d{2}) ", first)
    return dt.date.fromisoformat(match.group(1)) if match else None


def watchlist_index(path: Path | None = None) -> str:
    """Index in the watchlist header ("# Index: Nasdaq-100"); lists written before
    the index was recorded were always S&P 500."""
    path = path or WATCHLIST_FILE
    try:
        header = path.read_text().splitlines()[:6]
    except FileNotFoundError:
        return universe_settings.DEFAULT_INDEX
    for line in header:
        match = re.match(r"# Index: (.+?)\s*$", line)
        if match:
            return match.group(1)
    return universe_settings.DEFAULT_INDEX


def _fallback_file(setup) -> Path:
    """The configured fallback list (next to UNIVERSE_FILE, i.e. config/)."""
    return UNIVERSE_FILE.with_name(setup.fallback_file.name)


def _universe(rules: dict, today: dt.date) -> list[str]:
    """Today's watchlist if it was generated today for the configured index, else
    the fallback file (universe_filters.fallback_file, default universe.txt)."""
    setup = universe_settings.settings(rules)
    fallback = _fallback_file(setup)
    if watchlist_date() == today:
        built_for = watchlist_index()
        if built_for == setup.index:
            _warn_once(f"watchlist:{today}", logging.INFO,
                       "scanning %s (generated today, %s)", WATCHLIST_FILE.name, setup.index)
            return read_symbols(WATCHLIST_FILE)
        _warn_once(f"watchlist-index:{today}:{built_for}", logging.WARNING,
                   "%s was generated for %s but rules.json screens %s; scanning %s until "
                   "the next prefilter run", WATCHLIST_FILE.name, built_for, setup.index,
                   fallback.name)
    else:
        _warn_once(f"no-watchlist:{today}", logging.WARNING,
                   "%s not generated today (run morning_prefilter.py after 9:30 ET); "
                   "scanning %s instead", WATCHLIST_FILE.name, fallback.name)
    if not fallback.exists():
        _warn_once(f"universe:{fallback.name}", logging.WARNING,
                   "%s missing: add the %s symbols (one per line) to scan for entries",
                   fallback.name, setup.index)
        return []
    return read_symbols(fallback)


# ---------------------------------------------------------------- orders view

def _working(ib: IB) -> dict[str, Working]:
    out: dict[str, Working] = {}
    for t in ib.openTrades():
        ref = t.order.orderRef or ""
        if t.isDone() or ref not in (REF_ENTRY, REF_STOP, REF_EXIT):
            continue
        w = out.setdefault(t.contract.symbol.upper(), Working())
        if ref == REF_STOP:
            w.stop = t
        elif ref == REF_EXIT:
            w.exits.append(t)
        else:
            w.entries.append(t)
    return out


def _remaining(trade: Trade) -> int:
    return int(trade.order.totalQuantity - trade.orderStatus.filled)


def _age_seconds(trade: Trade) -> float:
    if not trade.log:
        return 0.0
    return (dt.datetime.now(dt.timezone.utc) - trade.log[0].time).total_seconds()


def _closing_sell(symbol: str, quantity: int, price: float, reason: str,
                  slippage: float = LIMIT_SLIPPAGE, oca_group: str | None = None) -> OrderIntent:
    limit = round(price * (1 - slippage), 2)
    return OrderIntent(symbol, "SELL", quantity, limit, reason=reason, closing=True,
                       oca_group=oca_group)


# ---------------------------------------------------------------- decide

def _now() -> dt.datetime:
    return dt.datetime.now(ET)


def decide(ib: IB, account: str) -> list:
    rules = load_rules()
    now = _now()
    held = {p.contract.symbol.upper(): p for p in ib.positions(account)
            if p.contract.secType == "STK" and p.position > 0}
    working = _working(ib)
    state = load_state()
    _adopt_and_forget(state, held, working)

    session = now.weekday() < 5 and RTH_OPEN <= now.time() < RTH_CLOSE
    force_close = session and now.time() >= _hhmm(rules["time_filter"]["force_close_et"])
    bars: dict[str, list] = {}
    if session and not force_close:
        bars = {s: _rth_bars(ib, s, now) for s in held if s in state["positions"]}
        _raise_stops(rules, state, held, working, bars)

    requests = _cancel_orphan_stops(held, working)
    tf = rules["time_filter"]
    if session and now >= _entry_cutoff(now, tf):
        requests += _sweep_entries(working)
    if force_close:
        closing = _force_close(ib, state, held, working, now)
        # symbols being force-closed get no new stop next to their closing SELL
        closed = {r.symbol for r in closing if isinstance(r, OrderIntent)}
        requests += _reconcile_stops(state, {s: p for s, p in held.items()
                                             if s not in closed}, working)
        requests += closing
    else:
        requests += _reconcile_stops(state, held, working)
    if session and not force_close:
        requests += _partial_profits(ib, rules, state, held, working, bars)
        if _in_entry_window(now, tf):
            requests += _find_entries(ib, account, rules, state, held, working, now)
    elif not session:
        for symbol in held:
            if symbol in state["positions"] and now.time() >= RTH_CLOSE:
                _warn_once(f"overnight:{symbol}:{now.date()}", logging.ERROR,
                           "ALERT %s still open after the close; protective stop kept at IBKR",
                           symbol)
                w = working.get(symbol, Working())
                if w.exits or w.entries:
                    _warn_once(f"live-after-close:{symbol}:{now.date()}", logging.ERROR,
                               "ALERT %s: a DAY entry/exit is still live after the close; the "
                               "stop is restored only once IBKR confirms it dead", symbol)
    _save_state(state)
    _release_quotes(ib, set(held) | set(working))
    return _dedupe_cancels(requests)


def _sweep_entries(working: dict[str, Working]) -> list:
    """After the entry window: cancel every unfilled entry (GTD should already have
    expired it at IBKR; this covers an order IBKR has not expired yet)."""
    return [CancelRequest(t.order.orderId, symbol, "entry window closed")
            for symbol, w in working.items() for t in w.entries]


def _dedupe_cancels(requests: list) -> list:
    seen, out = set(), []
    for r in requests:
        if isinstance(r, CancelRequest):
            if r.order_id in seen:
                continue
            seen.add(r.order_id)
        out.append(r)
    return out


def _adopt_and_forget(state: dict, held: dict, working: dict[str, Working]):
    """Drop state for closed positions; re-adopt our positions after a state reset."""
    for symbol in [s for s in state["positions"] if s not in held and s not in working]:
        del state["positions"][symbol]
    for symbol, pos in held.items():
        if symbol in state["positions"]:
            continue
        stop = working.get(symbol, Working()).stop
        if stop is not None:
            aux = stop.order.auxPrice
            state["positions"][symbol] = {"initial_stop": aux, "stop": aux,
                                          "breakeven": aux >= pos.avgCost,
                                          "partial_done": True}
            log.warning("ALERT re-adopted %s from its working stop %.2f (strategy state "
                        "was missing; no further partial profit)", symbol, aux)
        else:
            _warn_once(f"unmanaged:{symbol}", logging.WARNING,
                       "%s position is not managed by paperbot (no state, no paperbot stop); "
                       "leaving it alone", symbol)


def _cancel_orphan_stops(held: dict, working: dict[str, Working]) -> list:
    """Cancel a paperbot stop whose position is gone (it would open a short if it
    triggered). Requires two loops in a row, so a stop whose entry has just filled
    is not cancelled before IBKR's position update arrives."""
    requests = []
    for symbol, w in working.items():
        if w.stop is not None and symbol not in held and not w.entries:
            _orphans[symbol] = _orphans.get(symbol, 0) + 1
            if _orphans[symbol] >= 2:
                log.warning("ALERT cancelling paperbot stop for %s: no position held", symbol)
                requests.append(CancelRequest(w.stop.order.orderId, symbol,
                                              "orphan stop, no position"))
        else:
            _orphans.pop(symbol, None)
    return requests


def _reconcile_stops(state: dict, held: dict, working: dict[str, Working]) -> list:
    """Make sure every position we own has the right protective stop at IBKR."""
    requests = []
    for symbol, pos in held.items():
        st = state["positions"].get(symbol)
        w = working.get(symbol, Working())
        if st is None or w.entries:
            continue  # not ours, or the bracket is still working (IBKR manages its child)
        qty = int(pos.position) - sum(_remaining(t) for t in w.exits)
        if qty <= 0:
            continue  # fully covered by working exits
        if w.stop is None:
            log.error("ALERT %s has no protective stop at IBKR; placing one at %.2f",
                      symbol, st["stop"])
            requests.append(StopUpdate(symbol, qty, st["stop"], "restore protective stop"))
            continue
        price = max(st["stop"], w.stop.order.auxPrice)
        if int(w.stop.order.totalQuantity) != qty or w.stop.order.auxPrice < price:
            requests.append(StopUpdate(symbol, qty, price, "sync protective stop"))
    return requests


def _raise_stops(rules, state, held, working, bars):
    """Breakeven at +xR, then trail to the latest 5m swing low. Stops only move up."""
    ex = rules["exit"]
    for symbol, pos in held.items():
        st, rth = state["positions"].get(symbol), bars.get(symbol)
        if st is None or not rth or working.get(symbol, Working()).entries:
            continue
        price, entry = rth[-1].close, pos.avgCost
        risk = entry - st["initial_stop"]
        if risk <= 0:
            continue
        if not st["breakeven"] and price >= entry + ex["breakeven_trigger_R"] * risk:
            st["breakeven"] = True
            st["stop"] = max(st["stop"], round(entry, 2))
            log.info("%s reached %gR, stop to breakeven %.2f",
                     symbol, ex["breakeven_trigger_R"], st["stop"])
            _push(f"BE {symbol}", lambda: f"stop -> ${st['stop']:.2f}")
        if st["breakeven"]:
            swing = _swing_low(rth, *rules["_swing"])
            if swing is not None and swing > st["stop"] and swing < price:
                old = st["stop"]
                st["stop"] = swing
                log.info("%s trailing stop raised to swing low %.2f", symbol, swing)
                _push(f"TRAIL {symbol}", lambda: f"stop ${old:.2f} -> ${swing:.2f}")


def _partial_profits(ib, rules, state, held, working, bars) -> list:
    """Sell a fraction at +xR, priced from the live bid. The stop is shrunk first
    so stop + exit = position.

    State machine per position: idle -> pending (order sent; on_sent records the
    share count it was sent against) -> done or back to idle. A partial order not
    filled within EXIT_STALE_SECONDS is cancelled (DAY orders also expire). Once
    no partial order is working: fewer shares than when it was sent = filled,
    done; the same shares = expired/cancelled unfilled, so the state resets and
    the target is recalculated and retried on a later pass (the reconcile first
    restores the stop to the full position), up to PARTIAL_MAX_ATTEMPTS sends."""
    ex, requests = rules["exit"], []
    for symbol, pos in held.items():
        st, rth = state["positions"].get(symbol), bars.get(symbol)
        w = working.get(symbol, Working())
        if st is None or w.entries:
            continue
        stale = [t for t in w.exits if _age_seconds(t) >= EXIT_STALE_SECONDS]
        for t in stale:
            requests.append(CancelRequest(t.order.orderId, symbol,
                                          "partial-profit order not filled in time"))
        if st.get("partial_pending") is not None and not w.exits:
            _settle_partial(symbol, st, int(pos.position))
            continue  # a retry waits for the next pass, after the stop is resized
        if st["partial_done"] or w.exits or not rth or w.stop is None:
            continue
        price, qty, entry = rth[-1].close, int(pos.position), pos.avgCost
        risk = entry - st["initial_stop"]
        if risk <= 0 or price < entry + ex["partial_profit_trigger_R"] * risk:
            continue
        # nearest share (0.3333 of 9 = 3, not 2), and always leave a remainder
        part = min(int(qty * ex["partial_profit_fraction"] + 0.5), qty - 1)
        if part < 1:
            st["partial_done"] = True  # position too small to split
            continue
        quote = _quote(ib, symbol)
        if quote is None:
            log.warning("%s partial profit deferred: no live quote", symbol)
            continue
        st["partial_basis"] = qty  # on_sent turns this into partial_pending
        requests.append(StopUpdate(symbol, qty - part, max(st["stop"], w.stop.order.auxPrice),
                                   "shrink stop before partial profit"))
        requests.append(_closing_sell(symbol, part, quote[0], "partial profit"))
    return requests


def _settle_partial(symbol: str, st: dict, shares: int):
    """The partial order is gone: decide whether it filled, or reset it for a retry."""
    basis = st.pop("partial_pending")
    attempts = st.get("partial_attempts", 0)
    if shares < basis:
        st["partial_done"] = True
        log.info("%s partial profit filled (%d -> %d shares)", symbol, basis, shares)
    elif attempts >= PARTIAL_MAX_ATTEMPTS:
        st["partial_done"] = True
        log.warning("%s partial profit not filled after %d attempts; no further partial",
                    symbol, attempts)
    else:
        log.info("%s partial profit order ended unfilled (attempt %d/%d); target will be "
                 "recalculated and retried", symbol, attempts, PARTIAL_MAX_ATTEMPTS)


def _force_close(ib, state, held, working, now) -> list:
    """Close every position we own: cancel stale exits and entries, then send a
    marketable limit SELL (from the live bid) for the remaining shares.
    Re-priced each loop (wider each attempt) until flat.

    The SELL joins the protective stop's OCA group (ocaType 2: reduce with
    block), so the stop stays live until IBKR fills the SELL and IBKR itself
    makes sure the stop and the SELL can never both fill. A stop without an OCA
    group (placed before OCA linking) is cancelled first and the bot confirms
    the cancel before sending the SELL, as before."""
    requests, first_attempts = [], 0
    for symbol, pos in held.items():
        st = state["positions"].get(symbol)
        if st is None:
            continue
        if now.time() >= ALERT_AT:
            log.error("ALERT %s: %g shares still open at %s; force close has not filled",
                      symbol, pos.position, now.strftime("%H:%M"))
        w = working.get(symbol, Working())
        if any(_age_seconds(t) < EXIT_STALE_SECONDS for t in w.exits):
            continue  # the current close attempt is still fresh
        quote = _quote(ib, symbol)
        if quote is not None:
            price = quote[0]
        else:
            rth = _rth_bars(ib, symbol, now)
            if not rth:
                log.error("ALERT %s: no live quote and no price data, cannot force close "
                          "this loop", symbol)
                continue
            price = rth[-1].close
            _warn_once(f"fc-quote:{symbol}:{now.date()}", logging.ERROR,
                       "ALERT %s: no live quote; force-close limit priced from the last 5m "
                       "close %.2f", symbol, price)
        oca = getattr(w.stop.order, "ocaGroup", "") if w.stop is not None else ""
        for t in w.exits + w.entries:
            requests.append(CancelRequest(t.order.orderId, symbol, "force close: replace"))
        if w.stop is not None and not oca:
            requests.append(CancelRequest(w.stop.order.orderId, symbol,
                                          "force close: stop replaced by closing order "
                                          "(no OCA group)"))
        st["force_attempts"] = st.get("force_attempts", 0) + 1
        slippage = LIMIT_SLIPPAGE * min(st["force_attempts"], 5)
        requests.append(_closing_sell(symbol, int(pos.position), price,
                                      f"force close (attempt {st['force_attempts']})", slippage,
                                      oca_group=oca or None))
        if st["force_attempts"] == 1:
            first_attempts += 1
    if first_attempts:
        _push("EOD Force Close", lambda: f"flattening {first_attempts} positions", "high")
    return requests


def _find_entries(ib, account, rules, state, held, working, now) -> list:
    """Scan the universe once per 5m bar, at most SCAN_BUDGET_SECONDS per loop."""
    global _scan_bar, _scan_pos, _scan_list
    bar_start = now.replace(minute=now.minute - now.minute % 5, second=0, microsecond=0)
    universe = _universe(rules, now.date())
    if bar_start != _scan_bar or universe != _scan_list:
        # new bar, or the list changed (e.g. the morning watchlist just arrived)
        _scan_bar, _scan_pos, _scan_list = bar_start, 0, universe
    if _scan_pos >= len(universe):
        return []

    max_positions = rules["risk"]["max_concurrent_positions"]
    busy = set(held) | {s for s, w in working.items() if w.entries}
    net_liq = _net_liquidation(ib, account)
    if len(busy) >= max_positions or not net_liq:
        return []
    buying_power = buying_power_usd(ib, account)
    if not buying_power or buying_power <= 0:
        _warn_once(f"buying-power:{now.date()}", logging.WARNING,
                   "buying power unavailable (or zero); entry scan skipped")
        return []
    good_till = _entry_cutoff(now, rules["time_filter"])

    intents, deadline = [], time.monotonic() + SCAN_BUDGET_SECONDS
    while _scan_pos < len(universe):
        if time.monotonic() >= deadline:
            log.info("entry scan paused at %d/%d (time budget); resuming next loop",
                     _scan_pos, len(universe))
            break
        if runstate.stop_requested() or len(busy) + len(intents) >= max_positions:
            break
        if _now() >= good_till:
            log.info("entry scan stopped at %d/%d: entry window closed", _scan_pos, len(universe))
            break
        symbol = universe[_scan_pos]
        _scan_pos += 1
        if symbol in busy or state["entered"].get(symbol) == now.date().isoformat():
            continue
        signal = evaluate(ib, symbol, rules, now)
        if not signal["pass"]:
            continue
        quote = _quote(ib, symbol)
        if quote is None:
            log.warning("signal %s skipped: no live quote to price the entry", symbol)
            continue
        intent = _size_entry(symbol, signal, rules, net_liq, buying_power, quote[1], good_till)
        if intent:
            log.info("signal %s: %s", symbol, intent.reason)
            intents.append(intent)
            buying_power -= intent.quantity * intent.limit_price  # for the next signal
    return intents


def _log_safety(result: dict):
    """Append one evaluation result to logs/safety_log.jsonl."""
    SAFETY_LOG.parent.mkdir(exist_ok=True)
    with open(SAFETY_LOG, "a") as f:
        f.write(json.dumps(result, default=str) + "\n")


def evaluate(ib: IB, symbol: str, rules: dict, now: dt.datetime) -> dict:
    """Run the universe, daily and intraday filters for one symbol.

    Returns {"symbol", "time", "pass", "reasons", "price", "stop", "gap", "rvol"}.
    Checks run cheapest first and stop at the first failure; `reasons` lists
    every check passed plus the failing one ("FAIL ..."). Each result is also
    appended to logs/safety_log.jsonl.
    """
    uf, df, inf = rules["universe_filters"], rules["daily_filters"], rules["intraday_filters"]
    result = {"symbol": symbol, "time": now.isoformat(timespec="seconds"), "pass": False,
              "reasons": [], "price": 0.0, "stop": None, "gap": None, "rvol": None}
    reasons = result["reasons"]

    def done(failure: str | None = None) -> dict:
        if failure:
            reasons.append(f"FAIL {failure}")
        result["pass"] = bool(failure is None)
        _log_safety(result)
        return result

    contract = _contract(ib, symbol)
    if contract is None:
        return done("contract not found at IBKR")

    daily = _daily(ib, contract, now.date())
    if daily is None:
        return done("no daily history")
    if daily["prior_close"] < uf["min_price_usd"]:
        return done(f"prior close {daily['prior_close']:.2f} < min {uf['min_price_usd']}")
    if df["D2_prior_close_above_sma200"]:
        if not (daily["sma200"] and daily["prior_close"] > daily["sma200"]):
            sma = f"{daily['sma200']:.2f}" if daily["sma200"] else "n/a (<200 days)"
            return done(f"D2 prior close {daily['prior_close']:.2f} not above SMA200 {sma}")
        reasons.append(f"D2 prior close {daily['prior_close']:.2f} > SMA200 {daily['sma200']:.2f}")

    pre, rth = _today_bars(ib, contract, now)
    if len(rth) < 2:
        return done("fewer than 2 completed 5m bars today")
    price = result["price"] = float(rth[-1].close)  # close of the last completed 5m bar
    gap_pct = result["gap"] = (rth[0].open / daily["prior_close"] - 1) * 100
    if price < uf["min_price_usd"]:
        return done(f"price {price:.2f} < min {uf['min_price_usd']}")
    reasons.append(f"price {price:.2f} >= {uf['min_price_usd']}")
    if gap_pct < df["D3_min_gap_pct_from_prior_close"]:
        return done(f"D3 gap {gap_pct:.1f}% < {df['D3_min_gap_pct_from_prior_close']}%")
    reasons.append(f"D3 gap {gap_pct:.1f}%")
    if df["D1_above_prior_day_high"]:
        if price <= daily["prior_high"]:
            return done(f"D1 price not above prior-day high {daily['prior_high']:.2f}")
        reasons.append(f"D1 above prior-day high {daily['prior_high']:.2f}")
    if inf["I1_above_premarket_high"]:
        if not pre:
            return done("I1 no premarket bars")
        pm_high = max(b.high for b in pre)
        if price <= pm_high:
            return done(f"I1 price not above premarket high {pm_high:.2f}")
        reasons.append(f"I1 above premarket high {pm_high:.2f}")
    if inf["I2_above_today_hod"]:
        hod = max(b.high for b in rth[:-1])
        if price <= hod:
            return done(f"I2 price not above today's high {hod:.2f}")
        reasons.append(f"I2 above today's high {hod:.2f}")

    rvol = _rvol(ib, contract, rth, inf["I3_rvol_lookback_days"], now.date())
    result["rvol"] = rvol
    if rvol is None or rvol < inf["I3_rvol_min"]:
        shown = f"{rvol:.2f}" if rvol is not None else "n/a"
        return done(f"I3 RVOL {shown} < {inf['I3_rvol_min']}")
    reasons.append(f"I3 RVOL {rvol:.2f}")

    lod = min(b.low for b in rth)
    result["stop"] = round(lod * (1 - rules["_stop_pct"] / 100), 2)
    return done()


def check_symbol(ib: IB, account: str, symbol: str) -> dict:
    """Single-symbol dry check (bot.py --check-only). Never returns orders.

    Order: position dedupe, entry window, then every filter via evaluate().
    A passing result also shows the quantity the bot would buy.
    """
    rules = load_rules()
    now = _now()
    symbol = symbol.upper()
    tf = rules["time_filter"]
    window = f"{tf['earliest_entry_et']}-{tf['latest_entry_et']} ET"

    def early(why: str) -> dict:
        result = {"symbol": symbol, "time": now.isoformat(timespec="seconds"), "pass": False,
                  "reasons": [f"FAIL {why}"], "price": 0.0}
        _log_safety(result)
        return result

    held = {p.contract.symbol.upper(): p.position for p in ib.positions(account)
            if p.contract.secType == "STK" and p.position > 0}
    if symbol in held:
        return early(f"already in position ({held[symbol]:g} shares)")
    if _working(ib).get(symbol, Working()).entries:
        return early("entry order already working")
    if load_state()["entered"].get(symbol) == now.date().isoformat():
        return early("already entered today")
    if not (now.weekday() < 5 and _in_entry_window(now, tf)):
        return early(f"outside entry window {window} (now {now:%a %H:%M} ET)")

    result = evaluate(ib, symbol, rules, now)
    result["reasons"][:0] = ["no existing position", f"time gate ok ({window})"]
    net_liq = _net_liquidation(ib, account)
    buying_power = buying_power_usd(ib, account)
    if result["pass"] and net_liq and buying_power:
        quote = _quote(ib, symbol)
        intent = (_size_entry(symbol, result, rules, net_liq, buying_power, quote[1])
                  if quote else None)
        result["quantity"] = intent.quantity if intent else 0
    return result


def _size_entry(symbol: str, signal: dict, rules: dict, net_liq: float, buying_power: float,
                ask: float, good_till: dt.datetime | None = None) -> OrderIntent | None:
    """Shares = the smallest of the rules.json risk/size limits, the per-order caps
    (rules.json risk, via risk.load_limits) and the buying power. The limit is a marketable 0.2% through the live ask.
    Skipped when the estimated commission is too large a share of the trade's R."""
    risk = rules["risk"]
    limit = round(ask * (1 + LIMIT_SLIPPAGE), 2)
    per_share_risk = limit - signal["stop"]
    if per_share_risk <= 0:
        return None
    qty = math.floor(min(
        net_liq * risk["max_risk_per_trade_pct"] / 100 / per_share_risk,
        net_liq * risk["max_position_size_pct_of_portfolio"] / 100 / limit,
        _caps["max_quantity_per_order"],
        _caps["max_order_value"] / limit,
        buying_power / limit,
    ))
    if qty < 1:
        log.info("signal %s skipped: 1 share at %.2f exceeds the order caps or buying power "
                 "%.2f", symbol, limit, buying_power)
        return None
    drag = commission_drag(qty, limit, signal["stop"], risk.get("max_commission_pct_of_risk"))
    if drag:
        log.info("signal %s skipped: %s", symbol, drag)
        return None
    reason = (f"trend join: gap {signal['gap']:.1f}%, rvol {signal['rvol']:.1f}, "
              f"stop {signal['stop']:.2f}")
    return OrderIntent(symbol, "BUY", qty, limit, reason=reason, stop_price=signal["stop"],
                       good_till=good_till)


def on_sent(intent: OrderIntent):
    """Called by the bot after an order is really sent (not in dry run)."""
    state = load_state()
    symbol = intent.symbol.upper()
    if intent.action == "BUY":
        state["positions"][symbol] = {"initial_stop": intent.stop_price,
                                      "stop": intent.stop_price,
                                      "breakeven": False, "partial_done": False}
        state["entered"][symbol] = dt.datetime.now(ET).date().isoformat()
    elif intent.reason == "partial profit" and symbol in state["positions"]:
        st = state["positions"][symbol]
        basis = st.pop("partial_basis", None)
        if basis is None:
            st["partial_done"] = True  # no share count to settle against: one-shot, as before
        else:
            st["partial_pending"] = basis
            st["partial_attempts"] = st.get("partial_attempts", 0) + 1
    _save_state(state)
