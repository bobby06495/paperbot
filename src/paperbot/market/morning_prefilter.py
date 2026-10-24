"""Morning gap prefilter: today's biggest gappers of the configured index -> watchlist.txt.

    python -m paperbot.market.morning_prefilter [--strategy KEY] [--min-gap PCT]
                                                [--min-price USD] [--dry-run]
    (paperbot-prefilter is the same command; output goes to data/watchlist.txt)

What it screens comes from the watchlist strategy in rules.json (market/universe.py):
the running/saved strategy if its universe is the watchlist, else the default or the
first such strategy (--strategy picks one). Its universe names the index ("S&P 500" or
"Nasdaq-100") and top_n; the gap and price thresholds are its own entry blocks
(min_gap_pct, min_price), so the screen and the entry checks always agree.
--min-gap / --min-price override them for one run.

Run on a trading day after the 9:30 ET open and before the bot's entry
window (10:05 ET). Steps:
  1. live constituents of the index from Wikipedia (sp500_tickers.py)
  2. top_n of them by current market cap (yfinance, 5 worker threads)
  3. regular-hours daily bars for those from IB Gateway: a READ-ONLY connection
     with its own clientId (config.json client_id + IB_CLIENT_ID_OFFSET), so it
     runs next to the bot and can never place an order
  4. gap = today's open vs the previous session's close (the bot's gap block uses
     the same IBKR data); price = today's latest print
  5. survivors (gap >= min gap, price >= min price), sorted by gap, capped at 20,
     written to watchlist.txt (skipped with --dry-run); the header records the
     index, so the bot ignores a list built for another one

A failed run never writes the watchlist: Wikipedia, Yahoo or IB Gateway unavailable,
or price data missing for FAIL_RATIO of the symbols. The previous watchlist is kept,
so the bot scans an earlier list from today, or falls back to the static
universe.txt, instead of an empty list.

strategy.py scans watchlist.txt when it was generated today (ET) and falls
back to universe.txt otherwise. A JSON summary goes to stdout, alerts to
stderr. Unless --dry-run, the result (survivors, failure naming the step that
failed, or a degradation alert) is also pushed via notify.py. Exit 0 on
success, 1 on failure.
"""
import argparse
import asyncio
import dataclasses
import datetime as dt
import json
import math
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from zoneinfo import ZoneInfo

import yfinance as yf

from paperbot import paths
from paperbot.core.risk import ConfigError
from paperbot.market import universe
from paperbot.market.sp500_tickers import get_live_index_tickers, to_yahoo
from paperbot.reporting.notify import notify

WATCHLIST_PATH = paths.WATCHLIST_FILE
DEFAULT_MIN_GAP_PCT = universe.DEFAULT_MIN_GAP  # when the strategy has no gap block
DEFAULT_MIN_PRICE = universe.DEFAULT_MIN_PRICE
MAX_SURVIVORS = 20
TOP_N = universe.DEFAULT_TOP_N  # default; a strategy's universe.top_n overrides it
YF_THREADS = 5  # keep low: each thread holds sockets/file descriptors
ET = ZoneInfo("America/New_York")

# clientIds in use: bot +0, kill switch +1, --check-only +2, backtester +3
IB_CLIENT_ID_OFFSET = 4
IB_CONNECT_TIMEOUT = 15  # seconds
IB_REQUEST_TIMEOUT = 10  # seconds per contract lookup / historical request
IB_CONCURRENCY = 8  # simultaneous historical requests (IBKR allows up to 50)
IB_DURATION = "5 D"  # enough to contain the previous session after a holiday

FAIL_RATIO = 0.95  # this share of symbols without data = failed run, no watchlist
DEGRADED_RATIO = 0.30  # this share = watchlist written, with an alert
SAMPLE_ERRORS = 3  # failed symbols quoted in a failure message


class IbkrUnavailable(RuntimeError):
    """IB Gateway could not be reached; str() is the message for the alert."""


@dataclasses.dataclass(frozen=True)
class DailyBar:
    date: dt.date
    open: float
    close: float


def market_caps(ibkr_symbols: list[str]) -> dict[str, float]:
    """Current market cap per symbol (IBKR format). Symbols without one are left out."""
    tickers = yf.Tickers(" ".join(to_yahoo(s) for s in ibkr_symbols))

    def cap(symbol: str) -> tuple[str, float | None]:
        try:
            value = tickers.tickers[to_yahoo(symbol)].fast_info.market_cap
            return symbol, float(value) if value and not math.isnan(value) else None
        except Exception:  # noqa: BLE001 - any Yahoo failure just drops this symbol
            return symbol, None

    with ThreadPoolExecutor(max_workers=YF_THREADS) as pool:
        return {s: c for s, c in pool.map(cap, ibkr_symbols) if c}


def _as_date(value) -> dt.date:
    return value.date() if isinstance(value, dt.datetime) else value


async def _fetch_all(ib, symbols: list[str]) -> tuple[dict[str, list[DailyBar]], dict[str, str]]:
    from ib_async import Stock

    gate = asyncio.Semaphore(IB_CONCURRENCY)

    async def one(symbol: str) -> tuple[str, list[DailyBar] | None, str | None]:
        async with gate:
            contract = Stock(symbol, "SMART", "USD")
            try:
                await asyncio.wait_for(ib.qualifyContractsAsync(contract), IB_REQUEST_TIMEOUT)
                if not contract.conId:
                    return symbol, None, "contract not found at IBKR"
                raw = await ib.reqHistoricalDataAsync(
                    contract, "", IB_DURATION, "1 day", "TRADES", useRTH=True, formatDate=1,
                    timeout=IB_REQUEST_TIMEOUT)
            except asyncio.TimeoutError:
                return symbol, None, "IBKR request timed out"
            except Exception as e:  # noqa: BLE001 - one bad symbol must not stop the screen
                return symbol, None, f"IBKR error {e!r}"
            if not raw:
                return symbol, None, "no daily bars from IBKR (timed out or no data permission)"
            return symbol, [DailyBar(_as_date(b.date), float(b.open), float(b.close))
                            for b in raw], None

    bars, errors = {}, {}
    for symbol, got, error in await asyncio.gather(*(one(s) for s in symbols)):
        if error:
            errors[symbol] = error
        else:
            bars[symbol] = got
    return bars, errors


def ibkr_daily_bars(symbols: list[str], cfg: dict | None = None
                    ) -> tuple[dict[str, list[DailyBar]], dict[str, str]]:
    """Recent regular-hours daily bars per symbol from IB Gateway (today's bar is the
    session so far), and an error per symbol that has none. Read-only connection.
    IbkrUnavailable if the Gateway cannot be reached; ConfigError for a bad config."""
    from ib_async import IB

    from paperbot.core import broker

    cfg = cfg if cfg is not None else json.loads(paths.CONFIG_FILE.read_text())
    conn = broker.settings(cfg)
    client_id = conn.client_id + IB_CLIENT_ID_OFFSET
    ib = IB()
    try:
        ib.connect(conn.host, conn.port, clientId=client_id, timeout=IB_CONNECT_TIMEOUT,
                   readonly=True)
    except (OSError, asyncio.TimeoutError, TimeoutError, ConnectionError) as e:
        raise IbkrUnavailable(f"IB Gateway not reachable at {conn.host}:{conn.port} "
                              f"(clientId {client_id}): {e!r}") from None
    try:
        return ib.run(_fetch_all(ib, symbols))
    finally:
        ib.disconnect()


def latest_date(data: dict[str, list[DailyBar]]) -> dt.date | None:
    return max((b.date for bars in data.values() for b in bars), default=None)


def screen(data: dict[str, list[DailyBar]], symbols: list[str], min_gap: float,
           min_price: float, today: dt.date, errors: dict[str, str] | None = None) -> dict:
    """Apply the price and gap filters. Returns survivors (sorted, capped) and buckets.
    `errors` explains symbols missing from `data` (shown in "failed")."""
    errors = errors or {}
    survivors, failed = [], []
    below_gap = below_price = 0
    for symbol in symbols:
        try:
            if symbol not in data:
                raise ValueError(errors.get(symbol, "no daily bars"))
            bars = sorted((b for b in data[symbol]
                           if all(math.isfinite(v) and v > 0 for v in (b.open, b.close))),
                          key=lambda b: b.date)
            if not bars or bars[-1].date != today:
                latest = bars[-1].date if bars else None
                raise ValueError(f"no bar for today (latest {latest})")
            if len(bars) < 2:
                raise ValueError("no previous session bar")
            prev_close, today_open, last = bars[-2].close, bars[-1].open, bars[-1].close
        except ValueError as e:
            failed.append(f"{symbol}: {e}")
            continue
        gap_pct = (today_open - prev_close) / prev_close * 100
        if last < min_price:
            below_price += 1
        elif gap_pct < min_gap:
            below_gap += 1
        else:
            survivors.append({"symbol": symbol, "gap_pct": gap_pct, "open": today_open,
                              "prev_close": prev_close, "last": last,
                              "change_pct": (last - prev_close) / prev_close * 100})
    survivors.sort(key=lambda s: s["gap_pct"], reverse=True)
    return {"survivors": survivors[:MAX_SURVIVORS], "passed": len(survivors),
            "below_gap": below_gap, "below_price": below_price, "failed": failed}


def write_watchlist(path: Path, survivors: list[dict], passed: int, screened: int,
                    min_gap: float, min_price: float, now: dt.datetime,
                    index: str = universe.DEFAULT_INDEX):
    """Overwrite the watchlist atomically. The bot reads the date in line 1 and the
    index in the "# Index:" line."""
    lines = [
        f"# Auto-generated by morning_prefilter.py at {now:%Y-%m-%d %H:%M %Z}",
        f"# Index: {index}",
        f"# Filters: gap >= {min_gap:g}%, price >= ${min_price:g}",
        "# Source: Wikipedia index members, Yahoo market caps, IBKR daily bars",
        f"# Survivors: {len(survivors)} (capped at {MAX_SURVIVORS}; {passed} passed) "
        f"of {screened} Dynamic Screened Assets",
        "#",
        "# ticker  # gap +X.XX%  open X.XX prev X.XX",
    ]
    lines += [f"{s['symbol']}  # gap {s['gap_pct']:+.2f}%  open ${s['open']:.2f}  "
              f"prev ${s['prev_close']:.2f}" for s in survivors]
    path.parent.mkdir(parents=True, exist_ok=True)  # data/
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n")
    os.replace(tmp, path)


def failure(failed: int, total: int) -> bool:
    """Too few symbols with data for the watchlist to be trusted."""
    return bool(total) and failed / total >= FAIL_RATIO


def alerts(failed: int, total: int) -> list[str]:
    """Alerts for a run that still writes its watchlist (below FAIL_RATIO)."""
    if not total or failure(failed, total):
        return []
    if failed / total >= DEGRADED_RATIO:
        return [f"ALERT: IBKR price data degraded (failed {failed}/{total})"]
    return []


def _push(title: str, body, priority: str = "default"):
    """notify() that can never break the prefilter; `body` is a callable."""
    try:
        notify(title, body(), priority)
    except Exception:  # noqa: BLE001
        pass


def push_summary(summary: dict, now: dt.datetime):
    """Push the result of a real (non --dry-run) run: survivors, or the failure."""
    stamp = f"{now:%H:%M} ET"
    if summary["success"]:
        def body() -> str:
            lines = [f"{summary['survivors_count']}/{summary['total_screened']} survivors "
                     f"in {summary['elapsed_seconds']}s"]
            return "\n".join(lines + [f"- {s}" for s in summary["top_20_survivors"]])
        _push(f"Prefilter {stamp}", body, "default")
    else:
        _push(f"Prefilter FAILED {stamp}", lambda: str(summary.get("error", "unknown error")),
              "high")


def setup_for(strategy: str | None = None) -> universe.Settings:
    """The screen settings of the watchlist strategy (ConfigError if rules.json is
    invalid or no strategy uses the watchlist)."""
    if strategy:
        from paperbot.core import profile as profiles
        profile = profiles.load(strategy)
        if profile.universe.get("source") != "watchlist":
            raise ConfigError(f"strategy {strategy!r} does not use the morning watchlist")
    else:
        profile = universe.prefilter_profile()
        if profile is None:
            raise ConfigError("no strategy in rules.json uses the morning watchlist")
    return universe.settings(profile)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Morning gap prefilter (index from rules.json).")
    parser.add_argument("--strategy", help="the watchlist strategy whose settings to use")
    parser.add_argument("--min-gap", type=float,
                        help="minimum gap %% from yesterday's close (default: the strategy's gap block)")
    parser.add_argument("--min-price", type=float,
                        help="minimum latest price in USD (default: the strategy's price block)")
    parser.add_argument("--dry-run", action="store_true", help="do not write watchlist.txt")
    args = parser.parse_args(argv)

    started = time.monotonic()
    now = dt.datetime.now(ET)
    summary = {"success": False, "total_screened": 0, "survivors_count": 0, "below_gap": 0,
               "below_price": 0, "failed": 0, "elapsed_seconds": 0.0, "top_20_survivors": [],
               "watchlist_path": None if args.dry_run else str(WATCHLIST_PATH)}

    def finish(code: int, **extra) -> int:
        summary.update(extra, elapsed_seconds=round(time.monotonic() - started, 1))
        print(json.dumps(summary, indent=2))
        if not args.dry_run:
            push_summary(summary, now)
        return code

    def fail(why: str, **extra) -> int:
        """A failed run: the watchlist is NOT written, so the previous one is kept."""
        kept = "" if args.dry_run else "; watchlist not updated (previous list kept)"
        print(f"ALERT prefilter: {why}", file=sys.stderr)
        return finish(1, error=why + kept, **extra)

    try:
        setup = setup_for(args.strategy)
    except (ConfigError, OSError, ValueError, KeyError) as e:
        print(f"ALERT prefilter refused: {e}", file=sys.stderr)
        return finish(1, error=f"rules.json: {e}")
    if args.min_gap is not None:
        setup = dataclasses.replace(setup, min_gap=args.min_gap)
    if args.min_price is not None:
        setup = dataclasses.replace(setup, min_price=args.min_price)
    summary.update(strategy=setup.strategy, index_name=setup.index, top_n=setup.top_n,
                   min_gap=setup.min_gap, min_price=setup.min_price, data_source="ibkr")

    try:
        index = get_live_index_tickers(setup.index)
    except Exception as e:  # noqa: BLE001
        return fail(f"Wikipedia {setup.index} member list unavailable: {e!r}")

    caps = market_caps(index)
    top = sorted(caps, key=caps.get, reverse=True)[:setup.top_n]
    if not top:
        return fail("Yahoo Finance returned no market caps", index_size=len(index))

    try:
        data, errors = ibkr_daily_bars(top)
    except (IbkrUnavailable, ConfigError, OSError, ValueError) as e:
        return fail(str(e), total_screened=len(top))
    latest = latest_date(data)
    if data and latest != now.date():  # no data at all is reported below, per symbol
        return fail(f"no IBKR daily bar for today yet (latest {latest}); run after the "
                    "9:30 ET open on a trading day", total_screened=len(top))

    result = screen(data, top, setup.min_gap, setup.min_price, now.date(), errors)
    failed = result["failed"]
    if failure(len(failed), len(top)):
        sample = "; ".join(failed[:SAMPLE_ERRORS])
        return fail(f"IBKR price data missing for {len(failed)}/{len(top)} symbols "
                    f"(e.g. {sample})", total_screened=len(top), failed=len(failed),
                    failed_symbols=failed)
    for line in alerts(len(failed), len(top)):
        print(line, file=sys.stderr)
        if not args.dry_run:
            _push(f"Prefilter ALERT {now:%H:%M} ET", lambda: line, "high")
    survivors = result["survivors"]
    if not args.dry_run:
        write_watchlist(WATCHLIST_PATH, survivors, result["passed"], len(top),
                        setup.min_gap, setup.min_price, now, setup.index)
    return finish(
        0, success=True, total_screened=len(top), survivors_count=len(survivors),
        below_gap=result["below_gap"], below_price=result["below_price"],
        failed=len(failed),
        top_20_survivors=[f"{s['symbol']} ({s['gap_pct']:+.2f}%)" for s in survivors],
        index_size=len(index), market_cap_missing=len(index) - len(caps),
        failed_symbols=failed)


if __name__ == "__main__":
    sys.exit(main())
