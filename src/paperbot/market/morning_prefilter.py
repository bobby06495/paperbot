"""Morning gap prefilter: today's biggest gappers of the configured index -> watchlist.txt.

    python -m paperbot.market.morning_prefilter [--strategy KEY] [--min-gap PCT]
                                                [--min-price USD] [--dry-run]
    (paperbot-prefilter is the same command; output goes to data/watchlist.txt)

What it screens comes from the watchlist strategy in rules.json (market/universe.py):
the running/saved strategy if its universe is the watchlist, else the default or the
first such strategy (--strategy picks one). Its universe names the index ("S&P 500",
"Nasdaq-100", "S&P MidCap 400", "S&P SmallCap 600") or several ("indexes") and top_n;
the gap and price thresholds are its own entry blocks (min_gap_pct, min_price), so the
screen and the entry checks always agree. --min-gap / --min-price override them for
one run.

Run on a trading day after the 9:30 ET open. On a weekend, a market holiday or a
day the market calendar does not cover (market/market_calendar.py) it does nothing
and exits 0 without a "FAILED" push. Steps:
  1. live constituents of every index (market/constituents.py: Wikipedia, cached
     in data/constituents/ and used up to 7 days old when Wikipedia is down; an
     index that cannot be had is left out with an alert, the others still count),
     combined and de-duplicated
  2. one index: top_n of them by current market cap (yfinance, 5 worker threads).
     several indexes (~1,500 symbols): a broad first screen on Yahoo daily bars
     (batched downloads): price >= min price, then the top_n by gap. Only these
     go to IBKR, so the IBKR load is the same as for one index.
  3. regular-hours daily bars for those from IB Gateway: a READ-ONLY connection
     with its own clientId (config.json client_id + IB_CLIENT_ID_OFFSET), so it
     runs next to the bot and can never place an order
  4. gap = today's open vs the previous session's close (the bot's gap block uses
     the same IBKR data); price = today's latest print
  5. survivors (gap >= min gap, price >= min price), sorted by gap, capped at 20,
     written to watchlist.txt (skipped with --dry-run); the header records the
     index set and the run ID, so the bot ignores a list built for another one

Every run has a run ID (PF-YYYYMMDD-HHMM-xxxx). Each ticker it looked at gets one
line in data/prefilter_runs/<date>/<run ID>.jsonl: the indexes it came from, the
stage it stopped at and why (invalid ticker, no Yahoo data, below the price, not
in the top_n, contract not found at IBKR, no bar today, gap below the minimum,
passed but beyond the 20-symbol cap, or survivor). Also written for --dry-run
and for failed runs; folders older than RUN_LOG_DAYS are removed.

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
import shutil
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from zoneinfo import ZoneInfo

import yfinance as yf

from paperbot import paths
from paperbot.core.risk import ConfigError
from paperbot.market import constituents, market_calendar, universe
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
YAHOO_CHUNK = 200  # symbols per batched Yahoo download (broad multi-index screen)
RUN_LOG_DAYS = 30  # days of per-run ticker logs kept in data/prefilter_runs/


def constituents_dir() -> Path:
    """Index member cache, next to the watchlist (data/constituents/)."""
    return WATCHLIST_PATH.parent / "constituents"


def run_log_dir() -> Path:
    """Per-run ticker logs, next to the watchlist (data/prefilter_runs/)."""
    return WATCHLIST_PATH.parent / "prefilter_runs"


def new_run_id(now: dt.datetime) -> str:
    return f"PF-{now:%Y%m%d-%H%M}-{uuid.uuid4().hex[:4]}"


class RunLog:
    """One JSON line per ticker a run looked at: where it stopped and why."""

    def __init__(self, run_id: str, now: dt.datetime, strategy: str):
        self.run_id, self.now, self.strategy = run_id, now, strategy
        self.rows: dict[str, dict] = {}
        self.members: dict[str, tuple[str, ...]] = {}

    def add(self, symbol: str, stage: str, status: str, reason: str, **extra):
        row = {"run_id": self.run_id, "time": self.now.isoformat(timespec="seconds"),
               "strategy": self.strategy, "symbol": symbol,
               "indexes": list(self.members.get(symbol, ())), "stage": stage,
               "status": status, "reason": reason}
        row.update({k: (round(v, 4) if isinstance(v, float) else v)
                    for k, v in extra.items() if v is not None})
        self.rows[symbol] = row

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for row in self.rows.values():
            out[row["status"]] = out.get(row["status"], 0) + 1
        return out

    def write(self, base: Path) -> Path | None:
        """<base>/<date>/<run_id>.jsonl; prunes days older than RUN_LOG_DAYS. None on error."""
        folder = base / self.now.date().isoformat()
        try:
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{self.run_id}.jsonl"
            path.write_text("".join(json.dumps(r) + "\n" for r in self.rows.values()))
            cutoff = self.now.date() - dt.timedelta(days=RUN_LOG_DAYS)
            for old in base.iterdir():
                try:
                    if old.is_dir() and dt.date.fromisoformat(old.name) < cutoff:
                        shutil.rmtree(old, ignore_errors=True)
                except ValueError:
                    continue
            return path
        except OSError as e:
            print(f"ALERT prefilter: run log not written: {e!r}", file=sys.stderr)
            return None


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


def yahoo_daily(symbols: list[str], today: dt.date
                ) -> tuple[dict[str, tuple[float, float, float]], dict[str, str]]:
    """{symbol: (previous close, today's open, latest price)} from batched Yahoo daily
    bars, and a reason per symbol without usable data. Never raises."""
    import pandas as pd

    rows, errors = {}, {}
    for start in range(0, len(symbols), YAHOO_CHUNK):
        chunk = symbols[start:start + YAHOO_CHUNK]
        try:
            frame = yf.download([to_yahoo(s) for s in chunk], period="5d", interval="1d",
                                group_by="ticker", auto_adjust=False, progress=False,
                                threads=True)
        except Exception as e:  # noqa: BLE001 - one bad batch must not stop the screen
            errors.update({s: f"Yahoo download error {e!r}"[:200] for s in chunk})
            continue
        for symbol in chunk:
            try:
                bars = frame[to_yahoo(symbol)] if isinstance(frame.columns, pd.MultiIndex) else frame
                bars = bars.dropna(subset=["Open", "Close"])
                bars = bars[(bars["Open"] > 0) & (bars["Close"] > 0)]
                if bars.empty:
                    raise ValueError("no Yahoo daily bars")
                last_day = pd.Timestamp(bars.index[-1]).date()
                if last_day != today:
                    raise ValueError(f"no Yahoo bar for today (latest {last_day})")
                if len(bars) < 2:
                    raise ValueError("no previous Yahoo session bar")
                rows[symbol] = (float(bars["Close"].iloc[-2]), float(bars["Open"].iloc[-1]),
                                float(bars["Close"].iloc[-1]))
            except ValueError as e:
                errors[symbol] = str(e)
            except (KeyError, TypeError, IndexError, AttributeError):
                errors[symbol] = "no Yahoo daily bars"
    return rows, errors


def broad_screen(rows: dict[str, tuple[float, float, float]], symbols: list[str],
                 min_price: float, top_n: int
                 ) -> tuple[list[str], dict[str, str], dict[str, float]]:
    """First, coarse screen of a multi-index universe on Yahoo data: price >= min_price,
    then the top_n by gap go to IBKR. Returns (top, {symbol: exclusion reason},
    {symbol: Yahoo gap %})."""
    gaps, excluded = {}, {}
    for symbol in symbols:
        if symbol not in rows:
            continue
        prev_close, today_open, last = rows[symbol]
        gaps[symbol] = (today_open - prev_close) / prev_close * 100
        if last < min_price:
            excluded[symbol] = f"Yahoo price ${last:.2f} below ${min_price:g}"
    ranked = sorted((s for s in gaps if s not in excluded), key=lambda s: gaps[s], reverse=True)
    top = ranked[:top_n]
    for symbol in ranked[top_n:]:
        excluded[symbol] = (f"Yahoo gap {gaps[symbol]:+.2f}% not in the top {top_n} by gap "
                            "(not checked at IBKR)")
    return top, excluded, gaps


def latest_date(data: dict[str, list[DailyBar]]) -> dt.date | None:
    return max((b.date for bars in data.values() for b in bars), default=None)


def screen(data: dict[str, list[DailyBar]], symbols: list[str], min_gap: float,
           min_price: float, today: dt.date, errors: dict[str, str] | None = None) -> dict:
    """Apply the price and gap filters. Returns survivors (sorted, capped) and buckets.
    `errors` explains symbols missing from `data` (shown in "failed")."""
    errors = errors or {}
    survivors, failed = [], []
    below_gap = below_price = 0
    details: dict[str, tuple[str, str, float | None, float | None]] = {}
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
            details[symbol] = ("failed", str(e), None, None)
            continue
        gap_pct = (today_open - prev_close) / prev_close * 100
        if last < min_price:
            below_price += 1
            details[symbol] = ("below_price", f"price ${last:.2f} below ${min_price:g}",
                               gap_pct, last)
        elif gap_pct < min_gap:
            below_gap += 1
            details[symbol] = ("below_gap", f"gap {gap_pct:+.2f}% below {min_gap:g}%",
                               gap_pct, last)
        else:
            details[symbol] = ("survivor", f"gap {gap_pct:+.2f}% >= {min_gap:g}%", gap_pct, last)
            survivors.append({"symbol": symbol, "gap_pct": gap_pct, "open": today_open,
                              "prev_close": prev_close, "last": last,
                              "change_pct": (last - prev_close) / prev_close * 100})
    survivors.sort(key=lambda s: s["gap_pct"], reverse=True)
    for s in survivors[MAX_SURVIVORS:]:
        details[s["symbol"]] = ("beyond_cap", f"passed (gap {s['gap_pct']:+.2f}%) but beyond the "
                                f"{MAX_SURVIVORS}-symbol watchlist cap", s["gap_pct"], s["last"])
    return {"survivors": survivors[:MAX_SURVIVORS], "passed": len(survivors),
            "below_gap": below_gap, "below_price": below_price, "failed": failed,
            "details": details}


def write_watchlist(path: Path, survivors: list[dict], passed: int, screened: int,
                    min_gap: float, min_price: float, now: dt.datetime,
                    index: str = universe.DEFAULT_INDEX, run_id: str | None = None,
                    source: str = "Wikipedia index members, Yahoo market caps, IBKR daily bars"):
    """Overwrite the watchlist atomically. The bot reads the date in line 1 and the
    index set in the "# Index:" line."""
    lines = [
        f"# Auto-generated by morning_prefilter.py at {now:%Y-%m-%d %H:%M %Z}",
        f"# Index: {index}",
        *([f"# Run: {run_id}"] if run_id else []),
        f"# Filters: gap >= {min_gap:g}%, price >= ${min_price:g}",
        f"# Source: {source}",
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
    run: RunLog | None = None

    def finish(code: int, **extra) -> int:
        summary.update(extra, elapsed_seconds=round(time.monotonic() - started, 1))
        if run is not None:
            path = run.write(run_log_dir())
            summary.update(run_id=run.run_id, run_log=str(path) if path else None,
                           ticker_status=run.counts())
        print(json.dumps(summary, indent=2))
        if not args.dry_run:
            push_summary(summary, now)
        return code

    def fail(why: str, **extra) -> int:
        """A failed run: the watchlist is NOT written, so the previous one is kept."""
        kept = "" if args.dry_run else "; watchlist not updated (previous list kept)"
        print(f"ALERT prefilter: {why}", file=sys.stderr)
        return finish(1, error=why + kept, **extra)

    def alert(text: str):
        print(f"ALERT prefilter: {text}", file=sys.stderr)
        if not args.dry_run:
            _push(f"Prefilter ALERT {now:%H:%M} ET", lambda: text, "high")

    try:
        setup = setup_for(args.strategy)
    except (ConfigError, OSError, ValueError, KeyError) as e:
        print(f"ALERT prefilter refused: {e}", file=sys.stderr)
        return finish(1, error=f"rules.json: {e}")
    if args.min_gap is not None:
        setup = dataclasses.replace(setup, min_gap=args.min_gap)
    if args.min_price is not None:
        setup = dataclasses.replace(setup, min_price=args.min_price)
    indexes = tuple(setup.indexes) or (setup.index,)
    multi = len(indexes) > 1
    summary.update(strategy=setup.strategy, index_name=setup.index, indexes=list(indexes),
                   top_n=setup.top_n, min_gap=setup.min_gap, min_price=setup.min_price,
                   data_source="ibkr")

    day = market_calendar.session_at(now)
    if not day.trading:  # weekend, holiday or an uncovered day: nothing to screen
        summary.update(success=True, skipped=f"market closed: {day.describe()}",
                       elapsed_seconds=round(time.monotonic() - started, 1))
        if day.kind == "unknown":
            print(f"ALERT prefilter: market calendar: {day.describe()}", file=sys.stderr)
        print(json.dumps(summary, indent=2))
        return 0

    run = RunLog(new_run_id(now), now, setup.strategy)
    print(f"prefilter run {run.run_id}: {setup.describe()}", file=sys.stderr)

    found = constituents.resolve(indexes, lambda name: get_live_index_tickers(name),
                                 constituents_dir(), now)
    run.members = found.members
    for symbol, why in found.invalid.items():
        run.add(symbol, "members", "excluded", why)
    summary.update(index_sources=found.sources, indexes_failed=found.failed)
    if not found.members:
        details = "; ".join(f"{i}: {why}" for i, why in found.failed.items())
        return fail(f"Wikipedia {setup.index} member list unavailable: {details}")
    for text in found.alerts:
        alert(text)
    members = found.symbols

    if multi:  # broad Yahoo screen, then the top_n by gap go to IBKR
        rows, yahoo_errors = yahoo_daily(members, now.date())
        top, excluded, yahoo_gaps = broad_screen(rows, members, setup.min_price, setup.top_n)
        for symbol, why in yahoo_errors.items():
            run.add(symbol, "yahoo", "excluded", why)
        for symbol, why in excluded.items():
            run.add(symbol, "yahoo", "excluded", why, yahoo_gap_pct=yahoo_gaps.get(symbol),
                    yahoo_price=rows.get(symbol, (None, None, None))[2])
        if not top:
            return fail(f"Yahoo Finance returned no usable daily bars for today "
                        f"({len(yahoo_errors)}/{len(members)} without data)",
                        index_size=len(members))
        source = "Wikipedia index members, Yahoo daily bars (broad screen), IBKR daily bars"
    else:  # one index: the largest top_n by market cap, as before
        caps = market_caps(members)
        top = sorted(caps, key=caps.get, reverse=True)[:setup.top_n]
        for symbol in members:
            if symbol not in caps:
                run.add(symbol, "market_cap", "excluded", "no market cap from Yahoo")
            elif symbol not in top:
                run.add(symbol, "market_cap", "excluded",
                        f"not in the top {setup.top_n} by market cap", market_cap=caps[symbol])
        if not top:
            return fail("Yahoo Finance returned no market caps", index_size=len(members))
        yahoo_gaps = {}
        source = "Wikipedia index members, Yahoo market caps, IBKR daily bars"

    try:
        data, errors = ibkr_daily_bars(top)
    except (IbkrUnavailable, ConfigError, OSError, ValueError) as e:
        for symbol in top:
            run.add(symbol, "ibkr", "excluded", f"IBKR unavailable: {e}")
        return fail(str(e), total_screened=len(top))
    latest = latest_date(data)
    if data and latest != now.date():  # no data at all is reported below, per symbol
        for symbol in top:
            run.add(symbol, "ibkr", "excluded", f"no IBKR daily bar for today (latest {latest})")
        return fail(f"no IBKR daily bar for today yet (latest {latest}); run after the "
                    "9:30 ET open on a trading day", total_screened=len(top))

    result = screen(data, top, setup.min_gap, setup.min_price, now.date(), errors)
    for symbol, (status, why, gap, last) in result.get("details", {}).items():
        run.add(symbol, "ibkr", "excluded" if status not in ("survivor",) else "survivor",
                why if status != "failed" else f"IBKR: {why}", gap_pct=gap, price=last,
                yahoo_gap_pct=yahoo_gaps.get(symbol))
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
                        setup.min_gap, setup.min_price, now, setup.index, run.run_id, source)
    return finish(
        0, success=True, total_screened=len(top), survivors_count=len(survivors),
        below_gap=result["below_gap"], below_price=result["below_price"],
        failed=len(failed),
        top_20_survivors=[f"{s['symbol']} ({s['gap_pct']:+.2f}%)" for s in survivors],
        index_size=len(members),
        market_cap_missing=0 if multi else len(members) - len(caps),
        failed_symbols=failed)


if __name__ == "__main__":
    sys.exit(main())
