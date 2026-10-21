"""paper-bot: one command for Windows, macOS, Linux, servers and Docker.

    paper-bot start [--strategy KEY] [--yes] [--dry-run]
                        choose a strategy (menu), then run the whole trading day:
                        strategy passes, the built-in scheduler (watchlist, log
                        rotation, daily summary) and automatic reconnects.
                        Sends orders unless --dry-run. Ctrl+C or `paper-bot stop`.
    paper-bot stop [--timeout N]      graceful stop (stops stay at IBKR)
    paper-bot status                  mode, strategy, process, last pass, jobs
    paper-bot strategies              every rules.json strategy with its settings
    paper-bot check                   setup check (config, rules.json, state files)
    paper-bot dashboard [--open]      rebuild the dashboard (and open it)
    paper-bot kill [--all]            cancel the bot's entries/exits (--all: every order)
    paper-bot service install|uninstall|status [--dry-run] [--live-confirm ID]
                                      start automatically at boot/log-on
    paper-bot test                    run the unit tests

Paper or live is set in config.json ("mode"); see core/broker.py. Environment:
PAPERBOT_STRATEGY (strategy for --strategy), PAPERBOT_LIVE_CONFIRM (live,
unattended), PAPERBOT_IB_HOST / PAPERBOT_IB_PORT (e.g. Docker), PAPERBOT_HOME.
"""
import argparse
import datetime as dt
import json
import os
import sys

from paperbot import paths


def _start(args) -> int:
    from paperbot.core import engine
    argv = [] if args.dry_run else ["--submit"]
    key = args.strategy or os.environ.get("PAPERBOT_STRATEGY")
    if key:
        argv += ["--strategy", key]
    if args.yes:
        argv.append("--yes")
    if sys.stdin.isatty():  # a person ran `paper-bot start`: a STOP file left by `stop` is cleared
        argv.append("--clear-stop")
    engine.main(argv)
    return 0


def status_lines() -> list[str]:
    from paperbot.core import broker, clock, selector
    from paperbot.core.risk import ConfigError
    from paperbot.runtime import runstate
    lines = []
    try:
        conn = broker.settings(json.loads(paths.CONFIG_FILE.read_text()))
        lines.append(f"mode:       {conn.describe()}")
    except (ConfigError, OSError, ValueError) as e:
        lines.append(f"mode:       INVALID config.json ({e})")
    try:
        lines.append(f"strategy:   {selector.unattended_key()}")
    except ConfigError as e:
        lines.append(f"strategy:   none chosen ({e})")
    pid = runstate.running_pid()
    lines.append(f"process:    {'running, pid ' + str(pid) if pid else 'not running'}"
                 f"{'  (STOP file present)' if runstate.stop_requested() else ''}")
    try:
        beat = runstate.read_json_state(runstate.HEARTBEAT_FILE) or {}
    except runstate.StateCorrupt:
        beat = {}
    if beat:
        lines.append(f"last pass:  {beat.get('time')} ({beat.get('source')}, {beat.get('mode')}, "
                     f"{beat.get('status')}){' ' + beat['error'] if beat.get('error') else ''}")
        for p in beat.get("positions") or []:
            lines.append(f"  position  {p.get('symbol')} {p.get('qty'):g} @ {p.get('entry')}  "
                         f"stop {p.get('stop')}")
    try:
        jobs = (runstate.read_json_state(clock.STATE_FILE) or {}).get("jobs", {})
    except runstate.StateCorrupt:
        jobs = {}
    for name, job in sorted(jobs.items()):
        lines.append(f"job:        {name:<14} {job.get('status', '-'):<8} slot {str(job.get('slot', '-'))[:16]}")
    lines.append(f"dashboard:  {paths.DASHBOARD_FILE}")
    return lines


def _status(args) -> int:
    print("\n".join(status_lines()))
    return 0


def _strategies(args) -> int:
    from paperbot.core import selector
    print("\n".join(selector.describe_all()))
    return 0


def _check(args) -> int:
    from paperbot.ops import check_setup
    return check_setup.main()


def _dashboard(args) -> int:
    from paperbot.reporting import compute_perf
    out = compute_perf.refresh_dashboard()
    print(f"dashboard: {paths.DASHBOARD_FILE}")
    if args.open:
        import webbrowser
        webbrowser.open(paths.DASHBOARD_FILE.as_uri())
    return 0 if out else 1


def _stop(args) -> int:
    from paperbot.runtime import stopbot
    return stopbot.main(["--timeout", str(args.timeout)])


def _kill(args) -> int:
    from paperbot.runtime import kill_switch
    kill_switch.main(["--all"] if args.all else [])
    return 0


def _service(args) -> int:
    from paperbot.system import services
    if args.action == "install":
        return services.install(args.dry_run, args.live_confirm)
    if args.action == "uninstall":
        return services.uninstall(args.dry_run)
    return services.status()


def _test(args) -> int:
    from paperbot.ops import run_tests
    return run_tests.main([])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="paper-bot", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("start", help="choose a strategy and run the trading day")
    start.add_argument("--strategy", metavar="KEY", help="rules.json strategy (skips the menu)")
    start.add_argument("--yes", action="store_true", help="no start-up confirmation")
    start.add_argument("--dry-run", action="store_true", help="log orders instead of sending them")
    start.set_defaults(func=_start)
    stop = sub.add_parser("stop", help="graceful stop")
    stop.add_argument("--timeout", type=int, default=120)
    stop.set_defaults(func=_stop)
    sub.add_parser("status", help="what is running").set_defaults(func=_status)
    sub.add_parser("strategies", help="list rules.json strategies").set_defaults(func=_strategies)
    sub.add_parser("check", help="setup check").set_defaults(func=_check)
    dash = sub.add_parser("dashboard", help="rebuild the dashboard")
    dash.add_argument("--open", action="store_true", help="open it in the browser")
    dash.set_defaults(func=_dashboard)
    kill = sub.add_parser("kill", help="cancel the bot's working orders")
    kill.add_argument("--all", action="store_true", help="every open order, stops included")
    kill.set_defaults(func=_kill)
    service = sub.add_parser("service", help="start automatically at boot/log-on")
    service.add_argument("action", choices=("install", "uninstall", "status"))
    service.add_argument("--dry-run", action="store_true", help="print what would be done")
    service.add_argument("--live-confirm", metavar="ACCOUNT_ID",
                         help="live mode: allow the service to trade this account unattended")
    service.set_defaults(func=_service)
    sub.add_parser("test", help="run the unit tests").set_defaults(func=_test)
    return parser


def main(argv: list[str] | None = None) -> int:
    if os.environ.get("PAPERBOT_BACKTEST") == "1":  # backtests never start the live runner
        print("paper-bot refused: this is a backtesting process (PAPERBOT_BACKTEST=1)")
        return 1
    args = build_parser().parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
