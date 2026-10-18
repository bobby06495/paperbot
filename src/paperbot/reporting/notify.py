"""Push notifications for paperbot: Telegram, with ntfy as a fallback. Never raises.

    from paperbot.reporting.notify import notify
    notify("BUY AAPL", "@ $190.00, stop $185.00, qty 1")          # priority "default"
    notify("Cycle CRASHED", "ValueError: ...", "high")

Configuration (.env in the project root, or the environment; never printed or logged):
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   Telegram sendMessage, Markdown
  NOTIFY_URL                             optional ntfy topic URL, e.g. https://ntfy.sh/my-topic;
                                         used only if Telegram is not configured or its send failed
With none of them set, notify() does nothing. PAPERBOT_NOTIFY=off (set by
the test suite) turns every notification off.

Fire-and-forget: each message is sent from its own background thread with a
5-second timeout per request, so trading code never waits on the network.
The threads are not daemons, so a short-lived process (cycle.py) still
delivers pending messages before it exits. Every failure is written to
logs/notify_errors.log with the token and chat id masked; nothing is raised
and nothing is printed.

AlertHandler is a logging handler (installed by bot.setup_logging) that
forwards every log line containing "ALERT", and every ERROR, as a
high-priority notification, at most once per 10 minutes per identical line.
"""
import datetime as dt
import logging
import os
import re
import threading
import time
import traceback
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

from paperbot import paths

ERROR_LOG = paths.LOGS_DIR / "notify_errors.log"
ET = ZoneInfo("America/New_York")
TIMEOUT_SECONDS = 5
MAX_BODY = 3500  # Telegram allows 4096 characters per message

load_dotenv(paths.ENV_FILE)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
NOTIFY_URL = os.getenv("NOTIFY_URL", "").strip()

_MARKDOWN_SPECIAL = re.compile(r"([_*`\[])")


def escape_markdown(text: str) -> str:
    """Escape Telegram (legacy) Markdown so symbols like '_' cannot break the message."""
    return _MARKDOWN_SPECIAL.sub(r"\\\1", text)


def _mask(text: str) -> str:
    for secret in (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID):
        if secret:
            text = text.replace(secret, "***")
    return text


def _log_error(text: str):
    try:
        ERROR_LOG.parent.mkdir(exist_ok=True)
        stamp = dt.datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S ET")
        with open(ERROR_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{stamp}] {_mask(text).rstrip()}\n")
    except Exception:  # noqa: BLE001 - logging must never raise either
        pass


def _send_telegram(title: str, body: str) -> bool:
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return False
    # Escapes are not allowed inside the *bold* entity, so the title drops special chars.
    text = f"*{_MARKDOWN_SPECIAL.sub('', title)}*\n{escape_markdown(body)}"
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=TIMEOUT_SECONDS)
        if response.ok:
            return True
        _log_error(f"telegram HTTP {response.status_code}: {response.text[:300]}")
    except Exception as e:  # noqa: BLE001
        _log_error(f"telegram {type(e).__name__}: {e}")
    return False


def _send_ntfy(title: str, body: str, priority: str) -> bool:
    if not NOTIFY_URL:
        return False
    try:
        response = requests.post(
            NOTIFY_URL, data=body.encode("utf-8"),
            headers={"Title": title.encode("ascii", "replace").decode("ascii"),
                     "Priority": priority},
            timeout=TIMEOUT_SECONDS)
        if response.ok:
            return True
        _log_error(f"ntfy HTTP {response.status_code}: {response.text[:300]}")
    except Exception as e:  # noqa: BLE001
        _log_error(f"ntfy {type(e).__name__}: {e}")
    return False


def _deliver(title: str, body: str, priority: str):
    try:
        if not _send_telegram(title, body):
            _send_ntfy(title, body, priority)
    except Exception:  # noqa: BLE001
        _log_error(traceback.format_exc())


def notify(title: str, body: str, priority: str = "default") -> None:
    """Send a push notification in the background. Never raises, never blocks."""
    try:
        if os.getenv("PAPERBOT_NOTIFY", "").strip().lower() in ("0", "off", "false", "no"):
            return
        if not ((TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID) or NOTIFY_URL):
            return
        args = (str(title)[:200], str(body)[:MAX_BODY], str(priority))
        threading.Thread(target=_deliver, args=args, name="notify", daemon=False).start()
    except Exception:  # noqa: BLE001
        _log_error(traceback.format_exc())


class AlertHandler(logging.Handler):
    """Forward "ALERT" log lines and ERRORs as high-priority notifications."""
    REPEAT_SECONDS = 600  # the same line is sent at most once per 10 minutes

    def __init__(self):
        super().__init__(level=logging.INFO)
        self._last_sent: dict[str, float] = {}

    def emit(self, record: logging.LogRecord):
        try:
            message = record.getMessage()
            if "ALERT" not in message and record.levelno < logging.ERROR:
                return
            now = time.monotonic()
            if now - self._last_sent.get(message, -self.REPEAT_SECONDS) < self.REPEAT_SECONDS:
                return
            self._last_sent[message] = now
            body = message.replace("ALERT ", "", 1)
            notify("ALERT" if "ALERT" in message else record.levelname, body, "high")
        except Exception:  # noqa: BLE001
            pass
