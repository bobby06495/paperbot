"""Small helpers shared by the runner, the strategy engine and the blocks."""
import datetime as dt
import logging
import math
from zoneinfo import ZoneInfo

from paperbot.reporting.notify import notify

ET = ZoneInfo("America/New_York")
RTH_OPEN, RTH_CLOSE = dt.time(9, 30), dt.time(16, 0)

log = logging.getLogger("paperbot")
_warned: set[str] = set()  # one-time warnings, keyed by message


def warn_once(key: str, level: int, msg: str, *args):
    if key not in _warned:
        _warned.add(key)
        log.log(level, msg, *args)


def push(title: str, body, priority: str = "default"):
    """Push notification that can never interfere with trading. `body` is a
    callable so even building the text cannot raise into the strategy."""
    try:
        notify(title, body(), priority)
    except Exception:  # noqa: BLE001
        pass


def hhmm(text: str) -> dt.time:
    hour, minute = map(int, text.split(":"))
    return dt.time(hour, minute)


def finite(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None
