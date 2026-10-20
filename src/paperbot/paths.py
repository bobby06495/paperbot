"""Every file and folder paperbot reads or writes, resolved once from the project root.

The project root is the folder holding config/, src/ and pyproject.toml. It is
found from this file's location (src/paperbot/paths.py, editable install) or
taken from the PAPERBOT_HOME environment variable when set. All paths are
absolute and .resolve()d, so they work from any working directory, on any OS,
and from a service or container.

Trading mode (config.json "mode": "paper" or "live", read once at import):
paper keeps state, logs, the fill ledger and the dashboard in logs/, data/ and
dashboard/ (unchanged); live keeps its own copies in logs/live/, data/live/
and dashboard/live/, so paper history can never feed live risk checks. The
watchlist (market data, not account data) is shared.

Imports only the standard library, so it stays cheap to import (the scheduled
pass's off-hours exit relies on that).
"""
import json
import os
import sys
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = Path(os.environ.get("PAPERBOT_HOME") or PACKAGE_DIR.parent.parent).resolve()

# configuration (tracked in git)
CONFIG_DIR = PROJECT_ROOT / "config"
CONFIG_FILE = CONFIG_DIR / "config.json"
RULES_FILE = CONFIG_DIR / "rules.json"
UNIVERSE_FILE = CONFIG_DIR / "universe.txt"
ENV_FILE = PROJECT_ROOT / ".env"  # secrets, never tracked


def _mode() -> str:
    """config.json "mode": "live" only when written exactly so; anything else keeps the
    paper folders (core/broker.py refuses to start on an invalid mode)."""
    try:
        mode = json.loads(CONFIG_FILE.read_text()).get("mode", "paper")
    except (OSError, ValueError, AttributeError):
        mode = "paper"
    return "live" if mode == "live" else "paper"


MODE = _mode()
_MODE_DIR = "live" if MODE == "live" else ""  # paper: the original folders

# generated data (never tracked)
DATA_DIR = PROJECT_ROOT / "data"
TRADES_CSV = DATA_DIR / _MODE_DIR / "trades.csv"
WATCHLIST_FILE = DATA_DIR / "watchlist.txt"

# runtime state and logs (never tracked)
STOP_FILE = PROJECT_ROOT / "STOP"
LOGS_DIR = (PROJECT_ROOT / "logs" / _MODE_DIR).resolve()
ARCHIVE_DIR = LOGS_DIR / "archive"
DASHBOARD_DIR = (PROJECT_ROOT / "dashboard" / _MODE_DIR).resolve()
DASHBOARD_FILE = DASHBOARD_DIR / "index.html"  # executive summary (reporting/dashboard.py)
PERFORMANCE_FILE = DASHBOARD_DIR / "performance.html"  # trade performance (compute_perf.py)

# the interpreter that runs paperbot; scheduled/background jobs reuse it on every OS
PYTHON = Path(sys.executable).resolve()
# legacy Windows Task Scheduler setup (ops/setup_schedule.py): project/.venv or its parent's
_VENV_CANDIDATES = (PROJECT_ROOT / ".venv" / "Scripts" / "python.exe",
                    PROJECT_ROOT.parent / ".venv" / "Scripts" / "python.exe")
VENV_PYTHON = next((p.resolve() for p in _VENV_CANDIDATES if p.exists()),
                   _VENV_CANDIDATES[1].resolve())
