"""Monitoring dashboard builder: telemetry + logs -> dashboard/ (read-only).

    python -m paperbot.reporting.dashboard     rebuild now; safe at any hour, never connects

Pages (static HTML/JS from src/paperbot/reporting/web/, copied to dashboard/
when they change; open dashboard/index.html straight from disk):
  index.html        executive summary: KPIs, live state, next predicted action
  logs.html         every run today, step by step, plus the order lifecycle
  strategy.html     strategy parameters and the market-scanning live view
  performance.html  trade performance (compute_perf.py)

All data goes into one file, dashboard/data/telemetry.js
(`window.PAPERBOT = {...}`), which the pages load with a <script> tag (so it
works from file://) and reload every 15 s. It is built from:
  logs/telemetry.jsonl   one line per pass (telemetry.py; contract in
                         docs/telemetry.schema.json)
  logs/bot.log           scheduled-run starts, skips and connection errors
  logs/orders.jsonl, data/trades.csv   order lifecycle and fills
  logs/heartbeat.json, logs/state.json, logs/strategy_state.json,
  logs/cycle_errors.log, logs/test_results.json, data/watchlist.txt
  Task Scheduler       `schtasks /Query` for the HT_* tasks (read-only)

compute_perf.refresh_dashboard() calls build() after every bot/cycle pass.
Nothing here sends orders, talks to IBKR or changes the scheduler.
"""
import csv
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from paperbot import paths
from paperbot.reporting import telemetry as tm

ET = tm.ET
WEB_DIR = Path(__file__).resolve().parent / "web"
OUT_DIR = paths.DASHBOARD_DIR
DATA_JS = Path("data") / "telemetry.js"
HEARTBEAT = paths.LOGS_DIR / "heartbeat.json"
CYCLE_ERRORS = paths.LOGS_DIR / "cycle_errors.log"
TEST_RESULTS = paths.LOGS_DIR / "test_results.json"
SCHEDULER_CACHE = paths.LOGS_DIR / "scheduler_cache.json"
BUILTIN_SCHEDULER = paths.LOGS_DIR / "scheduler_state.json"  # core/clock.py (paper-bot start)
CONTINUOUS_SOURCES = ("bot.py", "paper-bot")  # heartbeat sources of a long-running bot
LOG_TAIL_BYTES = 4 * 1024 * 1024  # today's part of bot.log is well below this
MAX_RUNS = 500  # telemetry passes embedded in full; older ones are counted only
STALE_MINUTES = 10
NEAR_PCT = 0.5  # "near trigger" when a price check is within this distance
CYCLE_TASK = "HT_Cycle"
TASK_NAMES = ["HT_Cycle", "HT_LogRotate", *[f"HT_Prefilter_{i:02d}" for i in range(1, 8)],
              "HT_Dashboard", "HT_Reschedule"]
OTHER_TASKS_TTL = dt.timedelta(minutes=10)
PREFILTER_TIMES = ("09:55", "10:25", "10:55", "11:25", "11:55", "12:25", "12:55")
RESULT_TEXT = {0: "OK", 1: "exit code 1 (error)", 2: "exit code 2", 267008: "ready",
               267009: "running", 267010: "disabled", 267011: "not run yet",
               267014: "terminated by user", 2147750687: "an instance is already running",
               2147942402: "file not found", 2147942403: "path not found",
               3221225786: "terminated (Ctrl+C / closed)"}
NEUTRAL_RESULTS = {267008, 267009, 267011}


# ---------------------------------------------------------------- small readers

def _json(path: Path, default):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, type(default)) else default
    except (OSError, ValueError):
        return default


def _tail_lines(path: Path, limit: int = LOG_TAIL_BYTES) -> list[str]:
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            f.seek(max(0, size - limit))
            data = f.read()
    except OSError:
        return []
    lines = data.decode("utf-8", errors="replace").splitlines()
    return lines[1:] if size > limit else lines


def _ms(when: dt.datetime) -> int:
    return int(when.timestamp() * 1000)


def _iso(when: dt.datetime | None) -> str | None:
    return when.isoformat(timespec="seconds") if when else None


def telemetry_today(day: str, path: Path | None = None) -> list[dict]:
    out = []
    for line in _tail_lines(path or tm.TELEMETRY_FILE, 64 * 1024 * 1024):
        if day not in line:  # cheap pre-filter; the parsed "ended" decides
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and str(record.get("ended", "")).startswith(day):
            out.append(record)
    return out


def log_today(day: str, path: Path | None = None) -> list[dict]:
    lines = (tm.parse_log_line(l) for l in _tail_lines(path or tm.BOT_LOG))
    return [l for l in lines if l and l["time"].startswith(day)]


def ledger_today(day: str, path: Path | None = None) -> list[dict]:
    try:
        with open(path or paths.TRADES_CSV, newline="", encoding="utf-8") as f:
            return [r for r in csv.DictReader(f) if str(r.get("timestamp_iso", "")).startswith(day)]
    except OSError:
        return []


def parse_watchlist(path: Path | None = None) -> dict:
    """Header date and per-symbol gap/open/prev from data/watchlist.txt."""
    path = path or paths.WATCHLIST_FILE
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return {"generated": None, "symbols": []}
    generated = None
    if lines:
        match = re.search(r" at (\d{4}-\d{2}-\d{2} \d{2}:\d{2})", lines[0])
        generated = match.group(1) if match else None
    symbols = []
    for line in lines:
        ticker, _, comment = line.partition("#")
        ticker = ticker.strip().upper()
        if not ticker:
            continue
        nums = dict(re.findall(r"(gap|open|prev) \$?([+-]?\d+(?:\.\d+)?)", comment))
        symbols.append({"symbol": ticker, "gap_pct": tm._float(nums.get("gap")),
                        "open": tm._float(nums.get("open")), "prev": tm._float(nums.get("prev"))})
    return {"generated": generated, "symbols": symbols}


# ---------------------------------------------------------------- calendar model

def _next_weekday(day: dt.date) -> dt.date:
    day += dt.timedelta(days=1)
    while day.weekday() >= 5:
        day += dt.timedelta(days=1)
    return day


SESSION_DAYS = 7  # weekdays carried in the data, so a page left open for days keeps working


def session_windows(now: dt.datetime, times: dict) -> list[dict]:
    """The current (or next) weekday session and the following ones, as epoch ms.
    US market holidays are not modelled."""
    day = now.date()
    if day.weekday() >= 5 or now.time() >= times["run_until"]:
        day = _next_weekday(day)
    days = [day]
    while len(days) < SESSION_DAYS:
        days.append(_next_weekday(days[-1]))
    out = []
    for d in days:
        at = lambda t: _ms(dt.datetime.combine(d, t, ET))  # noqa: E731
        out.append({"date": d.isoformat(), "weekday": d.strftime("%a"),
                    "logrotate": at(dt.time(9, 25)), "open": at(times["open"]),
                    "entry_start": at(times["entry_start"]), "entry_end": at(times["entry_end"]),
                    "force_close": at(times["force_close"]), "alert": at(times["alert"]),
                    "close": at(times["close"]), "run_until": at(times["run_until"]),
                    "summary": at(dt.time(16, 5)),
                    "prefilter": [at(tm.hhmm(t, t)) for t in PREFILTER_TIMES]})
    return out


# ---------------------------------------------------------------- Task Scheduler

def _local_time(text: str) -> dt.datetime | None:
    try:
        when = dt.datetime.strptime(text.strip(), "%m/%d/%Y %I:%M:%S %p").astimezone()
    except (ValueError, AttributeError):
        return None
    return None if when.year < 2000 else when


def _interpreter(command: str) -> dict:
    """Does the task's python.exe exist and have the packages cycle.py needs?"""
    match = re.match(r'^\s*"([^"]+)"|^\s*(\S+)', command or "")
    exe = Path((match.group(1) or match.group(2)) if match else "")
    site = exe.parent.parent / "Lib" / "site-packages"
    missing = [pkg for pkg in ("tzdata", "ib_async", "pandas") if not (site / pkg).is_dir()]
    exists = bool(match) and exe.is_file()
    return {"path": str(exe), "exists": exists, "missing": missing if exists else [],
            "ok": exists and not missing}


def query_task(name: str, runner=subprocess.run) -> dict | None:
    """One task's schtasks /V /FO CSV row as a dict, or None if it is not registered."""
    try:
        result = runner(["schtasks", "/Query", "/TN", name, "/V", "/FO", "CSV"],
                        capture_output=True, text=True, timeout=10, errors="replace",
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    rows = list(csv.DictReader(result.stdout.splitlines()))
    return rows[0] if rows else None


def task_info(name: str, row: dict | None) -> dict:
    if row is None:
        return {"name": name, "registered": False}
    try:
        code = int(row.get("Last Result", ""))
    except ValueError:
        code = None
    code = code + 2 ** 32 if code is not None and code < 0 else code
    last, nxt = _local_time(row.get("Last Run Time", "")), _local_time(row.get("Next Run Time", ""))
    return {"name": name, "registered": True,
            "enabled": row.get("Scheduled Task State", "").strip().lower() == "enabled",
            "status": row.get("Status", ""), "command": row.get("Task To Run", ""),
            "last_run": _iso(last.astimezone(ET)) if last else None,
            "next_run": _iso(nxt.astimezone(ET)) if nxt else None,
            "last_result": code,
            "last_result_text": RESULT_TEXT.get(code, f"code {code} (0x{code:X})" if code is not None
                                                else "unknown"),
            "result_ok": code == 0 or code in NEUTRAL_RESULTS}


def scheduler_tasks(now: dt.datetime, runner=subprocess.run, cache: Path | None = None) -> dict:
    """HT_Cycle every build; the other tasks at most every OTHER_TASKS_TTL."""
    if sys.platform != "win32" and runner is subprocess.run:
        return {"available": False, "tasks": []}
    cache = cache or SCHEDULER_CACHE
    saved = _json(cache, {})
    tasks = {CYCLE_TASK: task_info(CYCLE_TASK, query_task(CYCLE_TASK, runner))}
    fresh = tm.parse_et(saved.get("time"))
    if fresh and now - fresh < OTHER_TASKS_TTL and isinstance(saved.get("tasks"), dict):
        others = saved["tasks"]
    else:
        others = {n: task_info(n, query_task(n, runner)) for n in TASK_NAMES if n != CYCLE_TASK}
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({"time": _iso(now), "tasks": others}))
        except OSError:
            pass
    tasks.update({n: t for n, t in others.items() if n != CYCLE_TASK})
    cycle = tasks[CYCLE_TASK]
    return {"available": True, "tasks": [tasks[n] for n in TASK_NAMES if n in tasks],
            "cycle": cycle,
            "interpreter": _interpreter(cycle.get("command", "")) if cycle.get("registered") else None}


# ---------------------------------------------------------------- derived views

def run_counts(log: list[dict], runs: list[dict], errors: list[str]) -> dict:
    starts = [l for l in log if l["msg"].startswith("cycle start")]
    skipped = [l for l in log if l["msg"].startswith("cycle skipped")]
    bot_passes = [r for r in runs if r.get("source") in CONTINUOUS_SOURCES]
    by_hour: dict[str, int] = {}
    for item in starts:
        by_hour[item["time"][11:13]] = by_hour.get(item["time"][11:13], 0) + 1
    for r in bot_passes:
        by_hour[r["ended"][11:13]] = by_hour.get(r["ended"][11:13], 0) + 1
    return {"executed": len(starts) + len(bot_passes), "scheduled_runs": len(starts),
            "bot_loops": len(bot_passes), "passes": len(runs), "skipped": len(skipped),
            "failed": len(errors), "by_hour": by_hour}


def missed_runs(now: dt.datetime, log: list[dict], times: dict, interval_min: int = 5) -> list[str]:
    """Scheduled 5-minute slots today with no trace in bot.log (no start, skip or failed
    connect), from the first run of the day until the last slot that should have run.
    A gap usually means the PC slept or the task did not fire."""
    if now.weekday() >= 5:
        return []
    starts = sorted(dt.datetime.fromisoformat(l["time"]) for l in log if l["msg"].startswith(
        ("cycle start", "cycle skipped", "cannot connect to IB Gateway")))
    if not starts:
        return []
    step = dt.timedelta(minutes=interval_min)
    first = starts[0].replace(second=0, microsecond=0)
    first -= dt.timedelta(minutes=first.minute % interval_min)
    close = dt.datetime.combine(now.date(), times["close"], ET)
    last = min(now - dt.timedelta(minutes=2), close)  # leave the current slot time to start
    seen = {(t.replace(second=0, microsecond=0) - dt.timedelta(minutes=t.minute % interval_min))
            for t in starts}
    missing, slot = [], first
    while slot <= last:
        if slot not in seen:
            missing.append(slot.strftime("%H:%M"))
        slot += step
    return missing


def _ranges(slots: list[str], interval_min: int = 5) -> str:
    """'15:40-16:00 (5 runs), 12:10' from consecutive HH:MM slots."""
    out, start, prev = [], None, None
    for s in slots + [None]:
        t = dt.datetime.strptime(s, "%H:%M") if s else None
        if prev and t and (t - prev).total_seconds() == interval_min * 60:
            prev = t
            continue
        if start:
            n = int((prev - start).total_seconds() // (interval_min * 60)) + 1
            out.append(f"{start:%H:%M}" + (f"-{prev:%H:%M} ({n} runs)" if n > 1 else ""))
        start = prev = t
    return ", ".join(out)


def timeline_extras(log: list[dict], errors: list[str]) -> list[dict]:
    """Runs that produced no telemetry: skipped runs and failed starts."""
    out = [{"time": l["time"], "kind": "skipped", "text": l["msg"]}
           for l in log if l["msg"].startswith("cycle skipped")]
    out += [{"time": l["time"], "kind": "failed", "text": l["msg"]}
            for l in log if l["msg"].startswith("cannot connect to IB Gateway")]
    for line in errors:
        match = re.match(r"^\[([^\]]+)\]\s*(.*)$", line)
        when = tm.parse_et(match.group(1)) if match else None
        if when:
            out.append({"time": _iso(when), "kind": "error", "text": match.group(2)[:400]})
    return sorted(out, key=lambda e: e["time"])


def cycle_errors_today(day: str, path: Path | None = None) -> list[str]:
    return [l for l in _tail_lines(path or CYCLE_ERRORS, 1024 * 1024) if l.startswith(f"[{day}")]


def order_lifecycle(events: list[dict], ledger: list[dict], snapshot: list[dict]) -> dict:
    """Per order: placement, modifications, status changes, fills, cancels."""
    orders: dict[int, dict] = {}
    intents = []

    def get(oid) -> dict:
        oid = int(oid)
        return orders.setdefault(oid, {
            "order_id": oid, "symbol": None, "role": None, "action": None, "qty": None,
            "limit": None, "stop": None, "reason": "", "placed_at": None, "events": [],
            "filled": 0.0, "fill_value": 0.0, "status": None, "closed_at": None,
            "ledger_fills": False})

    for row in ledger:
        try:
            get(row["order_id"])["ledger_fills"] = True
        except (KeyError, ValueError, TypeError):
            pass
    for e in sorted(events, key=lambda e: e.get("time") or ""):
        kind, when = e["event"], e.get("time")
        if kind in ("dry_run", "blocked"):
            intents.append({"time": when, "kind": kind, "symbol": e.get("symbol"),
                            "detail": e.get("detail", "")})
            continue
        ids = [i for i in e.get("order_ids") or [] if isinstance(i, int)]
        if kind == "sent" and ids:
            intent = e.get("intent") or {}
            for n, oid in enumerate(ids):
                r = get(oid)
                child = intent.get("action") == "BUY" and n > 0
                r.update(symbol=e.get("symbol"), placed_at=r["placed_at"] or when,
                         role="stop" if child else ("entry" if intent.get("action") == "BUY" else "exit"),
                         action="SELL" if child else intent.get("action"),
                         qty=intent.get("quantity"), reason=intent.get("reason") or "",
                         limit=None if child else intent.get("limit_price"),
                         stop=intent.get("stop_price") if child else None,
                         status=r["status"] or "Sent")
                r["events"].append({"time": when, "kind": "placed",
                                    "text": (f"protective STP {intent.get('quantity')} @ "
                                             f"{intent.get('stop_price')} (bracket child)") if child
                                    else e.get("detail", "")})
            continue
        for oid in ids:
            r = get(oid)
            r["symbol"] = r["symbol"] or e.get("symbol")
            if kind == "stop":
                stop = e.get("stop") or {}
                first = r["placed_at"] is None
                r.update(role="stop", action="SELL", qty=stop.get("quantity"),
                         stop=stop.get("stop_price"), placed_at=r["placed_at"] or when,
                         status=r["status"] or "Sent", reason=r["reason"] or stop.get("reason", ""))
                r["events"].append({"time": when, "kind": "placed" if first else "modified",
                                    "text": e.get("detail", "")})
            elif kind == "status":
                if r["status"] != e.get("status"):
                    r["events"].append({"time": when, "kind": "status", "text": e.get("detail", "")})
                r["status"] = e.get("status")
                if e.get("status") in ("Filled", "Cancelled", "ApiCancelled", "Inactive"):
                    r["closed_at"] = when
            elif kind == "fill" and not r["ledger_fills"]:
                fill = e.get("fill") or {}
                shares, price = tm._float(fill.get("shares")) or 0, tm._float(fill.get("price")) or 0
                r["filled"] += shares
                r["fill_value"] += shares * price
                r["events"].append({"time": when, "kind": "fill", "text": e.get("detail", "")})
            elif kind == "cancel":
                r["events"].append({"time": when, "kind": "cancel", "text": e.get("detail", "")})
    for row in ledger:
        try:
            r = get(row["order_id"])
        except (KeyError, ValueError, TypeError):
            continue
        shares, price = tm._float(row.get("size")) or 0, tm._float(row.get("fill_price")) or 0
        r["symbol"] = r["symbol"] or row.get("symbol")
        r["role"] = r["role"] or tm.ROLES.get(row.get("order_ref"), None)
        r["action"] = r["action"] or row.get("side")
        r["filled"] += shares
        r["fill_value"] += shares * price
        r["events"].append({"time": row.get("timestamp_iso"), "kind": "fill",
                            "text": f"{row.get('side')} {shares:g} @ {price:.2f} (exec)"})
    for o in snapshot:
        r = get(o["order_id"])
        r.update(symbol=r["symbol"] or o["symbol"], role=r["role"] or o["role"],
                 action=r["action"] or o["action"], qty=r["qty"] or o["qty"],
                 limit=r["limit"] or o.get("limit"), stop=o.get("aux") or r["stop"],
                 status=o.get("status") or r["status"], working=True)
    out = []
    for r in orders.values():
        r["events"].sort(key=lambda e: e.get("time") or "")
        qty, filled = tm._float(r["qty"]), r["filled"]
        r["avg_fill"] = round(r["fill_value"] / filled, 4) if filled else None
        r["partial"] = bool(qty and 0 < filled < qty)
        if r.get("working"):
            state = "Partially filled" if r["partial"] else "Working"
        elif qty and filled >= qty - 1e-9:
            state = "Filled"
        elif filled > 0:
            state = "Partially filled, rest cancelled" if r["status"] in (
                "Cancelled", "ApiCancelled", "Inactive") else "Partially filled"
        else:
            state = {"ApiCancelled": "Cancelled", "PreSubmitted": "Working",
                     "Submitted": "Working"}.get(r["status"], r["status"] or "Unknown")
        r["state"] = state
        reason = (r["reason"] or "").lower()
        r["kind"] = ("Entry" if r["role"] == "entry" else
                     "Stop-out" if r["role"] == "stop" and filled else
                     "Protective stop" if r["role"] == "stop" else
                     "EOD liquidation" if "force close" in reason else
                     "Partial profit" if "partial" in reason else
                     "Exit" if r["role"] == "exit" else "Order")
        r["liquidation"] = r["kind"] in ("Stop-out", "EOD liquidation")
        r["first_time"] = r["placed_at"] or (r["events"][0]["time"] if r["events"] else None)
        del r["fill_value"], r["ledger_fills"]
        r.pop("working", None)
        out.append(r)
    out.sort(key=lambda r: r["first_time"] or "", reverse=True)
    return {"orders": out, "intents": sorted(intents, key=lambda i: i["time"] or "", reverse=True)}


def scanner_view(watch: dict, runs: list[dict], universe_source: str, universe: list[str],
                 strategy_state: dict, held: set[str], day: str) -> list[dict]:
    """One row per watched symbol with its latest evaluation and next trigger."""
    latest: dict[str, dict] = {}
    for r in runs:
        for e in r.get("evaluations") or []:
            latest[e["symbol"]] = e
    meta = {s["symbol"]: s for s in watch["symbols"]}
    order = list(universe) + [s for s in latest if s not in universe]
    entered = {s for s, d in (strategy_state.get("entered") or {}).items() if d == day}
    rows = []
    for symbol in order:
        e = latest.get(symbol)
        if symbol in held:
            status = "IN POSITION"
        elif symbol in entered:
            status = "ENTERED TODAY"
        elif e is None:
            status = "NOT EVALUATED"
        elif e["pass"]:
            status = "SIGNAL"
        elif e["static"]:
            status = "BLOCKED TODAY"
        elif e["check"] == "precheck":
            status = "SKIPPED"
        elif e["distance_pct"] is not None and e["check"] != "I3" and e["distance_pct"] <= NEAR_PCT:
            status = "NEAR"
        else:
            status = "WAITING"
        rows.append({"symbol": symbol, "watch": meta.get(symbol), "eval": e, "status": status,
                     "in_list": symbol in universe, "source": universe_source})
    return rows


def funnel(scanner: list[dict]) -> list[dict]:
    """How far each evaluated symbol got, by the step that stopped it."""
    counts = {key: 0 for key, _ in tm.STEPS}
    passed = 0
    for row in scanner:
        e = row["eval"]
        if not e:
            continue
        if e["pass"]:
            passed += 1
        elif e["check"] in counts:
            counts[e["check"]] += 1
    out = [{"key": k, "label": label, "stopped": counts[k]} for k, label in tm.STEPS]
    out.append({"key": "pass", "label": "All filters passed", "stopped": passed})
    return out


def health_checks(now: dt.datetime, sched: dict, heartbeat: dict, log: list[dict],
                  errors: list[str], risk_state: dict, tests: dict, watch_fresh: bool,
                  last_run: dict | None, current_phase: str, stop_file: bool,
                  missed: list[str] | None = None, builtin: dict | None = None) -> list[dict]:
    def check(key, label, status, detail):
        return {"key": key, "label": label, "status": status, "detail": detail}

    out = []
    in_session = current_phase not in ("closed", "pre_open")
    cycle = (sched or {}).get("cycle") or {}
    if builtin is not None:  # paper-bot start: the bot schedules its own daily jobs
        today = now.date().isoformat()
        failed = [name for name, job in (builtin.get("jobs") or {}).items()
                  if job.get("status") == "failed" and str(job.get("started", ""))[:10] == today]
        out.append(check("scheduler", "Built-in scheduler", "warn" if failed else "pass",
                         f"failed today: {', '.join(failed)}" if failed else
                         f"daily jobs run inside paper-bot (updated {str(builtin.get('updated') or 'never')[11:19]} ET)"))
    elif not sched.get("available"):
        out.append(check("scheduler", "Task Scheduler", "skip", "not available on this system"))
    elif not cycle.get("registered"):
        out.append(check("scheduler", "Task Scheduler (HT_Cycle)", "warn",
                         "HT_Cycle not registered (run paperbot-schedule, or run the bot continuously)"))
    elif not cycle.get("enabled"):
        out.append(check("scheduler", "Task Scheduler (HT_Cycle)", "warn", "HT_Cycle is disabled"))
    else:
        out.append(check("scheduler", "Task Scheduler (HT_Cycle)",
                         "pass" if cycle.get("result_ok") else "fail",
                         f"last run {(cycle.get('last_run') or 'never')[11:19] or 'never'} ET: "
                         f"{cycle.get('last_result_text')}"))
    interp = (sched or {}).get("interpreter")
    if interp:
        out.append(check("interpreter", "Scheduler Python environment",
                         "pass" if interp["ok"] else "fail",
                         interp["path"] if interp["ok"] else
                         (f"missing packages: {', '.join(interp['missing'])}" if interp["exists"]
                          else f"not found: {interp['path']}")))
    last_ok = next((l for l in reversed(log) if l["msg"].startswith(("cycle start", "connected to ",
                                                                      "connection to IB Gateway restored"))), None)
    last_bad = next((l for l in reversed(log) if l["msg"].startswith("cannot connect to IB Gateway")), None)
    if last_bad and (not last_ok or last_bad["time"] > last_ok["time"]):
        out.append(check("gateway", "IB Gateway connection", "fail",
                         f"connection refused at {last_bad['time'][11:19]} ET; is Gateway logged in?"))
    elif last_ok:
        out.append(check("gateway", "IB Gateway connection", "pass",
                         f"connected at {last_ok['time'][11:19]} ET"))
    else:
        out.append(check("gateway", "IB Gateway connection", "skip", "no connection attempt today"))
    if heartbeat.get("status") == "crashed":
        out.append(check("heartbeat", "Bot heartbeat", "fail", f"crashed: {heartbeat.get('error', '')}"))
    else:
        last = tm.parse_et(heartbeat.get("time"))
        age = (now - last).total_seconds() / 60 if last else None
        if in_session and current_phase != "post_close" and (age is None or age > STALE_MINUTES):
            out.append(check("heartbeat", "Bot heartbeat", "warn",
                             f"no pass for {age:.0f} min" if age is not None else "no pass yet today"))
        else:
            out.append(check("heartbeat", "Bot heartbeat", "pass" if last else "skip",
                             f"last pass {last:%H:%M:%S} ET" if last else "no heartbeat yet"))
    if missed is not None:
        out.append(check("missed_runs", "Missed scheduled runs", "fail" if missed else "pass",
                         f"no run at {_ranges(missed)} ET; was the PC asleep or Gateway down?"
                         if missed else "every 5-minute slot ran"))
    out.append(check("run_errors", "Run errors today", "warn" if errors else "pass",
                     f"{len(errors)} in cycle_errors.log" if errors else "none"))
    if risk_state.get("halted") and str(risk_state.get("date")) == now.date().isoformat():
        out.append(check("halt", "Daily loss halt", "warn", risk_state.get("halt_reason") or "halted"))
    else:
        out.append(check("halt", "Daily loss halt", "pass", "not halted"))
    alerts = [l for l in log if "ALERT" in l["msg"]]
    out.append(check("alerts", "Alerts today", "warn" if alerts else "pass",
                     f"{len(alerts)}; last: {alerts[-1]['msg'][:90]}" if alerts else "none"))
    if last_run:
        found = {v["key"]: v for v in last_run.get("validations", [])}
        cover = found.get("stop_coverage")
        if cover and cover["status"] == "fail":
            out.append(check("stops", "Protective stops", "fail", cover["detail"]))
        blockers = [f"{found[k]['label']}: {found[k]['detail']}" for k in ("net_liq", "pnl_available")
                    if k in found and found[k]["status"] == "fail"]
        out.append(check("entries", "Entry readiness (last pass)", "fail" if blockers else "pass",
                         " | ".join(blockers) if blockers else "account value and daily P&L available"))
    if current_phase in ("opening", "entry_window") and now.time() >= dt.time(10, 0):
        out.append(check("watchlist", "Today's watchlist", "pass" if watch_fresh else "warn",
                         "generated today" if watch_fresh else "not generated today; using universe.txt"))
    if stop_file:
        out.append(check("stop_file", "STOP file", "warn", "present: scheduled runs skip themselves"))
    if tests:
        s = tests.get("summary", {})
        out.append(check("tests", "Unit tests", "pass" if tests.get("status") == "passed" else "fail",
                         f"{s.get('passed', 0)}/{s.get('total', 0)} passed"))
    return out


def scheduler_status(now: dt.datetime, sched: dict, heartbeat: dict, current_phase: str,
                     stop_file: bool, bot_pid: int | None) -> tuple[str, str]:
    """Overall scheduler state: ACTIVE, IDLE, PAUSED, STALE, DEGRADED, CRASHED, NOT SCHEDULED."""
    cycle = (sched or {}).get("cycle") or {}
    last = tm.parse_et(heartbeat.get("time"))
    continuous = heartbeat.get("source") in CONTINUOUS_SOURCES and bot_pid is not None
    if stop_file:
        return "PAUSED", "STOP file present: scheduled runs skip themselves until it is deleted"
    if heartbeat.get("status") == "crashed":
        return "CRASHED", f"last run crashed: {heartbeat.get('error', '')}"
    if continuous:
        runner = "paper-bot with built-in scheduler" if heartbeat.get("source") == "paper-bot" \
            else "continuous bot.py loop"
        return "ACTIVE", f"{runner} (pid {bot_pid})"
    if sched.get("available") and not cycle.get("registered"):
        return "NOT SCHEDULED", "HT_Cycle is not registered"
    if sched.get("available") and not cycle.get("enabled"):
        return "PAUSED", "HT_Cycle is disabled in Task Scheduler"
    in_session = current_phase not in ("closed", "pre_open")
    if not in_session:
        return "IDLE", "market closed: runs exit in under a second until 09:30 ET"
    if cycle and not cycle.get("result_ok", True):
        return "DEGRADED", f"last HT_Cycle run failed: {cycle.get('last_result_text')}"
    if last is None or (now - last).total_seconds() > STALE_MINUTES * 60:
        return "STALE", "no completed pass in the last 10 minutes"
    return "ACTIVE", "HT_Cycle runs every 5 minutes"


# ---------------------------------------------------------------- snapshot

def snapshot(now: dt.datetime | None = None, runner=subprocess.run) -> dict:
    from paperbot.reporting import compute_perf
    from paperbot.runtime import runstate

    now = now or dt.datetime.now(ET)
    day = now.date().isoformat()
    strategy_key, section, rules = tm.active_strategy(_json(paths.RULES_FILE, {}))
    config = _json(paths.CONFIG_FILE, {})
    times = tm.session_times(rules)
    current_phase = tm.phase(now, times)
    heartbeat = _json(HEARTBEAT, {})
    risk_state = _json(tm.RISK_STATE, {})
    strategy_state = _json(tm.strategy_state_file(section, strategy_key), {})
    tests = _json(TEST_RESULTS, {})
    stop_file = runstate.stop_requested()
    bot_pid = runstate.running_pid()

    runs = telemetry_today(day)
    last_run = runs[-1] if runs else None
    log = log_today(day)
    errors = cycle_errors_today(day)
    sched = scheduler_tasks(now, runner)

    watch = parse_watchlist()
    watch_day = (watch["generated"] or "")[:10]
    if watch_day == day:
        universe_source, universe = "watchlist", [s["symbol"] for s in watch["symbols"]]
    else:
        universe_source = "universe"
        try:
            from paperbot.strategies.rule_strategy import read_symbols
            universe = read_symbols(paths.UNIVERSE_FILE)
        except OSError:
            universe = []

    positions = (last_run or {}).get("positions")
    if positions is None:
        positions = [{"symbol": p.get("symbol"), "qty": p.get("qty"), "avg_cost": p.get("entry"),
                      "market_price": p.get("market_price"), "stop": p.get("stop"),
                      "initial_stop": p.get("initial_stop"), "managed": p.get("managed"),
                      "triggers": {}, "r_now": None} for p in heartbeat.get("positions", [])]
    held = {p["symbol"] for p in positions}

    order_events = [e for r in runs for e in r.get("order_events") or []]
    if not runs:  # no telemetry yet today: fall back to orders.jsonl directly
        order_events = [tm.order_event(r) for r in tm._json_lines(_tail_lines(tm.ORDERS_LOG))
                        if str(r.get("time", "")).startswith(day)]
    lifecycle = order_lifecycle(order_events, ledger_today(day), (last_run or {}).get("orders", []))

    today_perf = compute_perf.summarize(day, compute_perf.round_trips(
        compute_perf.load_day(paths.TRADES_CSV, day)))
    acct = (last_run or {}).get("account") or {}
    scanner = scanner_view(watch, runs, universe_source, universe, strategy_state, held, day)
    scheduled = heartbeat.get("source") not in CONTINUOUS_SOURCES
    builtin = _json(BUILTIN_SCHEDULER, {}) if heartbeat.get("source") == "paper-bot" else None
    missed = missed_runs(now, log, times) if scheduled else None
    checks = health_checks(now, sched, heartbeat, log, errors, risk_state, tests,
                           watch_day == day, last_run, current_phase, stop_file, missed, builtin)
    scored = [c for c in checks if c["status"] != "skip"]
    worst = ("fail" if any(c["status"] == "fail" for c in scored) else
             "warn" if any(c["status"] == "warn" for c in scored) else "pass")
    status, status_detail = scheduler_status(now, sched, heartbeat, current_phase, stop_file, bot_pid)
    limits = {**config.get("limits", {}), **rules.get("risk", {})}

    truncated = max(0, len(runs) - MAX_RUNS)
    return {
        "schema": "paperbot.dashboard/1",
        "telemetry_schema": tm.SCHEMA_VERSION,
        "generated_at": _iso(now), "generated_at_ms": _ms(now),
        "trading_mode": paths.MODE, "strategy_key": strategy_key,
        "day": day,
        "phase": {"key": current_phase, "label": tm.PHASES[current_phase]},
        "model": {"sessions": session_windows(now, times),
                  "times": {k: v.strftime("%H:%M") for k, v in times.items()},
                  "cycle_interval_min": 5, "force_loop_s": 30,
                  "loop_seconds": config.get("loop_seconds", 30),
                  "near_pct": NEAR_PCT, "stale_minutes": STALE_MINUTES,
                  "holidays_modelled": False,
                  "holds_overnight": (rules.get("time_filter") or {}).get("force_close_et", "") is None},
        "scheduler": {**sched, "status": status, "detail": status_detail,
                      "stop_file": stop_file, "bot_pid": bot_pid,
                      "mode": "continuous" if heartbeat.get("source") in CONTINUOUS_SOURCES else "scheduled",
                      "builtin": builtin},
        "heartbeat": heartbeat,
        "kpis": {"runs": run_counts(log, runs, errors),
                 "pnl": {"daily": acct.get("daily_pnl"), "unrealized": acct.get("unrealized_pnl"),
                         "realized": acct.get("realized_pnl"),
                         "ledger_closed": today_perf["gross_pnl_usd"],
                         "closed_trades": today_perf["total_trades"],
                         "win_rate": today_perf["win_rate_pct"],
                         "loss_limit": -abs(float(limits.get("max_daily_loss") or 0)),
                         "as_of": (last_run or {}).get("ended")},
                 "positions": {"count": len(held),
                               "managed": sum(1 for p in positions if p.get("managed")),
                               "max": limits.get("max_concurrent_positions")},
                 "health": {"status": worst,
                            "score": round(100 * sum(c["status"] == "pass" for c in scored)
                                           / len(scored)) if scored else None}},
        "health": checks,
        "account": acct,
        "risk": {**(last_run or {}).get("risk", {}),
                 "orders_today": risk_state.get("orders_today", 0) if risk_state.get("date") == day else 0,
                 "halted": bool(risk_state.get("halted")) and risk_state.get("date") == day},
        "positions": positions,
        "last_run": last_run,
        "runs": runs[-MAX_RUNS:], "runs_truncated": truncated,
        "timeline_extras": timeline_extras(log, errors) + [
            {"time": dt.datetime.combine(now.date(), dt.datetime.strptime(m, "%H:%M").time(), ET).isoformat(),
             "kind": "missed", "text": "No run in this 5-minute slot (PC asleep, task not fired, or Gateway down)"}
            for m in (missed or [])],
        "orders": lifecycle["orders"], "intents": lifecycle["intents"][:200],
        "alerts": [l for l in log if l["level"] in ("WARNING", "ERROR", "CRITICAL")
                   or "ALERT" in l["msg"]][-100:],
        "strategy": {"rules": rules, "limits": limits, "risk": rules.get("risk", {}),
                     "time_in_force": config.get("time_in_force"),
                     "steps": [{"key": k, "label": l} for k, l in tm.STEPS],
                     "funnel": funnel(scanner),
                     "scan_budget_s": 150 if heartbeat.get("source") not in CONTINUOUS_SOURCES else 10},
        "scanner": {"source": universe_source, "generated": watch["generated"],
                    "size": len(universe), "rows": scanner},
        "performance": {k: today_perf[k] for k in ("total_trades", "wins", "losses", "win_rate_pct",
                                                    "gross_pnl_usd", "profit_factor")},
        "tests": {"status": tests.get("status"), "time": tests.get("time"),
                  "summary": tests.get("summary", {})} if tests else None,
    }


# ---------------------------------------------------------------- output

def sync_site(out_dir: Path) -> list[str]:
    """Copy the static pages to out_dir, rewriting only files that changed."""
    written = []
    for src in sorted(WEB_DIR.rglob("*")):
        if not src.is_file():
            continue
        dest = out_dir / src.relative_to(WEB_DIR)
        data = src.read_bytes()
        try:
            if dest.read_bytes() == data:
                continue
        except OSError:
            pass
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, dest)
        written.append(str(dest.relative_to(out_dir)))
    return written


def write_data(out_dir: Path, data: dict) -> Path:
    path = out_dir / DATA_JS
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(data, default=str, separators=(",", ":")).replace("</", "<\\/")
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(f"window.PAPERBOT = {body};\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def build(out_dir: Path | None = None, now: dt.datetime | None = None,
          runner=subprocess.run) -> Path:
    """Rebuild the pages and their data. Returns dashboard/index.html."""
    out_dir = out_dir or OUT_DIR
    sync_site(out_dir)
    write_data(out_dir, snapshot(now, runner))
    return out_dir / "index.html"


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Rebuild the paperbot monitoring dashboard.")
    parser.parse_args(argv)
    from paperbot.reporting import compute_perf
    compute_perf.refresh_dashboard()  # performance.html, then build()
    print(f"Dashboard updated: {OUT_DIR / 'index.html'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
