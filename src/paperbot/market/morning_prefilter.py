"""Morning gap prefilter: today's biggest S&P 500 gappers -> watchlist.txt.

    python -m paperbot.market.morning_prefilter [--min-gap 3.0] [--min-price 3.0] [--dry-run]
    (paperbot-prefilter is the same command; output goes to data/watchlist.txt)

Run on a trading day after the 9:30 ET open and before the bot's entry
window (10:05 ET). Steps:
  1. live S&P 500 constituents from Wikipedia (sp500_tickers.py)
  2. top 100 of them by current market cap (yfinance, 5 worker threads)
  3. one bulk 2-day daily download for those 100 (threads=5)
  4. gap = today's open vs yesterday's close, the same measure as D3 in
     rules.json; price = the latest intraday print
  5. survivors (gap >= --min-gap, price >= --min-price), sorted by gap,
     capped at 20, written to watchlist.txt (skipped with --dry-run)

strategy.py scans watchlist.txt when it was generated today (ET) and falls
back to the static universe.txt otherwise. A JSON summary goes to stdout,
alerts to stderr. Unless --dry-run, the result (survivors, failure or a
degradation alert) is also pushed via notify.py. Exit 0 on success, 1 when no usable data was downloaded.
Read-only web access (Wikipedia, Yahoo Finance); never connects to IBKR.
"""
import argparse
import datetime as dt
import json
import math
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

from paperbot import paths
from paperbot.market.sp500_tickers import get_live_sp500_tickers, to_yahoo
from paperbot.reporting.notify import notify

WATCHLIST_PATH = paths.WATCHLIST_FILE
DEFAULT_MIN_GAP_PCT = 3.0
DEFAULT_MIN_PRICE = 3.0
MAX_SURVIVORS = 20
TOP_N = 100  # largest index members by market cap to screen
YF_THREADS = 5  # keep low: each thread holds sockets/file descriptors
ET = ZoneInfo("America/New_York")


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


def download(yahoo_symbols: list[str]) -> pd.DataFrame:
    return yf.download(
        tickers=" ".join(yahoo_symbols),
        period="2d",
        interval="1d",
        group_by="ticker",
        threads=YF_THREADS,
        progress=False,
        auto_adjust=True,
    )


def _bars(data: pd.DataFrame, yahoo_symbol: str) -> pd.DataFrame:
    """One ticker's bars from a yf.download(group_by="ticker") frame (KeyError if absent)."""
    if isinstance(data.columns, pd.MultiIndex):
        return data[yahoo_symbol]
    return data  # a single-ticker download has flat columns


def screen(data: pd.DataFrame, symbols: list[str], min_gap: float, min_price: float,
           today: dt.date) -> dict:
    """Apply the price and gap filters. Returns survivors (sorted, capped) and buckets."""
    survivors, failed = [], []
    below_gap = below_price = 0
    for symbol in symbols:
        try:
            bars = _bars(data, to_yahoo(symbol)).dropna(subset=["Open", "Close"])
            if len(bars) < 2:
                raise ValueError("fewer than 2 daily bars")
            if bars.index[-1].date() != today:
                raise ValueError(f"no bar for today (latest {bars.index[-1].date()})")
            prev_close = float(bars.iloc[-2]["Close"])
            today_open = float(bars.iloc[-1]["Open"])
            last = float(bars.iloc[-1]["Close"])  # latest intraday print
            if prev_close <= 0 or any(math.isnan(v) for v in (prev_close, today_open, last)):
                raise ValueError("missing price")
        except (KeyError, IndexError, ValueError) as e:
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
                    min_gap: float, min_price: float, now: dt.datetime):
    """Overwrite the watchlist atomically. strategy.py reads the date in line 1."""
    lines = [
        f"# Auto-generated by morning_prefilter.py at {now:%Y-%m-%d %H:%M %Z}",
        f"# Filters: gap >= {min_gap:g}%, price >= ${min_price:g}",
        "# Source: Live Daily Index Assembly & Dynamic yfinance Engine Filter Feed",
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


def alerts(failed: int, total: int) -> list[str]:
    if not total:
        return []
    if failed / total >= 0.95:
        return ["ALERT: Yahoo/Wikipedia connection failure suspected"]
    if failed / total >= 0.30:
        return [f"ALERT: yfinance data degradation (failed {failed}/{total})"]
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Morning S&P 500 gap prefilter.")
    parser.add_argument("--min-gap", type=float, default=DEFAULT_MIN_GAP_PCT,
                        help=f"minimum gap %% from yesterday's close (default {DEFAULT_MIN_GAP_PCT})")
    parser.add_argument("--min-price", type=float, default=DEFAULT_MIN_PRICE,
                        help=f"minimum latest price in USD (default {DEFAULT_MIN_PRICE})")
    parser.add_argument("--dry-run", action="store_true", help="do not write watchlist.txt")
    args = parser.parse_args()

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

    try:
        index = get_live_sp500_tickers()
    except Exception as e:  # noqa: BLE001
        print("ALERT: Yahoo/Wikipedia connection failure suspected", file=sys.stderr)
        return finish(1, error=f"S&P 500 list unavailable: {e!r}")

    caps = market_caps(index)
    top = sorted(caps, key=caps.get, reverse=True)[:TOP_N]
    if not top:
        print("ALERT: Yahoo/Wikipedia connection failure suspected", file=sys.stderr)
        return finish(1, error="no market caps returned by Yahoo Finance",
                      index_size=len(index))

    data = download([to_yahoo(s) for s in top])
    if data is None or data.empty:
        print("ALERT: Yahoo/Wikipedia connection failure suspected", file=sys.stderr)
        return finish(1, error="yf.download returned no data", total_screened=len(top))
    latest = max(d.date() for d in data.index)
    if latest != now.date():
        return finish(1, error=f"no daily bar for today yet (latest {latest}); run after "
                               "the 9:30 ET open on a trading day", total_screened=len(top))

    result = screen(data, top, args.min_gap, args.min_price, now.date())
    survivors = result["survivors"]
    for line in alerts(len(result["failed"]), len(top)):
        print(line, file=sys.stderr)
        if not args.dry_run:
            _push(f"Prefilter ALERT {now:%H:%M} ET", lambda: line, "high")
    if not args.dry_run:
        write_watchlist(WATCHLIST_PATH, survivors, result["passed"], len(top),
                        args.min_gap, args.min_price, now)
    return finish(
        0, success=True, total_screened=len(top), survivors_count=len(survivors),
        below_gap=result["below_gap"], below_price=result["below_price"],
        failed=len(result["failed"]),
        top_20_survivors=[f"{s['symbol']} ({s['gap_pct']:+.2f}%)" for s in survivors],
        index_size=len(index), market_cap_missing=len(index) - len(caps),
        failed_symbols=result["failed"])


if __name__ == "__main__":
    sys.exit(main())
