"""Register paperbot's Windows Task Scheduler tasks (your user account, no admin).

    paperbot-schedule                   register / update all HT_* tasks, keep PC awake on AC
    paperbot-schedule --if-dst-changed  (run daily by HT_Reschedule) re-register only if a
                                        task's registered start time or weekdays differ
                                        from today's US Eastern conversion (falls back to
                                        comparing saved UTC offsets if schtasks can't be read)
    (same as python -m paperbot.ops.setup_schedule)

Tasks run `<venv python> -m <module>`, so they need the editable install
(pip install -e .). Times are US Eastern, converted to local time AND weekday:
  HT_LogRotate          09:25 ET Mon-Fri        paperbot.ops.rotate_logs
  HT_Prefilter_01..07   09:55, 10:25 ... 12:55 ET Mon-Fri   paperbot.market.morning_prefilter
  HT_Cycle              every 5 min 09:30-16:10 ET Mon-Fri   paperbot.runtime.cycle --submit
                        (a weekly trigger repeating every 5 min for 6 h 40 min; the
                        cycle's own gate checks the US market calendar, so holiday
                        runs and the runs after an early close exit in < 1 s)
  HT_Dashboard          16:05 ET Mon-Fri        paperbot.reporting.compute_perf
  HT_Reschedule         07:00 ET daily          paperbot.ops.setup_schedule --if-dst-changed

Local weekday shift: 16:05 ET Monday is 04:05 Tuesday in Singapore, so
HT_Dashboard runs TUE-SAT there. Daylight saving: New York switches twice a
year (Singapore never), which moves every ET time by an hour locally;
HT_Reschedule compares the offsets saved in logs/schedule_state.json each
day and re-registers the tasks when they change.

Keep-awake: the first run saves your current sleep timeouts to
logs/power_backup.json, then sets sleep-on-AC to never (powercfg /change
standby-timeout-ac 0; battery settings are untouched). Later runs never
overwrite that backup; cleanup_schedule.py restores it.

Also deletes the older single "paperbot cycle" task, replaced by HT_Cycle.
Safe to re-run (/F overwrites). Never uses /RL HIGHEST or /RU SYSTEM: tasks
run as you, only while you are logged on. A failed task is reported and the
rest are still registered.
"""
import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from paperbot import paths

PROJECT_DIR = paths.PROJECT_ROOT
VENV_PY = paths.VENV_PYTHON
ET = ZoneInfo("America/New_York")
LOGS = paths.LOGS_DIR
STATE_FILE = LOGS / "schedule_state.json"
POWER_BACKUP = LOGS / "power_backup.json"
OLD_TASK = "paperbot cycle"
WEEKDAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
PREFILTER_TIMES = ("09:55", "10:25", "10:55", "11:25", "11:55", "12:25", "12:55")

WEEKLY_TASKS = [  # (name, ET time, module + args), Mon-Fri in ET
    ("HT_LogRotate", "09:25", ["paperbot.ops.rotate_logs"]),
    *[(f"HT_Prefilter_{i:02d}", t, ["paperbot.market.morning_prefilter"])
      for i, t in enumerate(PREFILTER_TIMES, 1)],
    ("HT_Dashboard", "16:05", ["paperbot.reporting.compute_perf"]),
]
CYCLE_TASK = ("HT_Cycle", "09:30", ["paperbot.runtime.cycle", "--submit"])
CYCLE_EVERY_MINUTES = 5
CYCLE_DURATION = "06:40"  # 09:30 -> 16:10 ET: the session plus the post-close runs
RESCHEDULE_TASK = ("HT_Reschedule", "07:00", ["paperbot.ops.setup_schedule", "--if-dst-changed"])
TASK_NAMES = [t[0] for t in WEEKLY_TASKS] + [CYCLE_TASK[0], RESCHEDULE_TASK[0]]


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=False, capture_output=True, text=True, errors="replace")


def to_local(moment: dt.datetime) -> dt.datetime:
    """This PC's local time for an aware datetime (honours the PC's own DST rules)."""
    return moment.astimezone()


def local_slot(et_hhmm: str, today: dt.date, convert=to_local,
               et_weekdays=range(5)) -> tuple[str, list[str]]:
    """Local 'HH:MM' and local weekday codes for an ET time on the given ET weekdays
    (0 = Monday), using each weekday's next date from today on. New York changes its
    clocks on a Sunday, so the coming Mon-Fri always share the new offset (the
    current week's Monday would still be on the old one)."""
    hour, minute = map(int, et_hhmm.split(":"))
    times, days = [], set()
    for weekday in et_weekdays:
        day = today + dt.timedelta(days=(weekday - today.weekday()) % 7)
        local = convert(dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=ET))
        times.append(local.strftime("%H:%M"))
        days.add(local.weekday())
    return times[0], [WEEKDAYS[d] for d in sorted(days)]


def task_run(args: list[str]) -> str:
    """The /TR command: venv python -m module (+ args). Quoted only if the path has spaces."""
    def q(path) -> str:
        return f'"{path}"' if " " in str(path) else str(path)
    return " ".join([q(VENV_PY), "-m", *args])


def build_commands(today: dt.date, convert=to_local) -> list[tuple[str, list[str], str]]:
    """(task name, schtasks command, human-readable schedule) for every task."""
    commands = []
    for name, et_time, args in WEEKLY_TASKS:
        start, days = local_slot(et_time, today, convert)
        commands.append((name, ["schtasks", "/Create", "/TN", name, "/TR", task_run(args),
                                "/SC", "WEEKLY", "/D", ",".join(days), "/ST", start, "/F"],
                         f"{et_time} ET Mon-Fri = {start} {','.join(days)} local"))
    name, et_time, args = CYCLE_TASK
    start, days = local_slot(et_time, today, convert)
    commands.append((name, ["schtasks", "/Create", "/TN", name, "/TR", task_run(args),
                            "/SC", "WEEKLY", "/D", ",".join(days), "/ST", start,
                            "/RI", str(CYCLE_EVERY_MINUTES), "/DU", CYCLE_DURATION, "/F"],
                     f"every {CYCLE_EVERY_MINUTES} min {et_time}-16:10 ET Mon-Fri = from "
                     f"{start} {','.join(days)} local for {CYCLE_DURATION} "
                     "(cycle.py gates on the market calendar)"))
    name, et_time, args = RESCHEDULE_TASK
    start, _ = local_slot(et_time, today, convert, et_weekdays=[today.weekday()])
    commands.append((name, ["schtasks", "/Create", "/TN", name, "/TR", task_run(args),
                            "/SC", "DAILY", "/ST", start, "/F"],
                     f"{et_time} ET daily = {start} local"))
    return commands


def utc_offsets(now: dt.datetime, convert=to_local) -> dict:
    """Minutes east of UTC for New York and for this PC, at `now`."""
    return {"et": int(now.astimezone(ET).utcoffset().total_seconds() // 60),
            "local": int(convert(now).utcoffset().total_seconds() // 60)}


XML_DAYS = {"Monday": "MON", "Tuesday": "TUE", "Wednesday": "WED", "Thursday": "THU",
            "Friday": "FRI", "Saturday": "SAT", "Sunday": "SUN"}


def registered_slot(name: str, runner=run) -> tuple[str, frozenset | None] | None:
    """('HH:MM', weekday codes or None for a daily task) as Task Scheduler holds it;
    ('', None) if the task is not registered; None if the output cannot be read."""
    result = runner(["schtasks", "/Query", "/TN", name, "/XML"])
    if result.returncode != 0:
        return "", None
    start = re.search(r"<StartBoundary>[^<]*T(\d\d:\d\d)", result.stdout or "")
    if not start:
        return None
    week = re.search(r"<DaysOfWeek>(.*?)</DaysOfWeek>", result.stdout, re.DOTALL)
    days = frozenset(XML_DAYS[d] for d in re.findall(r"<(\w+)\s*/>", week.group(1))
                     if d in XML_DAYS) if week else None
    return start.group(1), days


def schedule_mismatches(today: dt.date, runner=run, convert=to_local) -> list[str] | None:
    """Tasks whose registered start time or weekdays differ from what they should be
    today; None if Task Scheduler's answer cannot be read."""
    wrong = []
    for name, cmd, _ in build_commands(today, convert):
        expected_days = frozenset(cmd[cmd.index("/D") + 1].split(",")) if "/D" in cmd else None
        expected = (cmd[cmd.index("/ST") + 1], expected_days)
        actual = registered_slot(name, runner)
        if actual is None:
            return None
        if actual != expected:
            wrong.append(name)
    return wrong


def register_all(today: dt.date, runner=run, convert=to_local) -> tuple[int, list[str]]:
    """Create/overwrite every task. Returns (created, failure messages)."""
    removed = runner(["schtasks", "/Delete", "/TN", OLD_TASK, "/F"])
    if removed.returncode == 0:
        print(f'removed the old "{OLD_TASK}" task (replaced by HT_Cycle)')
    created, failures = 0, []
    for name, cmd, schedule in build_commands(today, convert):
        result = runner(cmd)
        if result.returncode == 0:
            created += 1
            print(f"ok    {name:<16} {schedule}")
        else:
            failures.append(f"{name}: {(result.stderr or result.stdout).strip()}")
            print(f"FAIL  {name:<16} {(result.stderr or result.stdout).strip()}")
    return created, failures


def read_standby(runner=run) -> dict | None:
    """Current sleep-after timeouts in seconds, from powercfg (last two hex values: AC, DC)."""
    result = runner(["powercfg", "/query", "SCHEME_CURRENT", "SUB_SLEEP", "STANDBYIDLE"])
    values = re.findall(r"0x([0-9a-fA-F]+)\s*$", result.stdout or "", re.MULTILINE)
    if result.returncode != 0 or len(values) < 2:
        return None
    return {"ac_seconds": int(values[-2], 16), "dc_seconds": int(values[-1], 16)}


def keep_awake(runner=run) -> str:
    """Back up the sleep timeouts once, then never sleep while on AC power."""
    if not POWER_BACKUP.exists():
        backup = read_standby(runner)
        if backup is None:
            return "keep-awake skipped: could not read the current sleep settings"
        LOGS.mkdir(exist_ok=True)
        POWER_BACKUP.write_text(json.dumps(backup, indent=2))
    result = runner(["powercfg", "/change", "standby-timeout-ac", "0"])
    if result.returncode != 0:
        return f"keep-awake failed: {(result.stderr or result.stdout).strip()}"
    return "keep-awake on: sleep on AC set to never (saved settings in logs/power_backup.json)"


def main(argv: list[str] | None = None, runner=run, convert=to_local) -> int:
    parser = argparse.ArgumentParser(description="Register paperbot's scheduled tasks.")
    parser.add_argument("--if-dst-changed", action="store_true",
                        help="re-register only if a UTC offset changed since the last setup")
    args = parser.parse_args(argv)
    if not VENV_PY.exists():
        print(f"STOP: {VENV_PY} not found. Create the venv first.")
        return 1

    now = dt.datetime.now(dt.timezone.utc)
    offsets = utc_offsets(now, convert)
    today = now.astimezone(ET).date()
    if args.if_dst_changed:
        # Compare with what Task Scheduler actually holds, so a wrong or skipped
        # re-registration is repaired on the next daily run.
        wrong = schedule_mismatches(today, runner, convert)
        if wrong == []:
            LOGS.mkdir(exist_ok=True)
            STATE_FILE.write_text(json.dumps(offsets))
            print("Task start times match US Eastern; schedule kept.")
            return 0
        if wrong:
            print(f"Start times off for {', '.join(wrong)}; re-registering tasks.")
        else:  # Task Scheduler unreadable: fall back to the saved UTC offsets
            try:
                saved = json.loads(STATE_FILE.read_text())
            except (OSError, ValueError):
                saved = None
            if saved == offsets:
                print("UTC offsets unchanged; schedule kept.")
                return 0
            print(f"UTC offsets changed ({saved} -> {offsets}); re-registering tasks.")

    created, failures = register_all(today, runner, convert)
    LOGS.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(offsets))
    if not args.if_dst_changed:
        print(keep_awake(runner))

    print()
    for name in TASK_NAMES:
        table = runner(["schtasks", "/Query", "/TN", name, "/FO", "TABLE", "/NH"])
        if table.returncode == 0 and table.stdout.strip():
            print(table.stdout.strip())
    print()
    total = len(TASK_NAMES)
    if failures:
        print(f"SCHEDULED: {created} of {total} tasks created; {len(failures)} failed (see above).")
        return 1
    print(f"SCHEDULED: {total} tasks created. The bot will fire on its own starting next "
          "market open. Watch Telegram.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
