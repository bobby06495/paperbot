"""Every file and folder paperbot reads or writes, resolved once from the project root.

The project root is the folder holding config/, src/ and pyproject.toml. It is
found from this file's location (src/paperbot/paths.py, editable install) or
taken from the PAPERBOT_HOME environment variable when set. All paths are
absolute and .resolve()d, so they work from any working directory, including
Windows Task Scheduler runs.

Imports only the standard library, so it stays cheap to import (cycle.py's
off-hours exit relies on that).
"""
import os
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = Path(os.environ.get("PAPERBOT_HOME") or PACKAGE_DIR.parent.parent).resolve()

# configuration (tracked in git)
CONFIG_DIR = PROJECT_ROOT / "config"
CONFIG_FILE = CONFIG_DIR / "config.json"
RULES_FILE = CONFIG_DIR / "rules.json"
UNIVERSE_FILE = CONFIG_DIR / "universe.txt"
ENV_FILE = PROJECT_ROOT / ".env"  # secrets, never tracked

# generated data (never tracked)
DATA_DIR = PROJECT_ROOT / "data"
TRADES_CSV = DATA_DIR / "trades.csv"
WATCHLIST_FILE = DATA_DIR / "watchlist.txt"

# runtime state and logs (never tracked)
STOP_FILE = PROJECT_ROOT / "STOP"
LOGS_DIR = PROJECT_ROOT / "logs"
ARCHIVE_DIR = LOGS_DIR / "archive"
DASHBOARD_FILE = PROJECT_ROOT / "dashboard" / "index.html"

# the virtual environment used by scheduled tasks: project/.venv or its parent's .venv
_VENV_CANDIDATES = (PROJECT_ROOT / ".venv" / "Scripts" / "python.exe",
                    PROJECT_ROOT.parent / ".venv" / "Scripts" / "python.exe")
VENV_PYTHON = next((p.resolve() for p in _VENV_CANDIDATES if p.exists()),
                   _VENV_CANDIDATES[1].resolve())
