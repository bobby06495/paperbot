"""Daily performance summary from the trades.csv fill ledger. Read-only, offline.

    paperbot-perf                    today (ET), prints JSON and pushes the summary
    paperbot-perf --date 2026-10-19  another day
    paperbot-perf --no-notify        print only
    paperbot-perf --no-html          skip dashboard/index.html
    (same as python -m paperbot.reporting.compute_perf; reads data/trades.csv)

trades.csv is written by bot.notify_fills (one row per execution of the bot's
own orders). This script never writes to it and never connects to IBKR, so
it is safe at any hour; after the 16:00 ET close the day is complete.

Round trips: BUY executions open lots (executions of one entry order form one
lot) and SELL executions close them first-in, first-out. A lot counts as ONE
round trip once it is fully sold, even across several sells (the 1/3 partial
profit, then the rest), with:
  pnl_usd   sell proceeds - buy cost - all commissions on its executions
  pnl_pct   pnl_usd / buy cost * 100
  hold_min  minutes from the first buy fill to the last sell fill
Sells with no open lot that day (e.g. a position from an earlier day) and
lots still open are reported but not counted.

Each round trip is also measured in R, the multiple of the dollars actually
risked: R = (exit price - entry price) / (entry price - initial stop). The
initial stop comes from the ledger's stop_price on the entry row; without one
a stop 1 % below the entry is assumed and the R is marked as an estimate.

Summary push (notify.py), title "Daily Summary YYYY-MM-DD":
  Trades: N (WW / LL, pct%) / P&L / Best / Worst / PF, or "No closed trades today."

Dashboard: dashboard/index.html (unless --no-html), a static page that works
when opened straight from disk: status from logs/heartbeat.json, today's P&L,
an R-multiple histogram of all trades in the ledger (pure CSS bars), open
positions with unrealized R, the last 20 closed trades, and the last
unit-test run (logs/test_results.json from paperbot-test). Bootstrap 5 CSS
from a CDN, no JavaScript, reloads itself every 30 s. bot.py and cycle.py
regenerate it after every pass (refresh_dashboard), so it stays current.
"""
import argparse
import datetime as dt
import html
import json
import os
import sys
from collections import defaultdict, deque
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from paperbot import paths
from paperbot.reporting.notify import notify

TRADES_CSV = paths.TRADES_CSV
HEARTBEAT = paths.LOGS_DIR / "heartbeat.json"
TEST_RESULTS = paths.LOGS_DIR / "test_results.json"  # written by paperbot-test (run_tests.py)
DASHBOARD = paths.DASHBOARD_FILE
ET = ZoneInfo("America/New_York")
PROXY_STOP_FRACTION = 0.99  # assumed stop 1 % below entry when none was recorded
SESSION = (dt.time(9, 30), dt.time(16, 5))  # when a fresh heartbeat is expected
STALE_MINUTES = 10
R_BUCKETS = [  # (label, upper bound inclusive); the last bucket is open-ended
    ("≤ −2R", -2.0), ("−2R … −1R", -1.0), ("−1R … 0R", 0.0), ("0R … +1R", 1.0),
    ("+1R … +2R", 2.0), ("+2R … +3R", 3.0), ("> +3R", float("inf")),
]
LEDGER_COLUMNS = ["timestamp_iso", "symbol", "side", "size", "fill_price", "order_id",
                  "commission", "stop_price"]


def load_rows(path: Path) -> pd.DataFrame:
    """All ledger rows, in time order. Missing/empty file -> no rows."""
    try:
        df = pd.read_csv(path, dtype=str, keep_default_na=False)
    except (FileNotFoundError, pd.errors.EmptyDataError):
        return pd.DataFrame(columns=LEDGER_COLUMNS)
    if "timestamp_iso" not in df.columns:
        return df.iloc[0:0]
    return df.sort_values("timestamp_iso", kind="stable")  # FIFO needs time order


def load_day(path: Path, day: str) -> pd.DataFrame:
    """The ledger rows of one ET date, in time order. Missing/empty file -> no rows."""
    df = load_rows(path)
    return df[df["timestamp_iso"].str.startswith(day)]


def _num(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def round_trips(rows: pd.DataFrame) -> dict:
    """FIFO-match one day's executions into completed round trips."""
    open_lots: dict[str, deque] = defaultdict(deque)
    trips, unmatched_sells = [], 0
    for row in rows.to_dict("records"):
        symbol, side = row["symbol"].upper(), row["side"].upper()
        qty, price = _num(row["size"]), _num(row["fill_price"])
        commission, when = _num(row.get("commission")), dt.datetime.fromisoformat(row["timestamp_iso"])
        if qty <= 0:
            continue
        lots = open_lots[symbol]
        if side == "BUY":
            stop = _num(row.get("stop_price"), 0.0) or None
            last = lots[-1] if lots else None
            if last and last["order_id"] == row.get("order_id") and not last["sold"]:
                last["qty"] += qty  # another execution of the same entry order
                last["open"] += qty
                last["cost"] += qty * price
                last["commission"] += commission
                last["stop"] = last["stop"] or stop
            else:
                lots.append({"symbol": symbol, "order_id": row.get("order_id"), "qty": qty,
                             "open": qty, "cost": qty * price, "proceeds": 0.0, "sold": 0.0,
                             "commission": commission, "entry": when, "exit": when,
                             "stop": stop})
            continue
        remaining = qty
        while remaining > 1e-9 and lots:
            lot = lots[0]
            take = min(lot["open"], remaining)
            lot["open"] -= take
            lot["sold"] += take
            lot["proceeds"] += take * price
            lot["commission"] += commission * take / qty
            lot["exit"] = when
            remaining -= take
            if lot["open"] <= 1e-9:
                lots.popleft()
                pnl = lot["proceeds"] - lot["cost"] - lot["commission"]
                entry_price = lot["cost"] / lot["qty"]
                exit_price = lot["proceeds"] / lot["qty"]
                r, estimated = r_multiple(entry_price, exit_price, lot["stop"])
                trips.append({
                    "symbol": symbol, "qty": lot["qty"],
                    "entry_price": round(entry_price, 4),
                    "exit_price": round(exit_price, 4),
                    "pnl_usd": round(pnl, 2),
                    "pnl_pct": round(pnl / lot["cost"] * 100, 2) if lot["cost"] else 0.0,
                    "hold_min": round((lot["exit"] - lot["entry"]).total_seconds() / 60, 1),
                    "commission_usd": round(lot["commission"], 2),
                    "initial_stop": lot["stop"],
                    "r_multiple": r,
                    "r_estimated": estimated,
                    "entry_time": lot["entry"].isoformat(),
                    "exit_time": lot["exit"].isoformat(),
                })
        if remaining > 1e-9:
            unmatched_sells += 1
    still_open = sum(len(lots) for lots in open_lots.values())
    return {"trips": trips, "unmatched_sells": unmatched_sells, "open_lots": still_open}


def r_multiple(entry: float, exit_price: float, stop: float | None) -> tuple[float, bool]:
    """(R, estimated). R uses the dollars actually risked per share (entry - stop)."""
    estimated = stop is None or stop <= 0 or stop >= entry
    if estimated:
        stop = entry * PROXY_STOP_FRACTION
    risk = entry - stop
    return (round(float((exit_price - entry) / risk), 2) if risk > 0 else 0.0), bool(estimated)


def r_histogram(r_values: list[float]) -> list[tuple[str, int]]:
    """Trade counts per R bucket: (-inf,-2], (-2,-1], (-1,0], (0,1], (1,2], (2,3], (3,inf)."""
    counts = [0] * len(R_BUCKETS)
    for r in r_values:
        for i, (_, upper) in enumerate(R_BUCKETS):
            if r <= upper:
                counts[i] += 1
                break
    return [(label, int(n)) for (label, _), n in zip(R_BUCKETS, counts)]


def summarize(day: str, matched: dict) -> dict:
    trips = matched["trips"]
    pnls = [t["pnl_usd"] for t in trips]
    winners = [t for t in trips if t["pnl_usd"] > 0]
    losers = [t for t in trips if t["pnl_usd"] < 0]
    win_sum = sum(t["pnl_usd"] for t in winners)
    loss_sum = sum(t["pnl_usd"] for t in losers)
    if not trips:
        profit_factor = "n/a"
    elif loss_sum == 0:
        profit_factor = "inf"
    else:
        profit_factor = round(win_sum / abs(loss_sum), 2)
    best = max(trips, key=lambda t: t["pnl_usd"], default=None)
    worst = min(trips, key=lambda t: t["pnl_usd"], default=None)
    return {
        "date": day,
        "total_trades": len(trips),
        "wins": len(winners),
        "losses": len(losers),
        "win_rate_pct": round(len(winners) / len(trips) * 100, 1) if trips else 0.0,
        "gross_pnl_usd": round(sum(pnls), 2),  # net of commissions, summed over trades
        "largest_winner": (f"{max(winners, key=lambda t: t['pnl_usd'])['symbol']} "
                           f"${max(t['pnl_usd'] for t in winners):+.2f}") if winners else None,
        "largest_loser": (f"{min(losers, key=lambda t: t['pnl_usd'])['symbol']} "
                          f"${min(t['pnl_usd'] for t in losers):+.2f}") if losers else None,
        "avg_winner": round(win_sum / len(winners), 2) if winners else None,
        "avg_loser": round(loss_sum / len(losers), 2) if losers else None,
        "profit_factor": profit_factor,
        "commissions_usd": round(sum(t["commission_usd"] for t in trips), 2),
        "unmatched_sells": matched["unmatched_sells"],
        "open_lots": matched["open_lots"],
        "best": {"symbol": best["symbol"], "pnl_usd": best["pnl_usd"]} if best else None,
        "worst": {"symbol": worst["symbol"], "pnl_usd": worst["pnl_usd"]} if worst else None,
        "trades": trips,
    }


def message(summary: dict) -> tuple[str, str]:
    title = f"Daily Summary {summary['date']}"
    if not summary["total_trades"]:
        return title, "No closed trades today."
    best, worst = summary["best"], summary["worst"]
    body = (f"Trades: {summary['total_trades']} ({summary['wins']}W / {summary['losses']}L, "
            f"{summary['win_rate_pct']}%)\n"
            f"P&L: ${summary['gross_pnl_usd']:+.2f}\n"
            f"Best: {best['symbol']} ${best['pnl_usd']:+.2f}\n"
            f"Worst: {worst['symbol']} ${worst['pnl_usd']:+.2f}\n"
            f"PF: {summary['profit_factor']}")
    return title, body


# ---------------------------------------------------------------- dashboard

def load_heartbeat(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def bot_status(heartbeat: dict | None, now: dt.datetime) -> tuple[str, str, str]:
    """(status, detail, Bootstrap colour) from the last heartbeat."""
    if not heartbeat or "time" not in heartbeat:
        return "NO DATA", "no cycle data yet", "secondary"
    try:
        last = dt.datetime.fromisoformat(heartbeat["time"]).astimezone(ET)
    except (TypeError, ValueError):
        return "NO DATA", "no cycle data yet", "secondary"
    detail = (f"last cycle {last:%Y-%m-%d %H:%M:%S} ET via {heartbeat.get('source', '?')} "
              f"({heartbeat.get('mode', '?')})")
    if heartbeat.get("status") == "crashed":
        return "CRASHED", f"{detail}: {heartbeat.get('error', '')}", "danger"
    if not (now.weekday() < 5 and SESSION[0] <= now.time() < SESSION[1]):
        return "IDLE", f"{detail}; market closed", "secondary"
    age = (now - last).total_seconds() / 60
    if age > STALE_MINUTES:
        return "STALE", f"{detail}; no pass for {age:.0f} min", "warning"
    return "ACTIVE", detail, "success"


def open_position_rows(heartbeat: dict | None) -> list[dict]:
    rows = []
    for p in (heartbeat or {}).get("positions", []):
        entry, price = float(p.get("entry") or 0), p.get("market_price")
        r, estimated = (r_multiple(entry, float(price), p.get("initial_stop"))
                        if entry > 0 and price else (None, False))
        rows.append({"symbol": str(p.get("symbol", "?")), "qty": float(p.get("qty") or 0),
                     "entry": entry, "stop": p.get("stop"), "price": price,
                     "r": r, "r_estimated": estimated, "managed": bool(p.get("managed"))})
    return rows


def _e(value) -> str:
    return html.escape(str(value))


def _money(value) -> str:
    return f"${float(value):,.2f}" if value is not None else "n/a"


def _r(value, estimated: bool = False) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):+.2f}R" + (" <small>(est.)</small>" if estimated else "")


def _r_class(value) -> str:
    if value is None or value == 0:
        return ""
    return "text-success fw-bold" if value > 0 else "text-danger fw-bold"


TEST_STATUS_COLOURS = {"passed": "success", "failed": "danger", "error": "danger",
                       "unexpected success": "danger", "skipped": "secondary",
                       "expected failure": "secondary"}


def load_test_results(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and isinstance(data.get("tests"), list) else None
    except (OSError, ValueError):
        return None


def _warning_items(items: list[dict]) -> str:
    return "".join(
        f'<li><span class="badge text-bg-{"warning" if w.get("kind") == "python" else "light border"}">'
        f'{_e(w.get("category", ""))}</span> {_e(w.get("message", ""))} '
        f'<small class="text-muted">({_e(w.get("location", ""))})</small></li>'
        for w in items)


def _test_row(t: dict) -> str:
    status = t.get("status", "?")
    what = _e(t.get("description", ""))
    if t.get("class_doc"):
        what += f'<br><small class="text-muted">{_e(t["class_doc"])}</small>'
    if t.get("detail") and status != "passed":
        what += f'<pre class="small text-danger mb-0 mt-1">{_e(t["detail"])}</pre>'
    warned = (f'<ul class="small mb-0 ps-3">{_warning_items(t["warnings"])}</ul>'
              if t.get("warnings") else '<span class="text-muted">none</span>')
    return (f'<tr><td><code>{_e(t.get("class", ""))}.{_e(t.get("name", ""))}</code></td>'
            f'<td>{what}</td>'
            f'<td><span class="badge text-bg-{TEST_STATUS_COLOURS.get(status, "secondary")}">'
            f'{_e(status)}</span></td>'
            f'<td>{float(t.get("duration_s") or 0):.3f}s</td><td>{warned}</td></tr>')


def render_tests(results: dict | None) -> str:
    """The unit-test card: last run, one collapsible group per test module."""
    if not results:
        return ('<p class="text-muted mb-0">No test results yet. Run <code>paperbot-test</code> '
                '(paperbot-start runs it too).</p>')
    s = results.get("summary", {})
    ok = results.get("status") == "passed"
    try:
        when = dt.datetime.fromisoformat(results["time"]).astimezone(ET).strftime("%Y-%m-%d %H:%M ET")
    except (KeyError, TypeError, ValueError):
        when = "unknown time"

    modules: dict[str, list[dict]] = {}
    for t in results["tests"]:
        modules.setdefault(t.get("module", "?"), []).append(t)
    groups = []
    for module, tests in sorted(modules.items()):
        bad = sum(1 for t in tests if t.get("status") in ("failed", "error", "unexpected success"))
        warned = sum(1 for t in tests if t.get("warnings"))
        rows = "".join(_test_row(t) for t in tests)
        badges = (f'<span class="badge text-bg-{"danger" if bad else "success"} ms-2">'
                  f'{len(tests) - bad}/{len(tests)} passed</span>'
                  + (f'<span class="badge text-bg-warning ms-1">{warned} with warnings</span>' if warned else ""))
        groups.append(
            f'<details class="mb-2"{" open" if bad else ""}><summary><code>{_e(module)}</code>{badges}'
            f'<small class="text-muted ms-2">{_e(tests[0].get("module_doc", ""))}</small></summary>'
            f'<div class="table-responsive"><table class="table table-sm mt-2 mb-0">'
            f'<thead><tr><th>Test</th><th>What it checks</th><th>Status</th><th>Time</th><th>Warnings</th></tr></thead>'
            f'<tbody>{rows}</tbody></table></div></details>')

    import_warnings = results.get("import_warnings") or []
    import_block = (f'<div class="alert alert-warning small py-2">Warnings while importing the test modules:'
                    f'<ul class="mb-0">{_warning_items(import_warnings)}</ul></div>'
                    if import_warnings else "")
    log_note = ('<p class="small text-muted">Log warnings (e.g. ALERT) are often expected: the test '
                'deliberately drives the code down that path. Python warnings (yellow) usually mean a '
                'library deprecation worth a look.</p>' if s.get("log_warnings") or s.get("python_warnings") else "")
    return f"""<div class="alert alert-{"success" if ok else "danger"} py-2 d-flex flex-wrap justify-content-between">
      <strong>Tests {"PASSED" if ok else "FAILED"}: {int(s.get("passed", 0))} of {int(s.get("total", 0))} passed</strong>
      <span>{int(s.get("failed", 0))} failed · {int(s.get("error", 0))} errors · {int(s.get("skipped", 0))} skipped ·
      {int(s.get("python_warnings", 0))} Python warnings · {int(s.get("log_warnings", 0))} log warnings
      ({int(s.get("tests_with_warnings", 0))} tests) · last run {_e(when)}, {float(results.get("duration_s") or 0):g}s</span>
    </div>
    {import_block}{log_note}{"".join(groups)}"""


def render_dashboard(today: dict, trips: list[dict], heartbeat: dict | None,
                     now: dt.datetime, tests: dict | None = None) -> str:
    """The whole page as a string. No JavaScript; Bootstrap CSS from a CDN."""
    status, detail, colour = bot_status(heartbeat, now)
    pnl = float(today["gross_pnl_usd"])

    buckets = r_histogram([t["r_multiple"] for t in trips])
    peak = max((n for _, n in buckets), default=0) or 1
    bars = "".join(
        f'<div class="d-flex align-items-center mb-1"><div class="text-end me-2" '
        f'style="width:7.5rem">{_e(label)}</div><div class="flex-grow-1 bg-light">'
        f'<div class="{"bg-danger" if i < 3 else "bg-success"} text-white px-1 small" '
        f'style="width:{n / peak * 100:.1f}%;min-width:{"1.5rem" if n else "0"}">'
        f'{n if n else ""}</div></div></div>'
        for i, (label, n) in enumerate(buckets))
    estimated = sum(1 for t in trips if t["r_estimated"])

    positions = open_position_rows(heartbeat)
    position_rows = "".join(
        f"<tr><td>{_e(p['symbol'])}{'' if p['managed'] else ' <small>(manual)</small>'}</td>"
        f"<td>{p['qty']:g}</td><td>{_money(p['entry'])}</td><td>{_money(p['stop'])}</td>"
        f"<td>{_money(p['price'])}</td>"
        f"<td class=\"{_r_class(p['r'])}\">{_r(p['r'], p['r_estimated'])}</td></tr>"
        for p in positions) or '<tr><td colspan="6" class="text-muted">No open positions</td></tr>'

    recent = sorted(trips, key=lambda t: t["exit_time"], reverse=True)[:20]
    trade_rows = "".join(
        f"<tr><td>{_e(t['exit_time'][:16].replace('T', ' '))}</td><td>{_e(t['symbol'])}</td>"
        f"<td>{float(t['qty']):g}</td><td>{_money(t['entry_price'])}</td>"
        f"<td>{_money(t['exit_price'])}</td>"
        f"<td class=\"{_r_class(t['pnl_usd'])}\">{float(t['pnl_usd']):+,.2f}</td>"
        f"<td class=\"{_r_class(t['r_multiple'])}\">{_r(t['r_multiple'], t['r_estimated'])}</td>"
        f"<td>{float(t['hold_min']):g} min</td></tr>"
        for t in recent) or '<tr><td colspan="8" class="text-muted">No closed trades yet</td></tr>'

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="30">
<title>paperbot dashboard</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css">
</head>
<body class="bg-body-tertiary">
<div class="container py-3">
<div class="alert alert-{colour} d-flex flex-wrap justify-content-between align-items-center">
  <strong>Bot Status: {_e(status)}</strong><span>{_e(detail)}</span>
</div>
<div class="row g-3">
  <div class="col-md-5"><div class="card h-100"><div class="card-body">
    <h5 class="card-title">Today ({_e(today['date'])})</h5>
    <p class="display-6 {_r_class(pnl)}">{pnl:+,.2f} USD</p>
    <p class="mb-1">Closed trades: <strong>{int(today['total_trades'])}</strong></p>
    <p class="mb-1">Wins / losses: <strong>{int(today['wins'])} / {int(today['losses'])}</strong></p>
    <p class="mb-1">Win rate: <strong>{float(today['win_rate_pct']):g}%</strong></p>
    <p class="mb-0">Profit factor: <strong>{_e(today['profit_factor'])}</strong></p>
  </div></div></div>
  <div class="col-md-7"><div class="card h-100"><div class="card-body">
    <h5 class="card-title">R-multiple distribution</h5>
    <p class="small text-muted">{len(trips)} closed trades in trades.csv{f'; {estimated} with estimated R (no recorded stop)' if estimated else ''}</p>
    {bars}
  </div></div></div>
  <div class="col-12"><div class="card"><div class="card-body">
    <h5 class="card-title">Open positions</h5>
    <div class="table-responsive"><table class="table table-sm mb-0">
      <thead><tr><th>Symbol</th><th>Qty</th><th>Entry</th><th>Stop</th><th>Last price</th><th>Unrealized R</th></tr></thead>
      <tbody>{position_rows}</tbody></table></div>
  </div></div></div>
  <div class="col-12"><div class="card"><div class="card-body">
    <h5 class="card-title">Recent closed trades</h5>
    <div class="table-responsive"><table class="table table-sm mb-0">
      <thead><tr><th>Closed (ET)</th><th>Symbol</th><th>Qty</th><th>Entry</th><th>Exit</th><th>P&amp;L</th><th>R</th><th>Hold</th></tr></thead>
      <tbody>{trade_rows}</tbody></table></div>
  </div></div></div>
  <div class="col-12"><div class="card"><div class="card-body">
    <h5 class="card-title">Unit tests</h5>
    {render_tests(tests)}
  </div></div></div>
</div>
<p class="text-muted small mt-3">Generated {now:%Y-%m-%d %H:%M:%S} ET by compute_perf.py; reloads every 30 s.</p>
</div>
</body>
</html>
"""


def refresh_dashboard(out: Path | None = None, trades: Path | None = None,
                      heartbeat_path: Path | None = None,
                      now: dt.datetime | None = None,
                      test_results_path: Path | None = None) -> Path:
    """Rebuild dashboard/index.html from trades.csv, the heartbeat and the last
    paperbot-test results (atomic write)."""
    out, trades = out or DASHBOARD, trades or TRADES_CSV
    now = now or dt.datetime.now(ET)
    rows = load_rows(trades)
    day = now.date().isoformat()
    today = summarize(day, round_trips(rows[rows["timestamp_iso"].str.startswith(day)]))
    page = render_dashboard(today, round_trips(rows)["trips"],
                            load_heartbeat(heartbeat_path or HEARTBEAT), now,
                            load_test_results(test_results_path or TEST_RESULTS))
    os.makedirs(out.parent, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(page, encoding="utf-8")
    os.replace(tmp, out)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Daily performance summary from trades.csv.")
    parser.add_argument("--date", help="ET date YYYY-MM-DD (default: today in ET)")
    parser.add_argument("--no-notify", action="store_true", help="print only, no push")
    parser.add_argument("--no-html", action="store_true", help="do not write dashboard/index.html")
    args = parser.parse_args(argv)
    day = args.date or dt.datetime.now(ET).date().isoformat()
    dt.date.fromisoformat(day)  # validate

    summary = summarize(day, round_trips(load_day(TRADES_CSV, day)))
    if not args.no_html:
        summary["dashboard_path"] = str(DASHBOARD)
    print(json.dumps(summary, indent=2))
    if not args.no_notify:
        try:
            notify(*message(summary), "default")
        except Exception:  # noqa: BLE001 - the summary is already printed
            pass
    if not args.no_html:
        try:
            refresh_dashboard()
        except Exception as e:  # noqa: BLE001 - JSON and Telegram are already done
            print(f"dashboard not written: {e!r}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
