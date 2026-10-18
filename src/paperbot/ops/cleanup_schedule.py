"""Remove every HT_* task created by setup_schedule.py and restore sleep settings.

    paperbot-unschedule      (same as python -m paperbot.ops.cleanup_schedule)

Lists all tasks (schtasks /query /fo CSV /nh), deletes each top-level task
whose name starts with HT_, then restores sleep-on-AC to the value saved in
logs/power_backup.json by setup_schedule.py (30 minutes if there is no
backup). Running bot processes are not touched; a cycle.py pass that is
already running finishes normally. Safe to re-run: with nothing left it
prints "Nothing to clean." and exits 0.
"""
import csv
import io
import json
import subprocess
import sys
from pathlib import Path

from paperbot import paths

LOGS = paths.LOGS_DIR
POWER_BACKUP = LOGS / "power_backup.json"
STATE_FILE = LOGS / "schedule_state.json"
DEFAULT_AC_MINUTES = 30


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=False, capture_output=True, text=True, errors="replace")


def ht_tasks(runner=run) -> list[str]:
    """Names of top-level scheduled tasks starting with HT_."""
    result = runner(["schtasks", "/Query", "/FO", "CSV", "/NH"])
    names: list[str] = []
    for row in csv.reader(io.StringIO(result.stdout or "")):
        if not row:
            continue
        name = row[0].strip()
        if name.startswith("\\"):
            name = name[1:]
        if name.startswith("HT_") and "\\" not in name and name not in names:
            names.append(name)
    return names


def restore_power(runner=run) -> str:
    """Put sleep-on-AC back to the saved value (or the Windows default of 30 min)."""
    minutes, source = DEFAULT_AC_MINUTES, "Windows default"
    try:
        backup = json.loads(POWER_BACKUP.read_text())
        minutes, source = max(0, round(backup["ac_seconds"] / 60)), "saved setting"
    except (OSError, ValueError, KeyError, TypeError):
        pass
    result = runner(["powercfg", "/change", "standby-timeout-ac", str(minutes)])
    if result.returncode != 0:
        return f"power restore failed: {(result.stderr or result.stdout).strip()}"
    POWER_BACKUP.unlink(missing_ok=True)
    return f"sleep on AC restored to {minutes} min ({source})"


def main(runner=run) -> int:
    names = ht_tasks(runner)
    if not names and not POWER_BACKUP.exists():
        print("Nothing to clean.")
        return 0

    deleted = 0
    for name in names:
        result = runner(["schtasks", "/Delete", "/TN", name, "/F"])
        if result.returncode == 0:
            deleted += 1
            print(f"deleted {name}")
        else:
            print(f"FAIL    {name}: {(result.stderr or result.stdout).strip()}")
    print(restore_power(runner))
    STATE_FILE.unlink(missing_ok=True)
    print(f"CLEANUP DONE: {deleted} tasks deleted; default power settings restored.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
