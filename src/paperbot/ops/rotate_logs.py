"""Log rotation and ledger trimming for paperbot. Safe at any hour; never connects to IBKR.

    paperbot-rotate      (same as python -m paperbot.ops.rotate_logs)

Best run once a day BEFORE the US session (e.g. 08:00 ET), when nothing has
been written yet today. Steps:
  1. logs/safety_log.jsonl over 5 MB: moved to logs/archive/YYYY-MM-DD_safety_log.jsonl
     and replaced by a fresh empty file.
  2. Every logs/*.log and logs/*.jsonl last modified on an earlier ET date
     moves to logs/archive/<that date>/. Files written today stay.
  3. data/trades.csv keeps the last 90 calendar days; older rows are appended
     to logs/archive/trades_YYYYMMDD.csv and trades.csv is rewritten atomically.
     Skipped while bot.py or a cycle.py run is active (logs/bot.pid), so a
     fill can never be appended mid-rewrite.

All moves use os.replace on resolved paths inside logs/ (same drive, atomic;
no copy-then-delete). A file another process holds open (Windows refuses to
move it) is skipped and reported, never forced. State files (*.json), the pid
file and the archive itself are never touched. Always exits 0.
"""
import csv
import datetime as dt
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from paperbot import paths
from paperbot.runtime import runstate

LOGS = paths.LOGS_DIR
ARCHIVE = paths.ARCHIVE_DIR
TRADES_CSV = paths.TRADES_CSV
SAFETY_LOG = LOGS / "safety_log.jsonl"
SAFETY_MAX_BYTES = 5_000_000
KEEP_DAYS = 90
ROTATE_SUFFIXES = (".log", ".jsonl")
ET = ZoneInfo("America/New_York")


def _free_name(path: Path) -> Path:
    """path, or path with -1, -2, ... if a file of that name was already archived."""
    candidate, n = path, 0
    while candidate.exists():
        n += 1
        candidate = path.with_name(f"{path.stem}-{n}{path.suffix}")
    return candidate


def _move(src: Path, dest: Path, archive: Path, report: list[str]) -> bool:
    """Atomic move into the archive folder; a locked file is skipped and reported."""
    src, dest = src.resolve(), dest.resolve()
    if not dest.is_relative_to(archive.resolve()):
        report.append(f"refused {src.name}: destination outside {archive.name}/")
        return False
    os.makedirs(dest.parent, exist_ok=True)
    try:
        os.replace(src, _free_name(dest))
        return True
    except PermissionError:
        report.append(f"skipped {src.name}: in use by another process")
    except OSError as e:
        report.append(f"skipped {src.name}: {e}")
    return False


def rotate_safety_log(path: Path, archive: Path, today: dt.date, report: list[str]) -> int:
    """Rule 1: archive an oversized safety log and start a fresh empty one."""
    if not path.exists() or path.stat().st_size <= SAFETY_MAX_BYTES:
        return 0
    if not _move(path, archive / f"{today.isoformat()}_{path.name}", archive, report):
        return 0
    path.touch()
    report.append(f"{path.name} over {SAFETY_MAX_BYTES:,} bytes: archived, fresh file started")
    return 1


def rotate_dated(logs: Path, archive: Path, today: dt.date, report: list[str]) -> int:
    """Rule 2: move logs not written today into archive/<their last-write date>/."""
    moved = 0
    for path in sorted(logs.iterdir()):
        if not path.is_file() or path.suffix not in ROTATE_SUFFIXES:
            continue
        written = dt.datetime.fromtimestamp(path.stat().st_mtime, ET).date()
        if written >= today:
            continue
        if _move(path, archive / written.isoformat() / path.name, archive, report):
            moved += 1
    return moved


def trim_trades(trades: Path, archive: Path, today: dt.date, report: list[str]) -> int:
    """Rule 3: keep the last KEEP_DAYS days in trades.csv, archive older rows."""
    if not trades.exists() or trades.stat().st_size == 0:
        return 0
    pid = runstate.running_pid()
    if pid is not None and pid != os.getpid():  # the running bot itself may trim (same thread)
        report.append("trades.csv not trimmed: bot or cycle run active")
        return 0
    cutoff = today - dt.timedelta(days=KEEP_DAYS)
    with open(trades, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fields = reader.fieldnames or []
        rows = list(reader)
    old, keep = [], []
    for row in rows:
        try:
            day = dt.date.fromisoformat((row.get("timestamp_iso") or "")[:10])
        except ValueError:
            day = None  # unparseable dates are kept, never archived
        (old if day is not None and day < cutoff else keep).append(row)
    if not old:
        return 0

    target = archive / f"trades_{today:%Y%m%d}.csv"
    os.makedirs(archive, exist_ok=True)
    new_archive = not target.exists()
    with open(target, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        if new_archive:
            writer.writeheader()
        writer.writerows(old)
    tmp = trades.with_name(trades.name + ".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(keep)
    try:
        os.replace(tmp, trades)
    except PermissionError:
        tmp.unlink(missing_ok=True)
        report.append("trades.csv not trimmed: in use by another process "
                      f"(its {len(old)} old rows were also appended to {target.name})")
        return 0
    report.append(f"trades.csv: {len(old)} rows before {cutoff} archived to {target.name}")
    return 1


def main() -> int:
    today = dt.datetime.now(ET).date()
    report: list[str] = []
    rotated = 0
    if LOGS.is_dir():
        rotated += rotate_safety_log(SAFETY_LOG, ARCHIVE, today, report)
        rotated += rotate_dated(LOGS, ARCHIVE, today, report)
    rotated += trim_trades(TRADES_CSV, ARCHIVE, today, report)
    for line in report:
        print(f"  {line}")
    print(f"Rotated {rotated} files to logs/archive/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
