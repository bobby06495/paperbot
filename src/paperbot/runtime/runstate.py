"""Run-state files shared by the bot and its stop tools.

STOP              Asks a running bot to shut down on its next loop. While it
                  exists the bot refuses to start; delete it to restart.
logs/bot.pid      Written by a running bot and removed when it exits, so the
                  stop tools can tell whether the bot is running without
                  matching process names or killing anything.
State files       logs/state.json (risk guard) and logs/strategy_state.json
                  are written atomically and read "fail closed": a corrupt
                  file stops the bot instead of silently resetting today's
                  safeguards. `paperbot-bot --reset-state` is the explicit
                  recovery step.

logs/heartbeat.json
                  Written after every bot.py loop / cycle.py pass: time (ET),
                  source, mode, status and open positions. Read by the
                  dashboard (compute_perf.py) to show whether the bot is alive.

This module only touches local files; it never connects to IBKR.
"""
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from paperbot import paths

STOP_FILE = paths.STOP_FILE
PID_FILE = paths.LOGS_DIR / "bot.pid"
HEARTBEAT_FILE = paths.LOGS_DIR / "heartbeat.json"


# ---------------------------------------------------------------- STOP file

BACKTEST_ENV = "PAPERBOT_BACKTEST"  # set to "1" by every backtesting process


class LiveTradingBlocked(RuntimeError):
    """A backtesting process tried to reach live order code."""


def refuse_if_backtest(what: str):
    """Fail closed: live order code must never run inside a backtesting process."""
    if os.environ.get(BACKTEST_ENV) == "1":
        raise LiveTradingBlocked(f"{what} refused: this is a backtesting process "
                                 f"({BACKTEST_ENV}=1); backtests never send orders")


def stop_requested() -> bool:
    return STOP_FILE.exists()


def request_stop():
    STOP_FILE.write_text("stop\n")


# ---------------------------------------------------------------- pid file

def pid_alive(pid: int) -> bool:
    """True if a process with this PID exists. Never signals the process.

    On Windows os.kill(pid, 0) would terminate the process, so the Win32 API
    is queried instead.
    """
    from paperbot.system.procs import pid_alive as os_pid_alive  # the per-OS check
    return os_pid_alive(pid)


def running_pid() -> int | None:
    """PID of the running bot, or None. A stale pid file counts as not running."""
    try:
        pid = int(PID_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None
    return pid if pid_alive(pid) else None


def claim_pid():
    """Record this process as the running bot; refuse if another bot is running."""
    pid = running_pid()
    if pid is not None and pid != os.getpid():
        raise SystemExit(f"paperbot is already running (pid {pid}); stop it with paperbot-stop first")
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))


def release_pid():
    """Remove the pid file if it is ours."""
    try:
        if int(PID_FILE.read_text().strip()) == os.getpid():
            PID_FILE.unlink()
    except (FileNotFoundError, ValueError):
        pass


# ---------------------------------------------------------------- state files

class StateCorrupt(SystemExit):
    """Raised (and exits the bot) when a state file cannot be trusted."""

    def __init__(self, path: Path, why):
        super().__init__(
            f"{path} is unreadable ({why}). Refusing to run with unknown risk state. "
            "Inspect the file, then run: paperbot-bot --reset-state")
        self.path = path


def read_json_state(path: Path) -> dict | None:
    """Return the saved state, or None if the file does not exist yet.

    Anything else that is not a JSON object raises StateCorrupt (fail closed).
    """
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise StateCorrupt(path, e) from None
    if not isinstance(data, dict):
        raise StateCorrupt(path, "not a JSON object")
    return data


def write_json_atomic(path: Path, data: dict):
    """Write JSON so a crash mid-write leaves either the old or the new file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def write_heartbeat(data: dict):
    write_json_atomic(HEARTBEAT_FILE, data)


def mark_heartbeat(status: str, error: str = ""):
    """Record e.g. a crash in the heartbeat, keeping the last known positions."""
    try:
        data = read_json_state(HEARTBEAT_FILE) or {}
    except StateCorrupt:
        data = {}
    data.update(status=status, error=str(error)[:500],
                time=dt.datetime.now(ZoneInfo("America/New_York")).isoformat(timespec="seconds"))
    write_json_atomic(HEARTBEAT_FILE, data)


def reset_corrupt(paths) -> list[Path]:
    """Move each corrupt state file aside (kept as a backup). Returns the backups."""
    backups = []
    for path in paths:
        try:
            read_json_state(path)
        except StateCorrupt:
            backup = path.with_name(f"{path.name}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}")
            os.replace(path, backup)
            backups.append(backup)
    return backups
