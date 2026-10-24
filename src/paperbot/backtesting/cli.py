"""paperbot-backtest: historical backtests of paperbot's strategies (never trades).

    paperbot-backtest strategies                       list the strategies that can be backtested
    paperbot-backtest run [--strategy KEY] [--start YYYY-MM-DD] [--end YYYY-MM-DD]
                          [--days N] [--provider ibkr|yfinance|synthetic] [--symbols A B ...]
                          KEY = any strategy in config/rules.json (default: its default_strategy)
    paperbot-backtest all [--strategy KEY ...] [--with-experiments]
                                                        every strategy (+ its experiments)
    paperbot-backtest propose [--strategy KEY]          write experiment ideas to backtesting/experiments/
    paperbot-backtest experiment EXP-id [EXP-id ...]    baseline + those variants on the same data
    paperbot-backtest compare RUN_A RUN_B               metric-by-metric comparison of two runs
    paperbot-backtest list                              past runs
    paperbot-backtest report [--period weekly|monthly]  rebuild dashboard data and reports
    paperbot-backtest prune --keep N [--dry-run]        delete old runs (explicit only)

Exit code 0 on success, 1 on a failed run (the failure is recorded in its run folder),
2 on bad arguments. Every run of this command sets PAPERBOT_BACKTEST=1, so paperbot's
live order code refuses to run inside it.
"""
import argparse
import datetime as dt
import json
import logging
import sys
from pathlib import Path

from paperbot import paths
from paperbot.backtesting import experiments, report, runner, runs
from paperbot.backtesting.data import DataError
from paperbot.backtesting.strategies import discover

log = logging.getLogger("paperbot.backtest")


def _date(text: str) -> dt.date:
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a date (YYYY-MM-DD): {text}") from None


def _period(args, cfg):
    end = args.end
    start = args.start
    if args.days and not start:
        end = end or (dt.date.today() - dt.timedelta(days=1))
        start = end - dt.timedelta(days=args.days)
    return start, end


def _provider(args, cfg, notes):
    name = args.provider or cfg["provider"]
    if name == "synthetic":
        notes.append("SYNTHETIC DATA: generated prices for demonstration only. NOT market data.")
    return runner.make_provider(name, cfg, notes)


def _print_run(run_dir):
    meta = runs.read_json(run_dir / "run_metadata.json", {})
    h = meta.get("headline") or {}
    print(f"run {meta.get('run_id')}: {meta.get('status')}"
          + (" [SYNTHETIC DATA]" if meta.get("synthetic_data") else ""))
    period = meta.get("period") or {}
    print(f"  period {period.get('actual_start')} .. {period.get('actual_end')} "
          f"({period.get('trading_days')} trading days), provider {meta.get('provider')}")
    if h:
        print(f"  net P&L {h.get('net_pnl'):,.2f} USD ({h.get('total_return_pct')}% of starting capital), "
              f"{h.get('completed_trades')} trades, max drawdown {h.get('max_drawdown_pct')}%")
    print(f"  folder  {run_dir}")
    print(f"  report  {run_dir / 'report.html'}")


def _add_period(p):
    p.add_argument("--start", type=_date)
    p.add_argument("--end", type=_date)
    p.add_argument("--days", type=int, help="calendar days ending --end (default: yesterday)")
    p.add_argument("--provider", choices=["ibkr", "yfinance", "synthetic"])
    p.add_argument("--symbols", nargs="+", help="override the universe (default: rules.json fallback list)")
    p.add_argument("--refresh", action="store_true", help="re-download cached market data")
    p.add_argument("--no-stress", action="store_true", help="skip the 3x cost stress run")
    p.add_argument("--capital", type=float, help="starting capital in USD")


def main(argv: list[str] | None = None) -> int:
    runner.mark_process()
    parser = argparse.ArgumentParser(prog="paperbot-backtest", description=__doc__.split("\n")[0])
    parser.add_argument("--config", help="backtest settings file (default backtesting/config/backtest.json)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    specs = discover()
    default = json.loads(paths.RULES_FILE.read_text(encoding="utf-8")).get("default_strategy")
    sub.add_parser("strategies")
    p = sub.add_parser("run")
    p.add_argument("--strategy", default=default if default in specs else next(iter(specs)),
                   choices=sorted(specs))
    _add_period(p)
    p = sub.add_parser("all")
    p.add_argument("--strategy", nargs="+", choices=sorted(specs), help="only these strategies")
    p.add_argument("--with-experiments", action="store_true")
    _add_period(p)
    p = sub.add_parser("experiment")
    p.add_argument("ids", nargs="+")
    _add_period(p)
    p = sub.add_parser("propose")
    p.add_argument("--strategy", nargs="+", choices=sorted(specs), help="default: every strategy")
    p = sub.add_parser("compare")
    p.add_argument("run_a")
    p.add_argument("run_b")
    p = sub.add_parser("list")
    p.add_argument("--strategy")
    p = sub.add_parser("report")
    p.add_argument("--period", choices=["weekly", "monthly"])
    p = sub.add_parser("prune")
    p.add_argument("--keep", type=int, required=True)
    p.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    overrides = {"starting_capital": args.capital} if getattr(args, "capital", None) else None
    try:
        cfg = runner.load_config(Path(args.config) if args.config else None, overrides)
    except ValueError as e:
        print(f"backtest settings error: {e}", file=sys.stderr)
        return 2

    if args.cmd == "strategies":
        for sid, spec in specs.items():
            print(f"{sid:24s} {spec.name} ({spec.timeframe}, {spec.holding}): {spec.description}\n"
                  f"{'':24s} simulated on {spec.tape} bars; live code: {spec.live_module}")
        return 0
    if args.cmd == "list":
        for r in runs.list_runs():
            if args.strategy and r.get("strategy_id") != args.strategy:
                continue
            h = r.get("headline") or {}
            print(f"{r['run_id']}  {r.get('strategy_id')}  {r.get('kind'):10s} "
                  f"{(r.get('experiment_id') or ''):22s} {r.get('status'):24s} "
                  f"net {h.get('net_pnl', 'n/a')}  trades {h.get('completed_trades', 'n/a')}"
                  + ("  SYNTHETIC" if r.get("synthetic_data") else ""))
        return 0
    if args.cmd == "report":
        path = report.periodic(args.period) if args.period else report.publish_all()
        print(f"written: {path}\ndashboard: {paths.DASHBOARD_DIR / 'backtest.html'}")
        return 0
    if args.cmd == "prune":
        removed = runs.prune(args.keep, dry_run=args.dry_run)
        print(("would delete " if args.dry_run else "deleted ") + f"{len(removed)} run(s)")
        for r in removed:
            print("  " + r)
        if removed and not args.dry_run:
            report.publish_all()
        return 0
    if args.cmd == "propose":
        rules = json.loads(paths.RULES_FILE.read_text(encoding="utf-8"))
        for sid in args.strategy or list(specs):
            latest = next((r for r in runs.list_runs() if r.get("kind") == "baseline"
                           and r.get("strategy_id") == sid
                           and str(r.get("status")).startswith("completed")), None)
            base = runs.load_run(Path(latest["_dir"])) if latest else None
            for exp in experiments.propose(sid, rules, base):
                path = experiments.save(exp)
                print(f"{exp['id']}: {exp['changes']}  -> {path}")
        print("Proposals only: nothing was applied to config/rules.json.")
        return 0
    if args.cmd == "compare":
        a, b = runs.find_run(args.run_a), runs.find_run(args.run_b)
        if not a or not b:
            print("run not found (use `paperbot-backtest list`)", file=sys.stderr)
            return 2
        path = runner.compare_runs(a, [b], cfg)
        data = runs.read_json(path, {})
        print(f"comparison written to {path}")
        print(f"same dataset: {data['same_dataset']}  ({data['labels']})")
        for row in data["rows"]:
            if row["segment"] == "full":
                print(f"  {row['metric']:22s} " + "  ".join(f"{v!s:>14}" for v in row["values"]))
        report.publish_all()
        return 0

    notes: list[str] = []
    try:
        with runs.RunLock():
            provider = _provider(args, cfg, notes)
            start, end = _period(args, cfg)
            try:
                if args.cmd == "run":
                    run_dir = runner.run_one(specs[args.strategy], cfg, provider, start=start,
                                             end=end, symbols=args.symbols, stress=not args.no_stress,
                                             refresh=args.refresh, notes=notes)
                    _print_run(run_dir)
                    return 0
                if args.cmd == "all":
                    out = runner.run_suite(cfg, provider, strategy_ids=args.strategy,
                                           with_experiments=args.with_experiments,
                                           start=start, end=end, symbols=args.symbols,
                                           stress=not args.no_stress, refresh=args.refresh, notes=notes)
                else:  # experiment
                    out = runner.run_suite(cfg, provider, experiment_ids=args.ids, start=start,
                                           end=end, symbols=args.symbols, stress=not args.no_stress,
                                           refresh=args.refresh, notes=notes)
                print(json.dumps(out, indent=1))
                return 1 if out["failed"] else 0
            finally:
                provider.close()
    except runner.BacktestFailed as e:
        print(f"BACKTEST FAILED: {e}\n  details: {e.run_dir}", file=sys.stderr)
        try:
            report.publish_all()
        except Exception:  # noqa: BLE001
            pass
        return 1
    except (runs.RunLocked, DataError, runner.BacktestError, ValueError) as e:
        print(f"BACKTEST NOT RUN: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
