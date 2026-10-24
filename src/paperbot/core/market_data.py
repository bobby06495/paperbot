"""Market data for the strategy engine: contracts, historical bars by timeframe,
per-day caches and streaming top-of-book quotes.

Timeframes (rules.json "timeframe") map to IBKR bar sizes in TIMEFRAMES. Only
completed bars are returned (bar start + bar length <= now).
"""
import datetime as dt
import logging
import time

from ib_async import IB, Stock

from paperbot.core.common import ET, RTH_CLOSE, RTH_OPEN, finite, log, warn_once

HIST_TIMEOUT = 10  # seconds per historical data request
QUOTE_TIMEOUT = 3.0  # seconds to wait for the first live bid/ask of a new subscription
TIMEFRAMES = {  # rules.json timeframe -> (IBKR bar size, bar length)
    "1m": ("1 min", dt.timedelta(minutes=1)),
    "5m": ("5 mins", dt.timedelta(minutes=5)),
    "15m": ("15 mins", dt.timedelta(minutes=15)),
    "1h": ("1 hour", dt.timedelta(hours=1)),
    "1d": ("1 day", dt.timedelta(days=1)),
}

_contracts: dict[str, Stock] = {}
_daily_cache: dict[str, tuple[dt.date, list]] = {}
_volume_cache: dict[tuple[str, str, int], tuple[dt.date, list]] = {}
_tickers: dict = {}  # SYMBOL -> streaming ib_async Ticker (reqMktData)


def clear_caches():
    for cache in (_contracts, _daily_cache, _volume_cache, _tickers):
        cache.clear()


def contract(ib: IB, symbol: str) -> Stock | None:
    if symbol not in _contracts:
        c = Stock(symbol, "SMART", "USD")
        ib.qualifyContracts(c)
        if not c.conId:
            warn_once(f"qualify:{symbol}", logging.WARNING, "could not qualify %s, skipping", symbol)
            return None
        _contracts[symbol] = c
    return _contracts[symbol]


def bars(ib: IB, c: Stock, duration: str, size: str, rth: bool) -> list:
    return ib.reqHistoricalData(c, "", duration, size, "TRADES", useRTH=rth,
                                formatDate=2, timeout=HIST_TIMEOUT) or []


def et(bar) -> dt.datetime:
    return bar.date.astimezone(ET)


def daily_bars(ib: IB, c: Stock, today: dt.date) -> list:
    """Up to a year of completed daily bars before today, cached per day."""
    cached = _daily_cache.get(c.symbol)
    if cached and cached[0] == today:
        return cached[1]
    prior = [b for b in bars(ib, c, "1 Y", "1 day", True) if b.date < today]
    _daily_cache[c.symbol] = (today, prior)
    return prior


def session_bars(ib: IB, c: Stock, now: dt.datetime, timeframe: str) -> tuple[list, list]:
    """Today's completed bars of `timeframe`, split into (premarket, regular hours)."""
    size, length = TIMEFRAMES[timeframe]
    done = [b for b in bars(ib, c, "1 D", size, False)
            if et(b).date() == now.date() and et(b) + length <= now]
    pre = [b for b in done if et(b).time() < RTH_OPEN]
    rth = [b for b in done if RTH_OPEN <= et(b).time() < RTH_CLOSE]
    return pre, rth


def rth_bars(ib: IB, symbol: str, now: dt.datetime, timeframe: str) -> list:
    c = contract(ib, symbol)
    return session_bars(ib, c, now, timeframe)[1] if c else []


def volume_history(ib: IB, c: Stock, today: dt.date, lookback: int,
                   timeframe: str) -> list[list[tuple[dt.time, float]]]:
    """(bar time, volume) per regular-hours bar for the last `lookback` days before
    today, one list per day, cached per day."""
    key = (c.symbol, timeframe, lookback)
    cached = _volume_cache.get(key)
    if not cached or cached[0] != today:
        by_day: dict[dt.date, list] = {}
        for b in bars(ib, c, f"{lookback + 6} D", TIMEFRAMES[timeframe][0], True):
            if et(b).date() < today:
                by_day.setdefault(et(b).date(), []).append((et(b).time(), b.volume))
        cached = (today, [by_day[d] for d in sorted(by_day)[-lookback:]])
        _volume_cache[key] = cached
    return cached[1]


def quote(ib: IB, symbol: str) -> tuple[float, float] | None:
    """Live (bid, ask) from a streaming reqMktData subscription, kept open between
    loops. None if IBKR sends no valid two-sided quote within QUOTE_TIMEOUT
    (no market-data permission, halted, crossed or one-sided book)."""
    c = contract(ib, symbol)
    if c is None:
        return None
    ticker = _tickers.get(symbol)
    if ticker is None:
        ticker = _tickers[symbol] = ib.reqMktData(c, "", False, False)
    deadline = time.monotonic() + QUOTE_TIMEOUT
    while True:
        bid, ask = finite(ticker.bid), finite(ticker.ask)
        halted = finite(getattr(ticker, "halted", None))  # NaN unknown, 0 trading, >0 halted
        if bid and ask and 0 < bid <= ask and not halted:
            return bid, ask
        if time.monotonic() >= deadline:
            warn_once(f"quote:{symbol}:{dt.datetime.now(ET).date()}", logging.WARNING,
                      "%s: no valid live quote from IBKR (bid %s, ask %s, halted %s)",
                      symbol, ticker.bid, ticker.ask, halted)
            return None
        ib.sleep(0.1)


def release_quotes(ib: IB, keep: set[str]):
    """Cancel streaming quotes no longer needed (IBKR limits market-data lines)."""
    for symbol in [s for s in _tickers if s not in keep]:
        ticker = _tickers.pop(symbol)
        try:
            ib.cancelMktData(ticker.contract)
        except Exception as e:  # noqa: BLE001 - a dropped connection already ended it
            log.debug("cancelMktData %s: %r", symbol, e)
