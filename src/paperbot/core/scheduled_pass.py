"""One-pass launcher for Windows Task Scheduler (alternative to a long-running bot.py).

    python -m paperbot.core.scheduled_pass               one dry-run pass (sends nothing)
    python -m paperbot.core.scheduled_pass --submit      one real pass on the IB Gateway PAPER account
    python -m paperbot.core.scheduled_pass --print-task  print a schtasks command (installs nothing)
(`paperbot-cycle` is the same command.) For the full schedule use paperbot-schedule.

Schedule it every 5 minutes, around the clock. Each run:
  1. Time gate on the ET clock, before any heavy import, so off-hours runs exit
     in well under a second: weekend or outside 09:30-16:10 ET -> exit 0.
  2. Skips quietly if a STOP file exists or bot.py / another cycle is running
     (logs/bot.pid), then claims the pid file for the duration of the run.
  3. Connects with the bot's clientId (so it can manage the bot's own stops);
     on failure waits 5 s and retries once, then exits 1. Verifies paper.
  4. Runs ONE pass of the existing bot logic: daily-loss check, then
     strategy.decide() -> risk guard -> bot.handle_request (brackets, stop
     updates, confirmed cancels). No trading logic lives in this file.
  5. Force-close window (rules.json force_close_et until 16:00 ET): stays
     connected and repeats the pass every 30 s until flat or 16:00, because
     5-minute runs would allow only two close attempts. The last run before
     force_close_et (15:50) does not hand over to the 15:55 run: if it holds
     bot positions it stays connected and starts the force close at exactly
     15:51:00.
  6. Post-close window (16:00-16:10 ET, runs at 16:00 and 16:05): before the
     pass, waits until IBKR confirms every DAY entry/exit dead
     (bot.await_day_orders_done), so the overnight stop is sized from the
     real exposure. The 16:05 run is a second protect-only pass.
  7. Disconnects and releases the pid file, always.

Uncaught errors are written to logs/cycle_errors.log, pushed as "Cycle
CRASHED" (notify.py), marked in logs/heartbeat.json, and exit with code 1.
Each pass also pushes fills of the bot's orders since the previous run
(bot.notify_fills), then refreshes logs/heartbeat.json and the dashboard.
Log lines go to logs/bot.log with ET timestamps, like bot.py.

Because every run is a fresh process, two strategy settings are adapted here:
the orphan-stop counter (a paperbot stop with no position is cancelled after
two passes in a row) is saved in logs/cycle_state.json between runs, and the
entry-scan budget is raised to 150 s so one run can scan the whole list.

Use either bot.py (continuous) or cycle.py (scheduled), not both; the pid
file makes sure only one runs at a time.
"""
import argparse
import datetime as dt
import json
import math
import os
import sys
import time
import traceback
from pathlib import Path
from zoneinfo import ZoneInfo

from paperbot import paths  # standard library only: keeps the off-hours exit fast

LOGS = paths.LOGS_DIR
ERROR_LOG = LOGS / "cycle_errors.log"
CYCLE_STATE = LOGS / "cycle_state.json"
ET = ZoneInfo("America/New_York")

SESSION_START = dt.time(9, 30)
SESSION_END = dt.time(16, 0)
RUN_UNTIL = dt.time(16, 10)  # 16:00 and 16:05 runs: post-close alerts / stop restores
RETRY_DELAY_SECONDS = 5
FORCE_LOOP_SECONDS = 30
SCHEDULE_SECONDS = 5 * 60  # Task Scheduler interval of HT_Cycle
SCAN_BUDGET_SECONDS = 150  # fits inside a 5-minute schedule; exits run first anyway
ORPHAN_STATE_MAX_AGE = dt.timedelta(minutes=15)  # older counts are from another day/session
TASK_NAME = "paperbot cycle"


# ---------------------------------------------------------------- time gate (stdlib only)

def _active_section() -> dict:
    """The active strategy's rules.json section, read with the standard library only
    (the saved choice in logs/active_strategy.json, else default_strategy)."""
    rules = json.loads(paths.RULES_FILE.read_text())
    try:
        key = json.loads((paths.LOGS_DIR / "active_strategy.json").read_text())["strategy"]
    except (OSError, ValueError, KeyError, TypeError):
        key = rules.get("default_strategy")
    return rules["strategies"][key]


def force_close_at() -> dt.time | None:
    """The active strategy's force_close_et; None for a strategy that holds overnight."""
    try:
        text = _active_section()["time_filter"]["force_close_et"]
        if text is None:
            return None
        hour, minute = map(int, text.split(":"))
        return dt.time(hour, minute)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return dt.time(15, 51)


def time_gate(now: dt.datetime, force_close: dt.time | None) -> str:
    """'weekend', 'closed', 'force_close', 'post_close' or 'run' for an ET datetime."""
    if now.weekday() >= 5:
        return "weekend"
    t = now.time()
    if t < SESSION_START or t >= RUN_UNTIL:
        return "closed"
    if force_close is not None and force_close <= t < SESSION_END:
        return "force_close"
    if t >= SESSION_END:
        return "post_close"
    return "run"


def seconds_to_force_close(now: dt.datetime, force_close: dt.time | None) -> float:
    """Seconds until force_close_et if the next scheduled run would start after it
    (so this run must start the force close itself), else 0 (also with no force close)."""
    if force_close is None:
        return 0.0
    target = now.replace(hour=force_close.hour, minute=force_close.minute, second=0,
                         microsecond=0)
    seconds = (target - now).total_seconds()
    return seconds if 0 < seconds < SCHEDULE_SECONDS else 0.0


def _ps(text) -> str:
    """PowerShell single-quoted literal."""
    return "'" + str(text).replace("'", "''") + "'"


def powershell_task_command() -> str:
    """Register-ScheduledTask block for PowerShell (no nested quoting through schtasks;
    Windows PowerShell 5.1 strips the \\" escapes a schtasks /TR string needs)."""
    python = Path(sys.executable).resolve()
    argument = "-m paperbot.runtime.cycle --submit"
    return "\n".join([
        f"$action = New-ScheduledTaskAction -Execute {_ps(python)} "
        f"-Argument {_ps(argument)} -WorkingDirectory {_ps(paths.PROJECT_ROOT)}",
        "$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) "
        "-RepetitionInterval (New-TimeSpan -Minutes 5)",
        "$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew "
        "-ExecutionTimeLimit (New-TimeSpan -Minutes 15) -StartWhenAvailable "
        "-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries",
        f"Register-ScheduledTask -TaskName {_ps(TASK_NAME)} -Action $action "
        "-Trigger $trigger -Settings $settings -Force",
    ])


def cmd_task_command() -> str:
    """The same task for cmd.exe. Paths are quoted only if they contain spaces."""
    def q(path) -> str:
        return f'\\"{path}\\"' if " " in str(path) else str(path)
    python = Path(sys.executable).resolve()
    return (f'schtasks /Create /TN "{TASK_NAME}" /SC MINUTE /MO 5 /F '
            f'/TR "{q(python)} -m paperbot.runtime.cycle --submit"')


# ---------------------------------------------------------------- orphan counter

def load_orphans(now: dt.datetime) -> dict[str, int]:
    """Orphan-stop counts saved by the previous run, if it was recent."""
    from paperbot.runtime.runstate import StateCorrupt, read_json_state
    try:
        saved = read_json_state(CYCLE_STATE) or {}
        when = dt.datetime.fromisoformat(saved["saved"])
        if now - when > ORPHAN_STATE_MAX_AGE:
            return {}
        return {str(k): int(v) for k, v in saved["orphans"].items()}
    except (StateCorrupt, KeyError, ValueError, TypeError, AttributeError):
        return {}  # non-critical: worst case an orphan stop is cancelled one run later


def save_orphans(orphans: dict[str, int], now: dt.datetime):
    from paperbot.runtime.runstate import write_json_atomic
    write_json_atomic(CYCLE_STATE, {"saved": now.isoformat(), "orphans": orphans})


# ---------------------------------------------------------------- one run

def connect_with_retry(ib, cfg: dict, log):
    """bot.connect, retried once after RETRY_DELAY_SECONDS; SystemExit(1) if both fail."""
    from paperbot.core import engine as bot
    from ib_async import StartupFetch, StartupFetchALL
    for attempt in (1, 2):
        try:
            bot.connect(ib, cfg, None,
                        fetchFields=StartupFetchALL & ~StartupFetch.EXECUTIONS)
            return
        except SystemExit:
            ib.disconnect()
            if attempt == 2:
                raise
            log.warning("retrying connection in %ss", RETRY_DELAY_SECONDS)
            time.sleep(RETRY_DELAY_SECONDS)


def one_pass(ib, guard, cfg: dict, account: str, pnl, submit: bool) -> int:
    """The body of bot.py's loop, once. Returns the number of requests handled."""
    from paperbot.core import engine as bot
    from paperbot.strategies import rule_strategy as strategy
    from paperbot.core.account import daily_pnl
    daily = daily_pnl(ib, account, pnl)  # 0.0 when provably flat today; None blocks entries
    was_halted = guard.halted
    guard.update_daily_pnl(daily)
    if guard.halted and not was_halted:
        bot.cancel_working(ib, guard.halt_reason)

    bot.notify_fills_safely(ib, account)  # BUY/STOP/PARTIAL/EXIT since the last run

    held_back: set[str] = set()
    requests = strategy.decide(ib, account)
    for request in requests:
        symbol = request.symbol.upper()
        if symbol in held_back:
            bot.log.warning("skipping %s: waiting for a cancel on %s", request, symbol)
            continue
        if not bot.handle_request(ib, guard, cfg, account, request, daily, submit):
            held_back.add(symbol)
    bot.after_pass(ib, account, "cycle.py", submit)  # heartbeat + dashboard; never raises
    return len(requests)


def sleep_until(ib, deadline: float) -> bool:
    """ib.sleep (keeps the connection's events flowing) until the monotonic deadline;
    False if a STOP file appears first."""
    from paperbot.runtime import runstate
    while (remaining := deadline - time.monotonic()) > 0:
        if runstate.stop_requested():
            return False
        ib.sleep(min(5.0, remaining))
    return True


def holds_bot_positions(ib, account: str) -> bool:
    from paperbot.strategies import rule_strategy as strategy
    owned = set(strategy.load_state()["positions"])
    return any(p.contract.symbol.upper() in owned and p.position > 0
               for p in ib.positions(account))


def run(gate: str, submit: bool) -> int:
    """Everything after the time gate. Heavy imports happen here, not at module load."""
    from paperbot.core import engine as bot
    from paperbot.core import orders, selector
    from paperbot.core import profile as profiles
    from paperbot.runtime import runstate
    from paperbot.strategies import rule_strategy as strategy
    from ib_async import IB
    from paperbot.core import broker
    from paperbot.core.risk import RiskGuard

    if not bot.log.handlers:
        bot.setup_logging()
    log = bot.log
    if runstate.stop_requested():
        log.info("cycle skipped: STOP file present")
        return 0
    pid = runstate.running_pid()
    if pid is not None:
        log.info("cycle skipped: paperbot already running (pid %s)", pid)
        return 0

    cfg = json.loads(paths.CONFIG_FILE.read_text())
    try:  # the saved strategy choice, validated before connecting
        profile = profiles.load(selector.unattended_key())
    except profiles.ConfigError as e:
        log.error("ALERT refusing to start: %s", e)
        raise SystemExit(f"Refusing to start: {e}") from None
    if "scheduled" not in profile.modes:
        log.error("ALERT %s runs in continuous mode only (paper-bot start); scheduled run "
                  "refused", profile.label)
        raise SystemExit(f"{profile.key} does not support scheduled runs")
    strategy.configure(profile)
    guard = RiskGuard(profile.limits, bot.RISK_STATE,
                      drawdown_path=bot.DRAWDOWN_STATE)  # fails closed
    strategy.load_state()  # fails closed before connecting
    bot.preflight(bot.connection_settings(cfg), profile, guard, cfg)
    runstate.claim_pid()
    ib = IB()
    try:
        connect_with_retry(ib, cfg, log)
        account = broker.verify_account(bot.connection_settings(cfg), ib.managedAccounts())
        log.info("cycle start (%s, %s) on paper account %s", gate,
                 "SUBMIT" if submit else "DRY RUN", account)
        pnl = ib.reqPnL(account)
        for _ in range(6):  # up to 3 s for the first daily P&L update
            if pnl.dailyPnL is not None and not math.isnan(pnl.dailyPnL):
                break
            ib.sleep(0.5)
        ib.reqAllOpenOrders()
        ib.orderStatusEvent += lambda t: bot.journal(
            "status", orderId=t.order.orderId, status=t.orderStatus.status,
            filled=t.orderStatus.filled)
        ib.execDetailsEvent += lambda t, fill: bot.journal(
            "fill", orderId=fill.execution.orderId, symbol=fill.contract.symbol,
            side=fill.execution.side, shares=fill.execution.shares,
            price=fill.execution.price)

        strategy.SCAN_BUDGET_SECONDS = SCAN_BUDGET_SECONDS
        orders._orphans.clear()
        orders._orphans.update(load_orphans(dt.datetime.now(ET)))

        if gate == "post_close":
            bot.await_day_orders_done(ib, submit)  # ALERTs itself if not confirmed
        handled = one_pass(ib, guard, cfg, account, pnl, submit)
        if gate == "run" and submit:
            wait = seconds_to_force_close(dt.datetime.now(ET), force_close_at())
            if wait and holds_bot_positions(ib, account):
                log.info("staying connected %.0f s to start the force close at %s ET",
                         wait, force_close_at().strftime("%H:%M"))
                if sleep_until(ib, time.monotonic() + wait):
                    gate = "force_close"
                    handled += one_pass(ib, guard, cfg, account, pnl, submit)
        # Force close: keep re-pricing every 30 s until flat or the close.
        while (gate == "force_close" and submit and not runstate.stop_requested()
               and dt.datetime.now(ET).time() < SESSION_END
               and holds_bot_positions(ib, account)):
            ib.sleep(FORCE_LOOP_SECONDS)
            handled += one_pass(ib, guard, cfg, account, pnl, submit)
        # Still holding at the close: this run blocks the 16:00 run (pid file), so it does
        # the post-close protect pass itself instead of leaving it to the 16:05 run.
        if (gate == "force_close" and submit and not runstate.stop_requested()
                and bot.post_close(dt.datetime.now(ET)) and holds_bot_positions(ib, account)):
            bot.await_day_orders_done(ib, submit)
            handled += one_pass(ib, guard, cfg, account, pnl, submit)

        save_orphans(dict(orders._orphans), dt.datetime.now(ET))
        if handled:
            ib.sleep(2)  # let IBKR acknowledge the last orders before disconnecting
        log.info("cycle done: %s request(s)", handled)
        return 0
    finally:
        ib.disconnect()
        bot.wait_for_dashboard()  # the last dashboard rebuild finishes before exit
        runstate.release_pid()


def record_error(text: str):
    LOGS.mkdir(exist_ok=True)
    stamp = dt.datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S ET")
    with open(ERROR_LOG, "a") as f:
        f.write(f"[{stamp}] {text.rstrip()}\n")


def main(argv: list[str] | None = None) -> int:
    if os.environ.get("PAPERBOT_BACKTEST") == "1":  # same check as runstate.refuse_if_backtest
        print("paperbot-cycle refused: this is a backtesting process (PAPERBOT_BACKTEST=1)")
        return 1
    parser = argparse.ArgumentParser(description="One scheduled pass of paperbot.")
    parser.add_argument("--submit", action="store_true",
                        help="send orders to the paper account (default: dry run)")
    parser.add_argument("--print-task", action="store_true",
                        help="print the Task Scheduler command and exit (installs nothing)")
    args = parser.parse_args(argv)

    if args.print_task:
        print("Schedule paperbot every 5 minutes. Run ONE of these yourself.\n")
        print("PowerShell (paste all four lines):\n")
        print(powershell_task_command())
        print("\ncmd.exe (Command Prompt, not PowerShell):\n")
        print(cmd_task_command())
        print(f"\nCheck it:   Get-ScheduledTask -TaskName {_ps(TASK_NAME)}"
              f"   (cmd: schtasks /Query /TN \"{TASK_NAME}\")")
        print(f"Remove it:  Unregister-ScheduledTask -TaskName {_ps(TASK_NAME)} -Confirm:$false"
              f"   (cmd: schtasks /Delete /TN \"{TASK_NAME}\" /F)")
        return 0

    gate = time_gate(dt.datetime.now(ET), force_close_at())
    if gate in ("weekend", "closed"):
        return 0  # fast exit: nothing imported, nothing connected

    try:
        return run(gate, args.submit)
    except SystemExit as e:  # deliberate stops: corrupt state, not paper, no connection
        if e.code in (0, None):
            return 0
        record_error(f"exit: {e.code}")
        return e.code if isinstance(e.code, int) else 1
    except Exception as exc:  # noqa: BLE001 - scheduled runs have no console; keep the traceback
        record_error(traceback.format_exc())
        try:
            from paperbot.reporting.notify import notify
            notify("Cycle CRASHED", f"{type(exc).__name__}: {exc}"[:500], "high")
            from paperbot.runtime import runstate
            runstate.mark_heartbeat("crashed", f"{type(exc).__name__}: {exc}")
        except Exception:  # noqa: BLE001 - never let a notification hide the crash
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
