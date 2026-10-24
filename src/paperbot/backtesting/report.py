"""Backtest reports and the dashboard's backtesting data.

  publish_run(run_dir)   report.html inside the run folder, plus copies in
                         backtesting/reports/<strategy_id>/, then publish_all()
  publish_all()          dashboard/data/backtest.js (all BACKTESTING pages), the
                         backtesting pages copied into dashboard/, and the portfolio
                         report backtesting/reports/portfolio/latest.html
  periodic(kind)         weekly / monthly executive report in reports/portfolio/

Every HTML report is self-contained: it embeds the run's data and the same
renderer the dashboard uses (web/assets/bt.js), so it opens from disk and shows
exactly the numbers in the run's CSV/JSON files. Nothing here touches IBKR.
"""
import datetime as dt
import json
import shutil
from pathlib import Path

from paperbot import paths
from paperbot.backtesting import analysis, experiments, runs
from paperbot.backtesting.strategies import discover

WEB = Path(__file__).resolve().parent.parent / "reporting" / "web"
DATA_FILE = Path("data") / "backtest.js"
MAX_EQUITY_POINTS = 4000
DETAIL_RUNS = 20  # full detail embedded for this many recent runs (others link to report.html)


def _downsample(rows: list[dict], limit: int = MAX_EQUITY_POINTS) -> list[dict]:
    if len(rows) <= limit:
        return rows
    step = len(rows) / limit
    keep = {int(i * step) for i in range(limit)} | {len(rows) - 1}
    # keep each day's lowest mark too, so drawdowns stay visible
    lows: dict[str, int] = {}
    for i, r in enumerate(rows):
        day = r["time_et"][:10]
        if day not in lows or r["equity"] < rows[lows[day]]["equity"]:
            lows[day] = i
    keep |= set(lows.values())
    return [rows[i] for i in sorted(keep)]


def _num_rows(rows: list[dict]) -> list[dict]:
    """CSV strings back to numbers where they are numbers."""
    out = []
    for r in rows:
        clean = {}
        for k, v in r.items():
            if isinstance(v, str) and v != "" and k not in ("symbol", "run_id", "order_ids"):
                try:
                    clean[k] = float(v) if any(c in v for c in ".eE") else int(v)
                    continue
                except ValueError:
                    pass
            clean[k] = v
        out.append(clean)
    return out


def run_payload(run_dir: Path, full: bool = True) -> dict:
    run = runs.load_run(run_dir)
    meta, m = run["metadata"], run["metrics"]
    payload = {"metadata": meta, "metrics": m,
               "report_path": f"../backtesting/results/{meta.get('strategy_id')}/{meta.get('run_id')}/report.html"}
    if not full:
        return payload
    payload.update({
        "validation": {k: v for k, v in run["validation"].items() if k != "watchlists"},
        "config": run["config"],
        "trades": _num_rows(run["trades"]), "orders": _num_rows(run["orders"]),
        "fills": _num_rows(run["fills"]), "daily": _num_rows(run["daily_performance"]),
        "monthly": _num_rows(run["monthly_performance"]),
        "equity": _downsample(_num_rows(run["equity_curve"])),
        "trade_context": run["trade_context"]})
    return payload


def _summary(meta: dict, run_dir: Path) -> dict:
    m = runs.read_json(run_dir / "metrics.json", {}) or {}
    daily = runs.read_csv(run_dir / "daily_performance.csv")
    cfg = runs.read_json(run_dir / "config_snapshot.json", {}) or {}
    return {k: meta.get(k) for k in (
        "run_id", "strategy_id", "strategy_name", "strategy_version", "kind", "experiment_id",
        "status", "started_at", "finished_at", "provider", "synthetic_data", "period",
        "dataset_id", "headline", "data_status", "out_of_sample", "code", "errors",
        "variants_on_dataset", "currency", "starting_capital")} | {
        "warnings_count": len(meta.get("warnings") or []),
        "report_path": f"../backtesting/results/{meta.get('strategy_id')}/{meta.get('run_id')}/report.html",
        "segments": (m.get("periods") or {}).get("segments", {}),
        "equity_daily": [[r["date"], float(r["end_equity"])] for r in daily],
        "rules": cfg.get("rules", {}), "execution": (cfg.get("backtest") or {}).get("execution", {})}


def _losing_days(trades: list[dict]) -> dict[str, list]:
    by_day: dict[str, list] = {}
    for t in trades:
        if t.get("status") == "closed":
            by_day.setdefault(str(t.get("exit_time_et"))[:10], []).append(t.get("net_pnl") or 0)
    return by_day


def portfolio(details: dict, summaries: list[dict]) -> dict:
    """The portfolio view over the latest completed baseline of every strategy.

    One strategy: its run is a full simulation with shared cash across all its symbols.
    Several strategies: each was simulated on its own capital, so they are shown side by
    side and any combined figure is labelled INDICATIVE (shared cash, cross-strategy
    concurrency and the account-wide loss limits are not simulated together)."""
    latest = {}
    for s in summaries:
        if s["kind"] == "baseline" and str(s["status"]).startswith("completed")                 and s["strategy_id"] not in latest:
            latest[s["strategy_id"]] = s["run_id"]
    included = [details[r] for r in latest.values() if r in details]
    if not included:
        return {"status": "none", "note": "No completed baseline run yet."}
    rows, symbol_pnl, daily_by_strategy = [], {}, {}
    for p in included:
        m, meta = p["metrics"], p["metadata"]
        rows.append({"strategy_id": meta["strategy_id"], "name": meta.get("strategy_name"),
                     "run_id": meta["run_id"], "period": f"{meta['period']['actual_start']} → "
                     f"{meta['period']['actual_end']}", "capital": m.get("starting_capital"),
                     "net_pnl": m.get("net_pnl"), "return_pct": m.get("total_return_pct"),
                     "trades": m.get("completed_trades"), "max_drawdown_pct": m.get("max_drawdown_pct"),
                     "avg_exposure_pct": m.get("avg_exposure_pct"),
                     "costs": round((m.get("total_commission") or 0) + (m.get("total_slippage") or 0), 2),
                     "synthetic": meta.get("synthetic_data")})
        for t in p["trades"]:
            if t.get("status") == "closed":
                row = symbol_pnl.setdefault(t["symbol"], [0.0, 0, 0])
                row[0] += t.get("net_pnl") or 0
                row[1] += 1
                row[2] += 1 if (t.get("net_pnl") or 0) > 0.005 else 0
        daily_by_strategy[meta["strategy_id"]] = {r["date"]: r["pnl"] for r in p.get("daily", [])}
    total = sum(r["net_pnl"] or 0 for r in rows)
    gross = sum(abs(r["net_pnl"] or 0) for r in rows)
    for r in rows:  # share of the absolute P&L: meaningful whatever the signs
        r["share_pct"] = round(100 * abs(r["net_pnl"] or 0) / gross, 1) if gross else None
    concentration = sorted(({"key": k, "net_pnl": round(v[0], 2), "trades": v[1],
                             "win_rate_pct": round(100 * v[2] / v[1], 1) if v[1] else None}
                            for k, v in symbol_pnl.items()), key=lambda r: -abs(r["net_pnl"]))
    if len(included) == 1:
        p = included[0]
        sim = [{"date": d, "losing_trades": sum(1 for x in v if x < 0), "net_pnl": round(sum(v), 2)}
               for d, v in sorted(_losing_days(p["trades"]).items()) if sum(1 for x in v if x < 0) >= 2]
        return {"status": "simulated", "basis": "fully simulated (one strategy)",
                "run_id": p["metadata"]["run_id"], "strategies": rows, "contribution": rows,
                "concentration": concentration, "simultaneous_losses": sim,
                "assumptions": [
                    "Only one strategy has a completed baseline, so the portfolio is that strategy's own "
                    "simulation: one cash balance shared by all its symbols, its live risk limits "
                    "(concurrent positions, buying power, daily-loss halt, drawdown breaker) applied "
                    "together, one equity curve.",
                    "Reporting currency: USD."]}
    days = sorted(set().union(*[set(d) for d in daily_by_strategy.values()]))
    combined, running = [], 0.0
    for d in days:
        running += sum(v.get(d, 0.0) for v in daily_by_strategy.values())
        combined.append([d, round(running, 2)])
    sim = []
    for d in days:
        losers = [k for k, v in daily_by_strategy.items() if v.get(d, 0) < 0]
        if len(losers) >= 2:
            sim.append({"date": d, "strategies_losing": ", ".join(losers),
                        "net_pnl": round(sum(v.get(d, 0.0) for v in daily_by_strategy.values()), 2)})
    return {"status": "indicative", "basis": "indicative only (strategies simulated separately)",
            "run_ids": [r["run_id"] for r in rows], "strategies": rows, "contribution": rows,
            "combined_net_pnl": round(total, 2), "combined_capital": round(sum(r["capital"] or 0 for r in rows), 2),
            "combined_cumulative_pnl": combined, "concentration": concentration,
            "simultaneous_losses": sim,
            "assumptions": [
                "Each strategy was simulated on its OWN starting capital with its own risk limits. "
                "The combined figures below simply add their daily P&L: they are INDICATIVE, not a "
                "simulated portfolio (shared cash, buying power and account-wide loss limits across "
                "strategies were not simulated together).",
                "Strategy periods can differ (bar sizes have different data availability); check each "
                "row's period before comparing.",
                "Reporting currency: USD."]}


def _comparisons(results: Path) -> list[dict]:
    out = []
    found = sorted(results.glob("*/_comparisons/*.json"), key=lambda p: p.stem, reverse=True)
    for path in found[:20]:
        data = runs.read_json(path, {})
        if data:
            data["id"] = path.stem
            out.append(data)
    return out


def dashboard_data(results: Path | None = None) -> dict:
    results = results or paths.BACKTEST_RESULTS_DIR
    metas = runs.list_runs(results)
    summaries = [_summary(m, Path(m["_dir"])) for m in metas]
    want = []
    specs = discover()
    for sid in specs:  # latest baseline per strategy first
        base = next((m for m in metas if m.get("strategy_id") == sid and m.get("kind") == "baseline"
                     and str(m.get("status")).startswith("completed")), None)
        if base:
            want.append(base)
    comparisons = _comparisons(results)
    seen_strategies = set()
    for c in comparisons:  # the latest comparison of each strategy, in full
        if c.get("strategy_id") in seen_strategies:
            continue
        seen_strategies.add(c.get("strategy_id"))
        want += [m for m in metas if m["run_id"] in [c["baseline_run"], *c["variant_runs"]]]
    for m in metas:
        if len({x["run_id"] for x in want}) >= DETAIL_RUNS:
            break
        want.append(m)
    details = {}
    for m in want:
        if m["run_id"] not in details and (Path(m["_dir"]) / "metrics.json").exists():
            details[m["run_id"]] = run_payload(Path(m["_dir"]))
    strategies = []
    for sid, spec in specs.items():
        mine = [s for s in summaries if s["strategy_id"] == sid]
        base = [s for s in mine if s["kind"] == "baseline"]
        ok = [s for s in base if str(s["status"]).startswith("completed")]
        strategies.append({"strategy_id": sid, "name": spec.name, "description": spec.description,
                           "timeframe": spec.timeframe, "holding": spec.holding,
                           "live_module": spec.live_module, "rules_file": str(spec.rules_file.name),
                           "latest_run": ok[0]["run_id"] if ok else None,
                           "last_attempt": base[0]["run_id"] if base else None,
                           "last_attempt_status": base[0]["status"] if base else "never run"})
    now = dt.datetime.now(dt.timezone.utc)
    return {"generated_at": now.isoformat(timespec="seconds"),
            "generated_at_ms": int(now.timestamp() * 1000),
            "currency": "USD", "timezone": "America/New_York",
            "strategies": strategies, "runs": summaries, "details": details,
            "comparisons": comparisons, "experiments": experiments.list_all(),
            "usage": runs.usage(results), "portfolio": portfolio(details, summaries),
            "limitations": [{"title": t, "text": x} for t, x in analysis.LIMITATIONS],
            "improvements": analysis.IMPROVEMENTS,
            "commands": COMMANDS}


COMMANDS = {
    "single": "paperbot-backtest run --strategy trend_join_long   (or macro_swing_etf, equity_mean_reversion)",
    "all": "paperbot-backtest all",
    "experiments": "paperbot-backtest all --with-experiments",
    "experiment": "paperbot-backtest experiment EXP-trend_join_long-rvol-lower",
    "propose": "paperbot-backtest propose",
    "compare": "paperbot-backtest compare <run_id_A> <run_id_B>",
    "report": "paperbot-backtest report",
    "weekly": "paperbot-backtest report --period weekly",
    "yahoo": "paperbot-backtest run --provider yfinance",
}


# ---------------------------------------------------------------- HTML

def _read(name: str) -> str:
    return (WEB / name).read_text(encoding="utf-8")


def render_html(title: str, payload: dict, view: str) -> str:
    """A self-contained page: inline styles, data and renderer (works from file://)."""
    body = json.dumps(payload, default=str, separators=(",", ":")).replace("</", "<\\/")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<script src="https://cdn.tailwindcss.com/3.4.16"></script>
<script>{_read("assets/tw-config.js")}</script>
<style>{_read("assets/theme.css")}</style>
</head>
<body class="min-h-screen bg-ink-950 font-sans text-slate-100 antialiased">
<main id="bt-app" class="mx-auto max-w-7xl space-y-5 px-4 py-5 sm:px-6"></main>
<script>window.PAPERBOT_BT_PAYLOAD = {body};</script>
<script>{_read("assets/bt.js")}</script>
<script>PBT.standalone({json.dumps(view)}, window.PAPERBOT_BT_PAYLOAD);</script>
</body>
</html>
"""


def _write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def publish_run(run_dir: Path, refresh_dashboard: bool = True) -> Path:
    payload = run_payload(run_dir)
    meta = payload["metadata"]
    title = f"Backtest {meta.get('strategy_name')} {meta.get('experiment_id') or ''} {meta['run_id']}"
    html = render_html(title.strip(), payload, "run")
    report = run_dir / "report.html"
    _write(report, html)
    folder = paths.BACKTEST_REPORTS_DIR / meta["strategy_id"]
    if run_dir.parent.parent.resolve() == paths.BACKTEST_RESULTS_DIR.resolve():
        _write(folder / f"{meta['run_id']}.html", html)
        if str(meta.get("status")).startswith("completed"):
            name = "latest.html" if meta.get("kind") == "baseline" else f"{meta['experiment_id']}-latest.html"
            _write(folder / name, html)
        if refresh_dashboard:
            publish_all()
    return report


def publish_all(results: Path | None = None, out_dir: Path | None = None) -> Path:
    from paperbot.reporting import dashboard  # copies every page in web/ (incl. backtesting)
    out_dir = out_dir or paths.DASHBOARD_DIR
    data = dashboard_data(results)
    dashboard.sync_site(out_dir)
    path = out_dir / DATA_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(data, default=str, separators=(",", ":")).replace("</", "<\\/")
    _write(path, f"window.PAPERBOT_BT = {body};\n")
    if results is None:
        _write(paths.BACKTEST_REPORTS_DIR / "portfolio" / "latest.html",
               render_html("Backtest portfolio summary", data, "portfolio"))
        _write(paths.BACKTEST_REPORTS_DIR / "analysis" / "latest.html",
               render_html("Backtest limitations and analysis", data, "analysis"))
    return path


def periodic(kind: str, today: dt.date | None = None) -> Path:
    """Weekly or monthly executive report: the runs of the period and the latest results."""
    today = today or dt.date.today()
    days = 7 if kind == "weekly" else 31
    since = (today - dt.timedelta(days=days)).isoformat()
    data = dashboard_data()
    data["period_report"] = {"kind": kind, "since": since, "until": today.isoformat(),
                             "runs": [r for r in data["runs"] if str(r.get("started_at", "")) >= since]}
    key = f"{today.isocalendar()[0]}-W{today.isocalendar()[1]:02d}" if kind == "weekly" else today.strftime("%Y-%m")
    path = paths.BACKTEST_REPORTS_DIR / "portfolio" / f"{kind}-{key}.html"
    _write(path, render_html(f"Backtest {kind} report {key}", data, "periodic"))
    return path


def copy_report(run_dir: Path, dest: Path) -> Path:
    shutil.copy2(run_dir / "report.html", dest)
    return dest
