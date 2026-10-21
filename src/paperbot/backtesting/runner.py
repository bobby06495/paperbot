"""Run a backtest end to end and write its folder.

    run_one()    one strategy (or one experiment variant) over one period
    run_suite()  every requested strategy, optionally with its experiments, on the same
                 data and period, plus the comparison and reports

Safety (fail closed):
  * PAPERBOT_BACKTEST=1 (and PAPERBOT_NOTIFY=off) are set for the process: the live
    order functions (core/engine.handle_*), paperbot-bot, paperbot-cycle and paper-bot
    refuse to run, and nothing is pushed to Telegram/ntfy.
  * The simulator never imports core/engine.py (the order-sending module); its SimIB
    raises on any order method. The IBKR data provider is read-only.
  * config/rules.json is hashed before and after every run; a change fails the run.
    Experiment variants are written to a temporary file only.
  * The config snapshot holds the strategy's rules section, the backtest settings and
    config.json's "limits" only: no host, port, account id or secret.
"""
import copy
import datetime as dt
import hashlib
import json
import logging
import os
import tempfile
import traceback
from pathlib import Path

from paperbot import paths
from paperbot.backtesting import analysis, experiments, metrics, runs
from paperbot.backtesting import config as bt_config
from paperbot.backtesting.data import (DataError, ET, IBKRProvider, MarketData, SyntheticProvider,
                                       YFinanceProvider, today_et)
from paperbot.backtesting.engine import BacktestError, Simulator
from paperbot.backtesting.strategies import StrategySpec, discover
from paperbot.runtime.runstate import BACKTEST_ENV

log = logging.getLogger("paperbot.backtest")
STRESS_MULTIPLIER = 3


class BacktestFailed(RuntimeError):
    def __init__(self, message: str, run_dir: Path | None = None):
        super().__init__(message)
        self.run_dir = run_dir


def mark_process():
    """Every backtesting process carries these flags; live order code refuses to run
    under the first, notifications are silenced by the second."""
    os.environ[BACKTEST_ENV] = "1"
    os.environ["PAPERBOT_NOTIFY"] = "off"


def file_hash(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def read_symbols(path: Path) -> list[str]:
    lines = (line.split("#")[0].strip().upper() for line in path.read_text().splitlines())
    return [s for s in lines if s]


def universe(profile, cfg: dict, override: list[str] | None = None) -> list[str]:
    """The symbols to load: the profile's list or file, or for a watchlist strategy its
    fallback list (the candidates the daily watchlist is rebuilt from)."""
    if override:
        return [s.upper() for s in override]
    if cfg.get("symbols"):
        return [s.upper() for s in cfg["symbols"]]
    uni = profile.universe
    if "symbols" in uni:
        return [s.upper() for s in uni["symbols"]]
    if "file" in uni:
        return read_symbols(paths.CONFIG_DIR / uni["file"])
    symbols = read_symbols(paths.CONFIG_DIR / uni.get("fallback_file", "universe.txt"))
    return symbols[: int(uni.get("top_n") or len(symbols))]  # the prefilter's top_n


def live_config() -> dict:
    return json.loads(paths.CONFIG_FILE.read_text(encoding="utf-8"))


def limits_only(config: dict) -> dict:
    """Only the "limits" safety rails of config.json (no connection details)."""
    return {"limits": config.get("limits", {})}


def make_provider(name: str, cfg: dict, notes: list[str]):
    """The configured provider; falls back (and says so) if IBKR cannot connect."""
    if name == "synthetic":
        return SyntheticProvider()
    if name == "yfinance":
        return YFinanceProvider()
    if name == "ibkr":
        config = live_config()
        conn = (config.get("connections") or {}).get("paper") or config
        try:
            return IBKRProvider({k: conn.get(k) for k in ("host", "port", "client_id")}, cfg["ibkr"])
        except DataError as e:
            fallback = cfg.get("fallback_provider")
            if not fallback or fallback == "ibkr":
                raise
            notes.append(f"IBKR provider unavailable ({e}); fell back to {fallback}.")
            log.warning("IBKR provider unavailable (%s); using %s", e, fallback)
            return make_provider(fallback, cfg, notes)
    raise BacktestError(f"unknown provider {name!r}")


def resolve_period(cfg: dict, provider, start: dt.date | None, end: dt.date | None,
                   profile, tape: str, notes: list[str]) -> tuple[dt.date, dt.date, dt.date, dt.date]:
    """(requested start, requested end, start, end). The end is never later than
    yesterday (ET); Yahoo's short intraday history moves the start forward."""
    yesterday = today_et() - dt.timedelta(days=1)
    req_end = end or yesterday
    req_start = start or (req_end - dt.timedelta(days=cfg["lookback_calendar_days"]))
    if req_start > req_end:
        raise BacktestError(f"start {req_start} is after end {req_end}")
    s, e = req_start, min(req_end, yesterday)
    if e < req_end:
        notes.append(f"End moved from {req_end} to {e}: today's session is incomplete.")
    if isinstance(provider, YFinanceProvider):
        rvol = next((b for b in profile.entry if b.type == "min_rvol"), None)
        sessions = (rvol.lookback_days + 6) if rvol else 0
        earliest = YFinanceProvider.earliest(tape) + dt.timedelta(days=int(sessions * 7 / 5) + 2)
        if s < earliest:
            why = f" and the RVOL check needs {rvol.lookback_days} earlier sessions" if rvol else ""
            notes.append(f"Start moved from {s} to {earliest}: Yahoo serves {tape} bars for a short "
                         f"history only{why}.")
            s = earliest
    if s > e:
        raise BacktestError(f"no testable days between {s} and {e} with provider {provider.name}")
    return req_start, req_end, s, e


def _execution_log(run_dir: Path) -> logging.Handler:
    class JsonLines(logging.Formatter):
        def format(self, record):
            return json.dumps({"time": dt.datetime.now(ET).isoformat(timespec="seconds"),
                               "level": record.levelname, "message": record.getMessage()})
    handler = logging.FileHandler(run_dir / "execution.log", encoding="utf-8")
    handler.setFormatter(JsonLines())
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    return handler


def load_profile(spec: StrategySpec, rules: dict, config: dict, workdir: Path):
    """The (possibly experimental) profile, validated by core/profile.py from a copy of
    the rules in `workdir`; config/rules.json itself is only ever read."""
    path = workdir / "rules.json"
    path.write_text(json.dumps(rules), encoding="utf-8")
    try:
        return spec.load_profile(path, config)
    except Exception as e:  # noqa: BLE001 - ConfigError: refuse like the bot would
        raise BacktestError(f"rules are not a valid strategy: {e}") from None


def run_one(spec: StrategySpec, cfg: dict, provider, *, start: dt.date | None = None,
            end: dt.date | None = None, symbols: list[str] | None = None,
            experiment: dict | None = None, kind: str = "baseline", stress: bool = True,
            results_dir: Path | None = None, cache_dir: Path | None = None,
            refresh: bool = False, config: dict | None = None, notes: list[str] | None = None,
            rules_override: dict | None = None, publish: bool = True) -> Path:
    """Run one backtest and write backtesting/results/<strategy_id>/<run_id>/.
    Raises BacktestFailed (after writing a 'failed' run folder) on any error."""
    mark_process()
    results_dir = results_dir or paths.BACKTEST_RESULTS_DIR
    cache_dir = cache_dir if cache_dir is not None else paths.BACKTEST_CACHE_DIR
    notes = list(notes or [])
    production_hash = file_hash(spec.rules_file)
    config = config or live_config()
    rules = copy.deepcopy(rules_override) if rules_override else spec.rules()
    if experiment:
        if experiment.get("strategy_id") not in (None, spec.strategy_id):
            raise BacktestError(f"experiment {experiment['id']} is for {experiment['strategy_id']}")
        rules = experiments.variant_rules(rules, spec.strategy_id, experiment["changes"])
    section = rules["strategies"][spec.strategy_id]
    run_id = runs.new_run_id()
    run_dir = results_dir / spec.strategy_id / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    handler = _execution_log(run_dir)
    started = dt.datetime.now(dt.timezone.utc)
    meta = {"run_id": run_id, "strategy_id": spec.strategy_id, "strategy_name": section.get("label"),
            "strategy_version": runs.rules_version(section), "kind": kind,
            "experiment_id": experiment["id"] if experiment else None,
            "experiment": experiment, "status": "running",
            "started_at": started.isoformat(timespec="seconds"), "finished_at": None,
            "code": runs.git_info(), "software": runs.software_info(),
            "provider": provider.name, "synthetic_data": bool(provider.synthetic),
            "timeframe": spec.timeframe, "holding": spec.holding, "tape": spec.tape,
            "currency": cfg["currency"], "starting_capital": cfg["starting_capital"],
            "execution_assumptions": cfg["execution"],
            "cost_assumptions": {"commission": "IBKR Fixed: $0.005/share, min $1.00, max 1% of value, "
                                               "per order (core/risk.round_trip_commission / 2)",
                                 "half_spread_bps": cfg["execution"]["half_spread_bps"],
                                 "slippage_bps": cfg["execution"]["slippage_bps"]},
            "timezone": "America/New_York (ET) for every timestamp",
            "warnings": [], "errors": [], "pinned": False}
    runs.write_json(run_dir / "run_metadata.json", meta)
    runs.write_json(run_dir / "config_snapshot.json",
                    {"strategy_id": spec.strategy_id, "rules": section,
                     "production_rules_sha256": production_hash, "backtest": cfg,
                     "limits": config.get("limits", {}),
                     "experiment_changes": experiment["changes"] if experiment else {}})
    try:
        with tempfile.TemporaryDirectory(prefix="paperbot-bt-rules-") as work:
            profile = load_profile(spec, rules, limits_only(config), Path(work))
        log.info("run %s: %s (%s) with %s", run_id, profile.label, kind, provider.name)
        req_start, req_end, s, e = resolve_period(cfg, provider, start, end, profile, spec.tape, notes)
        meta["period"] = {"requested_start": req_start.isoformat(), "requested_end": req_end.isoformat()}
        syms = universe(profile, cfg, symbols)
        data = MarketData(provider, cache_dir, refresh, bar=spec.tape)
        q = data.quality
        q.requested_start, q.requested_end = req_start.isoformat(), req_end.isoformat()
        q.symbols_requested = syms
        warmup = s - dt.timedelta(days=cfg["daily_warmup_calendar_days"])
        bench_symbol = cfg.get("benchmark_symbol")
        data.load_daily(syms + ([bench_symbol] if bench_symbol and bench_symbol not in syms else []),
                        warmup, e)
        q.symbols_with_data = [x for x in syms if any(b.date >= s for b in data.daily(x))]
        q.symbols_without_data = [x for x in syms if x not in q.symbols_with_data]
        for x in q.symbols_without_data:
            q.warn(f"{x}: no daily data in the period (excluded)")
        if not q.symbols_with_data:
            raise DataError(f"no daily data for any of {len(syms)} symbols between {s} and {e}")
        for note in notes:
            q.limit(note)
        result = Simulator(profile, data, cfg, q.symbols_with_data, s, e).run(
            progress=lambda i, n: log.info("simulated %d/%d days", i, n))
        q.actual_start, q.actual_end = result.days[0], result.days[-1]
        for text in provider.limitations():
            q.limit(text)
        if len(result.days) < 5:
            q.warn(f"only {len(result.days)} trading days simulated")
        bench = [b for b in data.daily(bench_symbol) if s <= b.date <= e] if bench_symbol else []
        m = metrics.compute(result.trades, result.open_trades, result.equity, result.daily,
                            cfg["starting_capital"], bench, cfg["validation"])
        per = metrics.periods(result.days, result.trades, result.equity, result.daily, cfg["validation"])
        dataset_id = _dataset_id(provider, spec.tape, s, e, q.symbols_with_data, data)
        usage_entry = runs.record_usage(dataset_id, run_id, meta["experiment_id"], results_dir)
        stress_result = _stress(profile, provider, cache_dir, cfg, q.symbols_with_data, s, e,
                                spec.tape) if stress else None
        m.update({"periods": per, "monthly": metrics.monthly(result.daily),
                  "by_symbol": metrics.breakdown(result.trades, "symbol"),
                  "by_exit_reason": metrics.breakdown(result.trades, "exit_reason"),
                  "funnel": result.funnel, "stress": stress_result, "definitions": metrics.DEFINITIONS,
                  "benchmark_symbol": bench_symbol,
                  "benchmark_series": ([[bench[0].date.isoformat(), bench[0].open]]
                                       + [[b.date.isoformat(), b.close] for b in bench]) if bench else []})
        quality = q.to_dict()
        questions = analysis.research_questions(m, per, result.funnel, result.trades, quality,
                                                stress_result, usage_entry, cfg)
        m["research_questions"] = questions
        meta.update({"dataset_id": dataset_id, "warnings": result.warnings + q.warnings,
                     "period": {**meta["period"], "actual_start": result.days[0],
                                "actual_end": result.days[-1], "trading_days": len(result.days)},
                     "symbols": q.symbols_with_data, "variants_on_dataset": len(usage_entry["variants"])})
        m["summary"] = analysis.summary(profile, meta, m, per, quality)
        m["assessment"] = analysis.assessment(m, per, quality, stress_result, questions, cfg)
        m["rules_explained"] = analysis.explain_profile(profile)
        checks = _checks(result, cfg, spec, production_hash)
        validation = {"data_quality": quality, "periods_method": per["method"], "checks": checks,
                      "risk_state": result.risk_state, "dataset_id": dataset_id,
                      "dataset_usage": usage_entry, "data_hashes": len(data.hashes),
                      "strategy_log": result.strategy_log, "watchlists": result.watchlists}
        runs.write_csv(run_dir / "trades.csv", result.trades + result.open_trades)
        runs.write_csv(run_dir / "orders.csv", result.orders)
        runs.write_csv(run_dir / "fills.csv", result.fills)
        runs.write_csv(run_dir / "equity_curve.csv", result.equity)
        runs.write_csv(run_dir / "daily_performance.csv", result.daily)
        runs.write_csv(run_dir / "monthly_performance.csv", m["monthly"])
        runs.write_json(run_dir / "metrics.json", m)
        runs.write_json(run_dir / "validation.json", validation)
        runs.write_json(run_dir / "trade_context.json", result.trade_context)
        with open(run_dir / "events.jsonl", "w", encoding="utf-8") as f:
            for ev in result.events:
                f.write(json.dumps(ev, default=str) + "\n")
        failed = [c for c in checks if c["status"] == "fail"]
        meta["status"] = "failed" if failed else ("completed_with_warnings"
                                                  if quality["status"] != "pass" else "completed")
        meta["errors"] = [c["detail"] for c in failed]
        meta["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        meta["headline"] = {k: m.get(k) for k in (
            "net_pnl", "total_return_pct", "completed_trades", "win_rate_pct", "max_drawdown_pct",
            "profit_factor", "total_commission", "total_slippage", "ending_equity")}
        meta["data_status"] = quality["status"]
        meta["out_of_sample"] = per["segments"]["out_of_sample"].get("status") == "ok"
        runs.write_json(run_dir / "run_metadata.json", meta)
        log.info("run %s finished: %s, net P&L %.2f USD, %d trades", run_id, meta["status"],
                 m["net_pnl"], m["completed_trades"])
        if publish:
            from paperbot.backtesting import report
            report.publish_run(run_dir)
        if failed:
            raise BacktestFailed("; ".join(meta["errors"]), run_dir)
        return run_dir
    except BacktestFailed:
        raise
    except Exception as exc:  # noqa: BLE001 - every failure is recorded, then re-raised
        meta.update({"status": "failed", "errors": meta["errors"] + [f"{type(exc).__name__}: {exc}"],
                     "finished_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")})
        runs.write_json(run_dir / "run_metadata.json", meta)
        log.error("run %s failed: %s\n%s", run_id, exc, traceback.format_exc())
        raise BacktestFailed(f"{type(exc).__name__}: {exc}", run_dir) from exc
    finally:
        log.removeHandler(handler)
        handler.close()
        if file_hash(spec.rules_file) != production_hash:  # must never happen
            raise BacktestFailed(f"{spec.rules_file} changed during the backtest", run_dir)


def _dataset_id(provider, tape, s, e, symbols, data: MarketData) -> str:
    """Identifies the market data a comparison runs on: provider, bar size, period,
    symbols and the content hash of every daily series. Variants on the same data share it."""
    wanted = {f"{x}:1d" for x in symbols}
    daily = "|".join(f"{k}={v}" for k, v in sorted(data.hashes.items()) if k in wanted)
    key = f"{provider.name}|{tape}|{s}|{e}|{','.join(sorted(symbols))}|{daily}"
    return (f"{provider.name.split()[0]}-{tape}-{s:%Y%m%d}-{e:%Y%m%d}-"
            f"{hashlib.sha256(key.encode()).hexdigest()[:10]}")


def _stress(profile, provider, cache_dir, cfg, symbols, s, e, tape) -> dict:
    """The same run with STRESS_MULTIPLIER x half-spread and slippage."""
    hard = copy.deepcopy(cfg)
    hard["execution"]["half_spread_bps"] *= STRESS_MULTIPLIER
    hard["execution"]["slippage_bps"] *= STRESS_MULTIPLIER
    data = MarketData(provider, cache_dir, bar=tape)
    data.load_daily(symbols, s - dt.timedelta(days=cfg["daily_warmup_calendar_days"]), e)
    res = Simulator(profile, data, hard, symbols, s, e).run()
    m = metrics.compute(res.trades, res.open_trades, res.equity, res.daily, cfg["starting_capital"])
    return {"multiplier": STRESS_MULTIPLIER, "half_spread_bps": hard["execution"]["half_spread_bps"],
            "slippage_bps": hard["execution"]["slippage_bps"],
            **{k: m.get(k) for k in ("net_pnl", "total_return_pct", "completed_trades",
                                     "max_drawdown_pct", "total_slippage", "profit_factor")}}


def _checks(result, cfg: dict, spec: StrategySpec, production_hash: str | None) -> list[dict]:
    """Internal consistency checks written to validation.json."""
    checks = []

    def add(key, label, ok, detail, warn=False):
        checks.append({"key": key, "label": label,
                       "status": "pass" if ok else ("warn" if warn else "fail"), "detail": detail})

    nets = sum(float(t["net_pnl"]) for t in result.trades + result.open_trades)
    expected = cfg["starting_capital"] + nets
    add("equity_reconciles", "Trade ledger reconciles with ending equity",
        abs(expected - result.final_equity) < 0.05 + 0.01 * len(result.trades),
        f"starting capital + sum of trade net P&L = {expected:,.2f}; simulated ending equity = "
        f"{result.final_equity:,.2f} USD")
    bought = sum(int(f["quantity"]) for f in result.fills if f["side"] == "BUY")
    traded = sum(int(t["quantity"]) for t in result.trades + result.open_trades)
    add("fills_match_trades", "Every bought share belongs to a trade", bought == traded,
        f"{bought} shares bought in fills, {traded} in the trade ledger")
    by_trade: dict = {}
    for f in result.fills:
        by_trade[f["trade_id"]] = by_trade.get(f["trade_id"], 0) + (
            int(f["quantity"]) if f["side"] == "BUY" else -int(f["quantity"]))
    shorts = [k for k, v in by_trade.items() if v < 0]
    add("no_short", "No trade ever sold more shares than it bought", not shorts,
        f"{len(by_trade)} trades checked; {len(shorts)} oversold")
    add("rules_unchanged", "config/rules.json was not modified",
        file_hash(spec.rules_file) == production_hash, "SHA-256 compared before and after the run")
    add("isolated", "Backtest process is marked as isolated from live trading",
        os.environ.get(BACKTEST_ENV) == "1",
        f"{BACKTEST_ENV}=1, so paperbot's order functions refuse to run in this process")
    add("open_positions", "No positions left open at the end", not result.open_trades,
        f"{len(result.open_trades)} open position(s) marked at their last price", warn=True)
    return checks


# ---------------------------------------------------------------- suites

def run_suite(cfg: dict, provider, *, strategy_ids: list[str] | None = None,
              with_experiments: bool = False, experiment_ids: list[str] | None = None,
              start=None, end=None, symbols=None, stress: bool = True, refresh: bool = False,
              notes: list[str] | None = None, publish: bool = True) -> dict:
    """Baseline for each strategy; then its requested experiments on the same period, and a
    comparison. Failures are recorded and the suite continues."""
    out = {"runs": [], "failed": [], "comparisons": []}
    specs = discover()
    wanted_exps = [experiments.load(x) for x in experiment_ids or []]
    if wanted_exps and not strategy_ids:
        strategy_ids = sorted({x["strategy_id"] for x in wanted_exps})
    for sid in strategy_ids or list(specs):
        spec = specs[sid]
        try:
            base_dir = run_one(spec, cfg, provider, start=start, end=end, symbols=symbols,
                               stress=stress, refresh=refresh, notes=notes, publish=False)
            out["runs"].append(str(base_dir))
        except BacktestFailed as e:
            out["failed"].append({"strategy": sid, "error": str(e), "run_dir": str(e.run_dir)})
            continue
        exps = [x for x in wanted_exps if x["strategy_id"] == sid] or (
            [x for x in experiments.list_all(strategy=sid) if "invalid" not in x]
            if with_experiments else [])
        if not exps:
            continue
        base = runs.load_run(base_dir)
        period = base["metadata"]["period"]
        s, e = dt.date.fromisoformat(period["actual_start"]), dt.date.fromisoformat(period["actual_end"])
        variant_dirs = []
        for exp in exps:
            try:
                d = run_one(spec, cfg, provider, start=s, end=e, symbols=base["metadata"]["symbols"],
                            experiment=exp, kind="experiment", stress=False, notes=notes, publish=False)
                variant_dirs.append(d)
                out["runs"].append(str(d))
            except (BacktestFailed, experiments.ExperimentError, BacktestError) as err:
                out["failed"].append({"strategy": sid, "experiment": exp["id"], "error": str(err)})
        if variant_dirs:
            out["comparisons"].append(str(compare_runs(base_dir, variant_dirs, cfg)))
    if publish:
        from paperbot.backtesting import report
        for path in out["runs"]:
            report.publish_run(Path(path), refresh_dashboard=False)
        report.publish_all()
    return out


def compare_runs(base_dir: Path, variant_dirs: list[Path], cfg: dict) -> Path:
    base = runs.load_run(base_dir)
    variants = [runs.load_run(d) for d in variant_dirs]
    entry = runs.usage(base_dir.parent.parent).get(base["metadata"].get("dataset_id"))
    result = experiments.compare(base, variants, cfg, entry)
    result["created_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    path = base_dir.parent / "_comparisons" / f"{runs.new_run_id()}.json"
    runs.write_json(path, result)
    return path


def load_config(path: Path | None = None, overrides: dict | None = None) -> dict:
    return bt_config.load(path, overrides)
