"""Run folders, metadata, the run lock, dataset-usage tracking and retention.

Every run gets its own folder, backtesting/results/<strategy_id>/<run_id>/, and
nothing ever writes into another run's folder: a later run cannot overwrite an
earlier one. Old runs are deleted only by an explicit `paperbot-backtest prune`.
"""
import csv
import datetime as dt
import hashlib
import json
import os
import platform
import shutil
import subprocess
import uuid
from pathlib import Path

from paperbot import paths
from paperbot.runtime.runstate import pid_alive

UTC = dt.timezone.utc
ENGINE_VERSION = "1.0.0"
CSV_FILES = ("trades", "orders", "fills", "equity_curve", "daily_performance", "monthly_performance")


class RunLocked(RuntimeError):
    pass


def new_run_id(now: dt.datetime | None = None) -> str:
    now = now or dt.datetime.now(UTC)
    return f"{now:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"


def rules_version(rules: dict) -> str:
    clean = {k: v for k, v in rules.items() if not k.startswith("_")}
    return hashlib.sha256(json.dumps(clean, sort_keys=True).encode()).hexdigest()[:12]


def git_info(root: Path | None = None) -> dict:
    root = root or paths.PROJECT_ROOT

    def git(*args) -> str | None:
        try:
            out = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return None
        return out.stdout.strip() if out.returncode == 0 else None

    commit = git("rev-parse", "HEAD")
    status = git("status", "--porcelain")
    return {"commit": commit, "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
            "dirty": bool(status) if status is not None else None}


def _paperbot_version() -> str:
    try:
        from importlib.metadata import version
        return version("paperbot")
    except Exception:  # noqa: BLE001 - not installed (e.g. run from a checkout)
        return "unknown"


def software_info() -> dict:
    return {"paperbot": _paperbot_version(), "backtest_engine": ENGINE_VERSION,
            "python": platform.python_version()}


def strategy_id(rules: dict) -> str:
    return "".join(c if c.isalnum() else "_" for c in rules["strategy_name"].lower()).strip("_")


# ---------------------------------------------------------------- writing

def write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, path)


def write_csv(path: Path, rows: list[dict], fields: list[str] | None = None):
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = fields or (list(rows[0].keys()) if rows else ["empty"])
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def read_csv(path: Path) -> list[dict]:
    try:
        with open(path, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except OSError:
        return []


# ---------------------------------------------------------------- index

def list_runs(results: Path | None = None) -> list[dict]:
    """Every run's metadata, newest first."""
    results = results or paths.BACKTEST_RESULTS_DIR
    runs = []
    for meta in results.glob("*/*/run_metadata.json"):
        data = read_json(meta)
        if isinstance(data, dict) and data.get("run_id"):
            data["_dir"] = str(meta.parent)
            runs.append(data)
    return sorted(runs, key=lambda r: r.get("started_at", ""), reverse=True)


def find_run(run_id: str, results: Path | None = None) -> Path | None:
    results = results or paths.BACKTEST_RESULTS_DIR
    matches = [p for p in results.glob(f"*/{run_id}*") if (p / "run_metadata.json").exists()]
    return matches[0] if len(matches) == 1 else None


def load_run(run_dir: Path) -> dict:
    out = {"metadata": read_json(run_dir / "run_metadata.json", {}),
           "metrics": read_json(run_dir / "metrics.json", {}),
           "validation": read_json(run_dir / "validation.json", {}),
           "config": read_json(run_dir / "config_snapshot.json", {})}
    for name in CSV_FILES:
        out[name] = read_csv(run_dir / f"{name}.csv")
    out["trade_context"] = read_json(run_dir / "trade_context.json", {})
    return out


# ---------------------------------------------------------------- lock

class RunLock:
    """One backtest process at a time (manual or scheduled). A lock left by a dead
    process is taken over."""

    def __init__(self, results: Path | None = None):
        self.path = (results or paths.BACKTEST_RESULTS_DIR) / ".backtest.lock"

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = read_json(self.path)
        if isinstance(data, dict) and data.get("pid") and data["pid"] != os.getpid() \
                and pid_alive(int(data["pid"])):
            raise RunLocked(f"another backtest is running (pid {data['pid']}, since "
                            f"{data.get('started')}); wait for it to finish")
        write_json(self.path, {"pid": os.getpid(),
                               "started": dt.datetime.now(UTC).isoformat(timespec="seconds")})
        return self

    def __exit__(self, *exc):
        try:
            if (read_json(self.path) or {}).get("pid") == os.getpid():
                self.path.unlink()
        except OSError:
            pass
        return False


# ---------------------------------------------------------------- dataset usage

def record_usage(dataset_id: str, run_id: str, experiment_id: str | None,
                 results: Path | None = None) -> dict:
    """How many runs / distinct rule variants have been evaluated on one dataset
    (the more variants tried, the more a best result may be luck)."""
    path = (results or paths.BACKTEST_RESULTS_DIR) / "dataset_usage.json"
    usage = read_json(path, {}) or {}
    entry = usage.setdefault(dataset_id, {"runs": [], "variants": [], "first_used": None})
    entry["first_used"] = entry["first_used"] or dt.datetime.now(UTC).isoformat(timespec="seconds")
    entry["runs"].append(run_id)
    variant = experiment_id or "baseline"
    if variant not in entry["variants"]:
        entry["variants"].append(variant)
    write_json(path, usage)
    return entry


def usage(results: Path | None = None) -> dict:
    return read_json((results or paths.BACKTEST_RESULTS_DIR) / "dataset_usage.json", {}) or {}


# ---------------------------------------------------------------- retention

def prune(keep: int, results: Path | None = None, dry_run: bool = False) -> list[str]:
    """Delete all but the newest `keep` runs per strategy. Explicit only: never called
    automatically. Pinned runs (metadata "pinned": true) are always kept."""
    removed = []
    by_strategy: dict[str, list] = {}
    for run in list_runs(results):
        by_strategy.setdefault(run.get("strategy_id", "?"), []).append(run)
    for runs in by_strategy.values():
        for run in runs[keep:]:
            if run.get("pinned"):
                continue
            removed.append(run["run_id"])
            if not dry_run:
                shutil.rmtree(run["_dir"], ignore_errors=True)
    return removed
