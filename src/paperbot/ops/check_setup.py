"""Offline setup check for paperbot. Never connects to IBKR or places orders.

    paperbot-check      (same as python -m paperbot.ops.check_setup)

Verifies, in order:
  * the dependencies import (ib_async, Eastern Time zone data)
  * every paperbot module imports without side effects
  * the project folders (config/, data/, logs/) resolve where expected
  * config.json + rules.json load into valid limits on a paper port
  * rules.json parses into a supported strategy
  * universe.txt / watchlist.txt tickers look like IBKR symbols, and
    whether today's watchlist exists (else the bot falls back to universe.txt)
  * the state files in logs/ are readable (not corrupt)
  * whether Telegram / ntfy notifications are configured (values never shown)
  * whether a STOP file or a running bot (logs/bot.pid) is present

Exits 0 when there are no problems (warnings are informational), 1 otherwise.
"""
import importlib
import json
import re
import sys

from paperbot import paths

MODULES = (
    "paperbot.paths",
    "paperbot.runtime.runstate", "paperbot.trading.risk", "paperbot.trading.strategy",
    "paperbot.trading.bot", "paperbot.runtime.kill_switch", "paperbot.runtime.stopbot",
    "paperbot.market.sp500_tickers", "paperbot.market.morning_prefilter",
    "paperbot.runtime.cycle", "paperbot.reporting.notify", "paperbot.reporting.compute_perf",
    "paperbot.ops.rotate_logs", "paperbot.ops.setup_schedule", "paperbot.ops.cleanup_schedule",
    "paperbot.ops.run_tests", "paperbot.ops.launch",
)


def run_checks() -> tuple[list[str], list[str]]:
    """Return (problems, warnings)."""
    problems, warnings = [], []

    try:
        import ib_async
        from zoneinfo import ZoneInfo
        ZoneInfo("America/New_York")
        print(f"ok  ib_async {ib_async.__version__}, Eastern Time zone data")
    except Exception as e:  # noqa: BLE001 - report any setup failure
        problems.append(f"dependencies: {e!r} (run: pip install -r requirements.txt)")
        return problems, warnings

    for name in MODULES:
        try:
            importlib.import_module(name)
        except Exception as e:  # noqa: BLE001
            problems.append(f"import {name}: {e!r}")
    if problems:
        return problems, warnings
    print(f"ok  {len(MODULES)} paperbot modules import")
    if not paths.CONFIG_FILE.exists():
        problems.append(f"{paths.CONFIG_FILE} not found: project root is {paths.PROJECT_ROOT} "
                        "(set PAPERBOT_HOME if the package is not an editable install)")
        return problems, warnings
    print(f"ok  project root {paths.PROJECT_ROOT}")

    from paperbot.runtime import runstate
    from paperbot.trading import strategy
    from paperbot.trading.risk import PAPER_PORTS, load_limits

    try:
        cfg = json.loads(paths.CONFIG_FILE.read_text())
        limits = load_limits(cfg, paths.RULES_FILE)
        if cfg["port"] not in PAPER_PORTS:
            problems.append(f"config.json port {cfg['port']} is not a paper port {PAPER_PORTS}")
        missing = [k for k in ("host", "client_id", "loop_seconds",
                               "lookup_timeout_seconds", "time_in_force") if k not in cfg]
        if missing:
            problems.append(f"config.json is missing {missing}")
        print(f"ok  config.json + rules.json limits (port {cfg['port']}, "
              f"max {limits.max_concurrent_positions} positions)")
    except (OSError, ValueError, KeyError, TypeError) as e:
        problems.append(f"config.json/rules.json: {e!r}")

    try:
        rules = strategy.load_rules()
        print(f"ok  rules.json: {rules['strategy_name']}")
    except SystemExit as e:
        problems.append(f"rules.json: {e}")
    except (OSError, ValueError, KeyError, TypeError) as e:
        problems.append(f"rules.json: {e!r}")

    for path in (strategy.UNIVERSE_FILE, strategy.WATCHLIST_FILE):
        if not path.exists():
            continue
        symbols = strategy.read_symbols(path)
        bad = [s for s in symbols if not re.fullmatch(r"[A-Z]+( [A-Z])?", s)]
        if bad:
            warnings.append(f"{path.name}: unusual IBKR tickers {bad} "
                            "(share classes use a space, e.g. 'BRK B')")
        print(f"ok  {path.name}: {len(symbols)} symbols")
    from paperbot.trading.risk import today_et
    generated = strategy.watchlist_date()
    if generated != today_et():
        fallback = "universe.txt" if strategy.UNIVERSE_FILE.exists() else "nothing (no universe.txt)"
        warnings.append(f"watchlist.txt {'from ' + str(generated) if generated else 'missing'}: "
                        f"the bot will scan {fallback} until morning_prefilter.py runs today")

    for path in (paths.LOGS_DIR / "state.json", strategy.STATE):
        try:
            runstate.read_json_state(path)
        except runstate.StateCorrupt as e:
            problems.append(str(e))
    print("ok  state files checked")

    from paperbot.reporting import notify  # report what is configured, never the values
    telegram = bool(notify.TELEGRAM_BOT_TOKEN and notify.TELEGRAM_CHAT_ID)
    if telegram or notify.NOTIFY_URL:
        print(f"ok  notifications: Telegram {'on' if telegram else 'off'}, "
              f"ntfy fallback {'on' if notify.NOTIFY_URL else 'off'}")
    else:
        warnings.append("notifications off: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID "
                        "(or NOTIFY_URL) in .env")

    if runstate.stop_requested():
        warnings.append("STOP file present: the bot will not start until it is deleted")
    pid = runstate.running_pid()
    if pid is not None:
        warnings.append(f"paperbot is running (pid {pid})")
    return problems, warnings


def main() -> int:
    problems, warnings = run_checks()
    for w in warnings:
        print("WARN", w)
    for p in problems:
        print("FAIL", p)
    print("Setup OK." if not problems else f"{len(problems)} problem(s) found.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
