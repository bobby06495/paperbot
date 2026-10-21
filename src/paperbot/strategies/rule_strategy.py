"""The one generic strategy: runs any rules.json strategy profile.

configure(profile) is called by the runner after the start-up selector chose a
strategy and before it connects; load_profile() falls back to the saved choice
(scheduled runs) on first use. decide() is called every pass and returns
requests (core/risk.py) that the runner sends through the risk guard.

Each pass, in priority order (only for the strategy's own orders and state):
  1. Reconcile: every position this strategy owns has a protective SELL STP at
     IBKR sized to the shares not already covered by a working exit; a missing
     stop is re-created (ALERT), a stop whose position is gone is cancelled.
     Runs at any hour.
  2. Regular hours, before force close: manage blocks (breakeven, swing-low
     trail, partial profit with retries).
  3. From time_filter.force_close_et (force_close exit): flatten with a
     marketable SELL in the stop's OCA group, re-priced until flat. Before
     that, signal exits (close_above_sma, time_stop, ...) close single
     positions the same way. A multi_day strategy has no force close and
     holds overnight on its GTC stops.
  4. In the entry window (inclusive, to the minute): scan the universe once per
     bar of the strategy's timeframe within a time budget, run the entry blocks
     (cheapest first, first failure stops) and size passing signals with
     core/sizing.py. Entries are GTD and expire one minute after the window;
     the sweep cancels any still working.

Every evaluation is appended to the strategy's safety_log.jsonl. Limit prices
come from live quotes (core/market_data.py); signals from completed bars. A
block that crashes (a bug or unexpected data) fails that symbol cleanly instead
of aborting the pass. The entry-scan position belongs to one strategy key and
restarts when the running strategy changes.
"""
import dataclasses
import datetime as dt
import json
import logging
import re
import time
from pathlib import Path

from ib_async import IB

from paperbot import paths
from paperbot.blocks.context import RESULT_DEFAULTS, EvalContext, Fail
from paperbot.core import market_data as md
from paperbot.core import orders, selector
from paperbot.core import profile as profiles
from paperbot.core.account import buying_power_usd, net_liquidation_usd
from paperbot.core.common import ET, RTH_CLOSE, RTH_OPEN, log, warn_once
from paperbot.core.orders import Working
from paperbot.core.risk import OrderIntent
from paperbot.core.sizing import size_entry
from paperbot.runtime import runstate
from paperbot.runtime.runstate import StateCorrupt, read_json_state, write_json_atomic

RULES_FILE = paths.RULES_FILE
CONFIG_FILE = paths.CONFIG_FILE
WATCHLIST_FILE = paths.WATCHLIST_FILE  # today's gappers from morning_prefilter.py
STATE: Path | None = None  # None = the profile's state file (tests point it elsewhere)
SAFETY_LOG: Path | None = None  # None = the profile's safety log
ACTIVE: str | None = None  # strategy key to load when not configured; None = saved choice
SCAN_BUDGET_SECONDS = 10  # max entry-scan time per pass; the scan resumes next pass

_profile: profiles.StrategyProfile | None = None
_scan_key: str | None = None  # strategy the scan position below belongs to
_scan_bar: dt.datetime | None = None
_scan_pos = 0
_scan_list: list[str] = []


# ---------------------------------------------------------------- profile / state

def configure(profile: profiles.StrategyProfile):
    """Run `profile` from now on (called by the runner before it connects)."""
    global _profile, _scan_key, _scan_bar, _scan_pos, _scan_list
    _profile = profile
    _scan_key, _scan_bar, _scan_pos, _scan_list = None, None, 0, []
    log.info("strategy %s (%s) loaded from %s", profile.label, profile.key, RULES_FILE.name)


def load_profile() -> profiles.StrategyProfile:
    """The configured profile; on first use without configure(), the saved choice."""
    if _profile is None:
        config = json.loads(CONFIG_FILE.read_text())
        key = ACTIVE or selector.unattended_key(RULES_FILE)
        try:
            configure(profiles.load(key, RULES_FILE, config))
        except profiles.ConfigError as e:
            log.error("ALERT refusing to start: %s", e)
            raise SystemExit(f"Refusing to start: {e}") from None
    return _profile


def state_path() -> Path:
    return STATE or load_profile().state_file


def safety_log_path() -> Path:
    return SAFETY_LOG or load_profile().safety_log


def load_state() -> dict:
    """Per-position exit state. Fails closed (StateCorrupt) if the file is corrupt."""
    path = state_path()
    state = read_json_state(path) or {}
    state.setdefault("positions", {})  # SYMBOL -> {initial_stop, stop, breakeven, partial_done}
    state.setdefault("entered", {})  # SYMBOL -> date of last entry
    state.setdefault("entry_counts", {})  # SYMBOL -> [date, entries that day]
    if not isinstance(state["positions"], dict) or not isinstance(state["entered"], dict):
        raise StateCorrupt(path, "positions/entered must be objects")
    return state


def _save_state(state: dict):
    write_json_atomic(state_path(), state)


def entries_today(state: dict, symbol: str, today: str) -> int:
    """Entries sent for `symbol` on ET date `today` (an older state has only "entered")."""
    counts = state.get("entry_counts", {}).get(symbol)
    if isinstance(counts, list) and len(counts) == 2 and counts[0] == today:
        return int(counts[1])
    return 1 if state["entered"].get(symbol) == today else 0


def refs() -> tuple[str, str, str]:
    """orderRef tags (entry, stop, exit) of the running strategy."""
    return load_profile().refs


# ---------------------------------------------------------------- universe

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
    """Index in the watchlist header ("# Index: Nasdaq-100"); lists written before the
    index was recorded were always S&P 500."""
    path = path or WATCHLIST_FILE
    try:
        header = path.read_text().splitlines()[:8]
    except FileNotFoundError:
        return "S&P 500"
    for line in header:
        match = re.match(r"# Index: (.+?)\s*$", line)
        if match:
            return match.group(1)
    return "S&P 500"


def _universe(profile: profiles.StrategyProfile, today: dt.date) -> list[str]:
    """The symbols to scan today: today's watchlist when it was built for this strategy's
    index, else the fallback list."""
    universe = profile.universe
    if "symbols" in universe:
        return list(universe["symbols"])
    if "file" in universe:
        return read_symbols(paths.CONFIG_DIR / universe["file"])
    fallback = paths.CONFIG_DIR / universe.get("fallback_file", "universe.txt")
    index = universe.get("index", "S&P 500")
    if watchlist_date() == today:
        built_for = watchlist_index()
        if built_for == index:
            warn_once(f"watchlist:{today}", logging.INFO, "scanning %s (generated today, %s)",
                      WATCHLIST_FILE.name, index)
            return read_symbols(WATCHLIST_FILE)
        warn_once(f"watchlist-index:{today}:{built_for}", logging.WARNING,
                  "%s was generated for %s but this strategy screens %s; scanning %s until "
                  "the next prefilter run", WATCHLIST_FILE.name, built_for, index, fallback.name)
    else:
        warn_once(f"no-watchlist:{today}", logging.WARNING,
                  "%s not generated today (run morning_prefilter.py after 9:30 ET); "
                  "scanning %s instead", WATCHLIST_FILE.name, fallback.name)
    if not fallback.exists():
        warn_once("universe", logging.WARNING,
                  "%s missing: add the %s symbols (one per line) to scan for entries",
                  fallback.name, universe.get("index", "universe"))
        return []
    return read_symbols(fallback)


# ---------------------------------------------------------------- decide

def _now() -> dt.datetime:
    return dt.datetime.now(ET)


def decide(ib: IB, account: str) -> list:
    profile = load_profile()
    if ACTIVE and profile.key != ACTIVE:
        log.error("ALERT strategy mismatch: running %s but %s is selected; pass skipped",
                  profile.key, ACTIVE)
        raise RuntimeError(f"strategy mismatch: {profile.key} != {ACTIVE}")
    now = _now()
    held = {p.contract.symbol.upper(): p for p in ib.positions(account)
            if p.contract.secType == "STK" and p.position > 0}
    working = orders.working_orders(ib, profile.refs)
    state = load_state()
    orders.adopt_and_forget(state, held, working)

    tf = profile.time_filter
    close_at = tf.force_close_time()
    session = now.weekday() < 5 and RTH_OPEN <= now.time() < RTH_CLOSE
    force_close = session and close_at is not None and now.time() >= close_at
    bars: dict[str, list] = {}
    if session and not force_close and profile.manage:
        bars = {s: md.rth_bars(ib, s, now, profile.timeframe)
                for s in held if s in state["positions"]}
        orders.raise_stops(profile, state, held, working, bars)

    requests = orders.cancel_orphan_stops(held, working)
    if session and now >= tf.entry_cutoff(now):
        requests += orders.sweep_entries(working)
    if force_close:
        closing = orders.force_close(ib, profile, state, held, working, now)
        # symbols being force-closed get no new stop next to their closing SELL
        closed = {r.symbol for r in closing if isinstance(r, OrderIntent)}
        requests += orders.reconcile_stops(state, {s: p for s, p in held.items()
                                                   if s not in closed}, working)
        requests += closing
    else:
        exits = orders.signal_exits(ib, profile, state, held, working, now) if session else []
        # symbols being closed by an exit signal get no new stop next to their SELL
        closed = {r.symbol for r in exits if isinstance(r, OrderIntent)}
        requests += orders.reconcile_stops(state, {s: p for s, p in held.items()
                                                   if s not in closed}, working)
        requests += exits
    if session and not force_close:
        requests += orders.partial_profits(ib, profile, state, held, working, bars)
        if tf.in_entry_window(now):
            requests += _find_entries(ib, account, profile, state, held, working, now)
    elif not session and profile.holding == "intraday":
        for symbol in held:
            if symbol in state["positions"] and now.time() >= RTH_CLOSE:
                warn_once(f"overnight:{symbol}:{now.date()}", logging.ERROR,
                          "ALERT %s still open after the close; protective stop kept at IBKR",
                          symbol)
                w = working.get(symbol, Working())
                if w.exits or w.entries:
                    warn_once(f"live-after-close:{symbol}:{now.date()}", logging.ERROR,
                              "ALERT %s: a DAY entry/exit is still live after the close; the "
                              "stop is restored only once IBKR confirms it dead", symbol)
    _save_state(state)
    md.release_quotes(ib, set(held) | set(working))
    return orders.dedupe_cancels(requests)


def _bar_start(now: dt.datetime, bar: dt.timedelta) -> dt.datetime:
    minutes = max(1, int(bar.total_seconds() // 60))
    floor = (now.hour * 60 + now.minute) // minutes * minutes
    return now.replace(hour=floor // 60, minute=floor % 60, second=0, microsecond=0)


def _find_entries(ib, account, profile, state, held, working, now) -> list:
    """Scan the universe once per bar, at most SCAN_BUDGET_SECONDS per pass."""
    global _scan_key, _scan_bar, _scan_pos, _scan_list
    bar_start = _bar_start(now, profile.scan_every)
    universe = _universe(profile, now.date())
    if bar_start != _scan_bar or universe != _scan_list or profile.key != _scan_key:
        # new bar, the list changed (e.g. the morning watchlist just arrived),
        # or another strategy is running now: start the scan over
        _scan_key, _scan_bar, _scan_pos, _scan_list = profile.key, bar_start, 0, universe
    if _scan_pos >= len(universe):
        return []

    max_positions = profile.limits.max_concurrent_positions or len(universe)
    busy = set(held) | {s for s, w in working.items() if w.entries}
    net_liq = net_liquidation_usd(ib, account)
    if len(busy) >= max_positions or not net_liq:
        return []
    buying_power = buying_power_usd(ib, account)
    if not buying_power or buying_power <= 0:
        warn_once(f"buying-power:{now.date()}", logging.WARNING,
                  "buying power unavailable (or zero); entry scan skipped")
        return []
    good_till = profile.time_filter.entry_cutoff(now)

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
        if (symbol in busy or entries_today(state, symbol, now.date().isoformat())
                >= profile.max_entries_per_symbol_per_day):
            continue
        signal = evaluate(ib, symbol, profile, now)
        if not signal["pass"]:
            continue
        quote = md.quote(ib, symbol)
        if quote is None:
            log.warning("signal %s skipped: no live quote to price the entry", symbol)
            continue
        intent = _size_entry(symbol, signal, profile, net_liq, buying_power, quote[1], good_till)
        if intent:
            log.info("signal %s: %s", symbol, intent.reason)
            intents.append(intent)
            buying_power -= intent.quantity * intent.limit_price  # for the next signal
    return intents


# ---------------------------------------------------------------- evaluate

def _log_safety(result: dict):
    """Append one evaluation result to the strategy's safety_log.jsonl."""
    path = safety_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(result, default=str) + "\n")


def evaluate(ib: IB, symbol: str, profile: profiles.StrategyProfile, now: dt.datetime) -> dict:
    """Run the profile's entry blocks for one symbol, in order.

    Returns {"symbol", "time", "pass", "reasons", "price", "stop", "gap", "rvol"}.
    Checks stop at the first failure; `reasons` lists every check passed plus the
    failing one ("FAIL ..."). On a pass the stop block sets "stop". Each result is
    also appended to the safety log.
    """
    result = {"symbol": symbol, "time": now.isoformat(timespec="seconds"),
              **RESULT_DEFAULTS, "reasons": []}
    reasons = result["reasons"]

    def done(failure: str | None = None) -> dict:
        if failure:
            reasons.append(f"FAIL {failure}")
        result["pass"] = bool(failure is None)
        _log_safety(result)
        return result

    contract = md.contract(ib, symbol)
    if contract is None:
        return done("contract not found at IBKR")
    ctx = EvalContext(ib, contract, symbol, now, profile, result)
    block = None
    try:
        for block in profile.entry:
            reason = block.check(ctx)
            if reason:
                reasons.append(reason)
        block = profile.stop
        result["stop"] = profile.stop.compute(ctx)
    except Fail as failure:
        return done(str(failure))
    except Exception as e:  # noqa: BLE001 - a broken block fails the symbol, not the pass
        name = getattr(block, "type", "block")
        log.warning("%s: %s block error %r; symbol skipped", symbol, name, e)
        return done(f"{name}: error {e!r}")
    return done()


def check_symbol(ib: IB, account: str, symbol: str) -> dict:
    """Single-symbol dry check (--check-only). Never returns orders.

    Order: position dedupe, entry window, then every entry block via evaluate().
    A passing result also shows the quantity the bot would buy.
    """
    profile = load_profile()
    now = _now()
    symbol = symbol.upper()
    tf = profile.time_filter
    window = tf.window()

    def early(why: str) -> dict:
        result = {"symbol": symbol, "time": now.isoformat(timespec="seconds"), "pass": False,
                  "reasons": [f"FAIL {why}"], "price": 0.0}
        _log_safety(result)
        return result

    held = {p.contract.symbol.upper(): p.position for p in ib.positions(account)
            if p.contract.secType == "STK" and p.position > 0}
    if symbol in held:
        return early(f"already in position ({held[symbol]:g} shares)")
    if orders.working_orders(ib, profile.refs).get(symbol, Working()).entries:
        return early("entry order already working")
    done = entries_today(load_state(), symbol, now.date().isoformat())
    if done >= profile.max_entries_per_symbol_per_day:
        return early("already entered today" if profile.max_entries_per_symbol_per_day == 1
                     else f"already entered {done} times today "
                          f"(max {profile.max_entries_per_symbol_per_day})")
    if not (now.weekday() < 5 and tf.in_entry_window(now)):
        return early(f"outside entry window {window} (now {now:%a %H:%M} ET)")

    result = evaluate(ib, symbol, profile, now)
    result["reasons"][:0] = ["no existing position", f"time gate ok ({window})"]
    net_liq = net_liquidation_usd(ib, account)
    buying_power = buying_power_usd(ib, account)
    if result["pass"] and net_liq and buying_power:
        quote = md.quote(ib, symbol)
        intent = (_size_entry(symbol, result, profile, net_liq, buying_power, quote[1])
                  if quote else None)
        result["quantity"] = intent.quantity if intent else 0
    return result


def _entry_reason(profile: profiles.StrategyProfile, signal: dict) -> str:
    fields = {k: signal.get(k) for k in ("price", "stop", "gap", "rvol")}
    try:
        return profile.entry_reason.format(label=profile.label, **fields)
    except (KeyError, ValueError, TypeError):
        return f"{profile.label}: stop {signal['stop']:.2f}"


def _size_entry(symbol: str, signal: dict, profile: profiles.StrategyProfile, net_liq: float,
                buying_power: float, ask: float,
                good_till: dt.datetime | None = None) -> OrderIntent | None:
    """core/sizing.size_entry with this strategy's limits and order note."""
    return size_entry(symbol, signal, profile.limits, net_liq, buying_power, ask,
                      _entry_reason(profile, signal), good_till)


def with_limits(profile: profiles.StrategyProfile, **changes) -> profiles.StrategyProfile:
    """A copy of `profile` with some limits changed (what-if sizing, tests)."""
    return dataclasses.replace(profile, limits=dataclasses.replace(profile.limits, **changes))


def on_sent(intent: OrderIntent):
    """Called by the runner after an order is really sent (not in dry run)."""
    state = load_state()
    symbol = intent.symbol.upper()
    if intent.action == "BUY":
        state["positions"][symbol] = {"initial_stop": intent.stop_price,
                                      "stop": intent.stop_price,
                                      "breakeven": False, "partial_done": False,
                                      "opened_at": dt.datetime.now(ET).isoformat(timespec="seconds")}
        today = dt.datetime.now(ET).date().isoformat()
        state["entry_counts"][symbol] = [today, entries_today(state, symbol, today) + 1]
        state["entered"][symbol] = today
    elif intent.reason == "partial profit" and symbol in state["positions"]:
        st = state["positions"][symbol]
        basis = st.pop("partial_basis", None)
        if basis is None:
            st["partial_done"] = True  # no share count to settle against: one-shot
        else:
            st["partial_pending"] = basis
            st["partial_attempts"] = st.get("partial_attempts", 0) + 1
    _save_state(state)
