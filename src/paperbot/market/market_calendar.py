"""US equity market calendar: is the market open today, and until when (ET)?

    from paperbot.market import market_calendar as mc
    day = mc.session_at(now)        # any aware datetime; the date is the New York date
    day.kind                        # regular | early_close | holiday | weekend | unknown
    day.is_open(now)                # open <= ET time < close on a trading day
    mc.shift(dt.time(15, 51), day)  # 12:51 on a 13:00 early close, unchanged otherwise

The dates live in config/market_calendar.json (NYSE's published holidays and
1:00 p.m. early closes; Nasdaq keeps the same schedule, so one calendar serves
both). The file also takes unexpected closures without a code change:

    "unscheduled_closures":     {"2027-01-09": "National Day of Mourning"}
    "unscheduled_early_closes": {"2027-03-01": {"close": "14:00", "reason": "..."}}

Fail closed: a weekday outside covered_years, or any problem reading or
validating the file, is "unknown", never "open". Trading code treats unknown
like a holiday (no entries, exits or force closes; protective stops are still
reconciled) and raises an ALERT. coverage_warning() starts warning 30 days
before the last covered year ends, so the file is extended in time.

At connection time the bot also asks IBKR for SPY's liquid hours
(cross_check_with_ibkr). When IBKR reports the day closed, a later open or an
earlier close than the file, that stricter answer wins for the rest of the
process; when IBKR reports a session the file does not have, the file still
wins (closed) and an ALERT is logged. A malformed IBKR answer is logged and
ignored: the file's session stays as it is.

Times are ET wall-clock times from zoneinfo, so daylight saving needs nothing.
Standard library only (scheduled_pass imports this before its fast exit).
"""
import dataclasses
import datetime as dt
import json
import logging
import re
from pathlib import Path
from zoneinfo import ZoneInfo

from paperbot import paths

ET = ZoneInfo("America/New_York")
CALENDAR_FILE = paths.CONFIG_DIR / "market_calendar.json"
REGULAR_OPEN, REGULAR_CLOSE = dt.time(9, 30), dt.time(16, 0)
EARLY_CLOSE = dt.time(13, 0)
COVERAGE_WARNING_DAYS = 30
TRADING_KINDS = ("regular", "early_close")
REQUIRED_KEYS = ("covered_years", "holidays", "early_closes")
_ET_ZONE_IDS = ("US/Eastern", "America/New_York", "EST5EDT", "EST")  # IBKR timeZoneId

log = logging.getLogger("paperbot")


class CalendarError(ValueError):
    """The calendar file is missing or invalid (trading code then fails closed)."""


@dataclasses.dataclass(frozen=True)
class Session:
    day: dt.date
    kind: str  # regular | early_close | holiday | weekend | unknown
    open: dt.time | None = None
    close: dt.time | None = None
    reason: str = ""

    @property
    def trading(self) -> bool:
        return self.kind in TRADING_KINDS

    def is_open(self, now: dt.datetime) -> bool:
        """open <= now < close on this trading day (now converted to ET)."""
        if not self.trading:
            return False
        now = now.astimezone(ET) if now.tzinfo else now
        return now.date() == self.day and self.open <= now.time() < self.close

    def describe(self) -> str:
        if self.trading:
            text = f"{self.day} {self.kind} {self.open:%H:%M}-{self.close:%H:%M} ET"
        else:
            text = f"{self.day} {self.kind}"
        return f"{text} ({self.reason})" if self.reason else text


# ---------------------------------------------------------------- the file

_cache: dict = {"key": None, "data": None}
_ibkr: dict[dt.date, tuple[dt.time, dt.time] | None] = {}  # IBKR liquid hours (None = closed)


def _hhmm(where: str, text) -> dt.time:
    try:
        hour, minute = map(int, str(text).split(":"))
        return dt.time(hour, minute)
    except (ValueError, TypeError):
        raise CalendarError(f"{where} must be \"HH:MM\", got {text!r}") from None


def _date(where: str, text) -> dt.date:
    try:
        return dt.date.fromisoformat(str(text))
    except ValueError:
        raise CalendarError(f"{where}: {text!r} is not a YYYY-MM-DD date") from None


def validate(raw: dict) -> dict:
    """The calendar file parsed into dates and times. CalendarError if anything is off:
    a missing section, a bad date or time, a weekend date, a date outside covered_years,
    or a date listed both as a holiday and as an early close."""
    if not isinstance(raw, dict):
        raise CalendarError("market_calendar.json must be an object")
    missing = [k for k in REQUIRED_KEYS if k not in raw]
    if missing:
        raise CalendarError(f"market_calendar.json is missing {missing}")
    years = raw["covered_years"]
    if not (isinstance(years, list) and years and all(isinstance(y, int) for y in years)):
        raise CalendarError("covered_years must be a non-empty list of years")
    years = set(years)
    regular_open = _hhmm("regular_open", raw.get("regular_open", "09:30"))
    regular_close = _hhmm("regular_close", raw.get("regular_close", "16:00"))
    early = _hhmm("early_close", raw.get("early_close", "13:00"))
    if not regular_open < early < regular_close:
        raise CalendarError("times must satisfy regular_open < early_close < regular_close")

    def dates(section: str, require_covered: bool) -> dict[dt.date, object]:
        value = raw.get(section, {})
        if not isinstance(value, dict):
            raise CalendarError(f"{section} must be an object of \"YYYY-MM-DD\": ...")
        out = {}
        for text, note in value.items():
            day = _date(section, text)
            if day.weekday() >= 5:
                raise CalendarError(f"{section}: {day} is a weekend day")
            if require_covered and day.year not in years:
                raise CalendarError(f"{section}: {day} is outside covered_years {sorted(years)}")
            out[day] = note
        return out

    holidays = {d: str(n) for d, n in dates("holidays", True).items()}
    early_closes = {d: (early, str(n)) for d, n in dates("early_closes", True).items()}
    closures = {d: str(n) for d, n in dates("unscheduled_closures", False).items()}
    unscheduled_early = {}
    for day, spec in dates("unscheduled_early_closes", False).items():
        if not isinstance(spec, dict):
            raise CalendarError(f"unscheduled_early_closes: {day} needs {{\"close\": \"HH:MM\"}}")
        close = _hhmm(f"unscheduled_early_closes {day}.close", spec.get("close"))
        if not regular_open < close < regular_close:
            raise CalendarError(f"unscheduled_early_closes: {day} close must be within the session")
        unscheduled_early[day] = (close, str(spec.get("reason", "unscheduled early close")))
    both = sorted(set(holidays) & set(early_closes))
    if both:
        raise CalendarError(f"dates listed as holiday and early close: {both}")
    return {"years": years, "open": regular_open, "close": regular_close,
            "holidays": holidays, "early_closes": early_closes, "closures": closures,
            "unscheduled_early": unscheduled_early}


def load(path: Path | None = None) -> dict:
    """The validated calendar (cached until the file changes). CalendarError if invalid."""
    path = path or CALENDAR_FILE
    try:
        stat = path.stat()
        key = (str(path), stat.st_mtime_ns, stat.st_size)
        if _cache["key"] == key:
            return _cache["data"]
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise CalendarError(f"cannot read {path.name}: {e}") from None
    data = validate(raw)
    _cache.update(key=key, data=data)
    return data


# ---------------------------------------------------------------- sessions

def _file_session(day: dt.date, path: Path | None) -> Session:
    if day.weekday() >= 5:
        return Session(day, "weekend", reason=day.strftime("%A"))
    try:
        cal = load(path)
    except CalendarError as e:
        return Session(day, "unknown", reason=f"calendar unavailable: {e}")
    if day in cal["closures"]:
        return Session(day, "holiday", reason=f"unscheduled closure: {cal['closures'][day]}")
    if day.year not in cal["years"]:
        return Session(day, "unknown", reason=f"{day.year} is not in market_calendar.json "
                                              f"covered_years {sorted(cal['years'])}")
    if day in cal["holidays"]:
        return Session(day, "holiday", reason=cal["holidays"][day])
    if day in cal["unscheduled_early"]:
        close, why = cal["unscheduled_early"][day]
        return Session(day, "early_close", cal["open"], close, why)
    if day in cal["early_closes"]:
        close, why = cal["early_closes"][day]
        return Session(day, "early_close", cal["open"], close, why)
    return Session(day, "regular", cal["open"], cal["close"])


def session(day: dt.date, path: Path | None = None) -> Session:
    """The US equity session on New York date `day`, with IBKR's stricter view applied."""
    found = _file_session(day, path)
    if day not in _ibkr or not found.trading:
        return found
    hours = _ibkr[day]
    if hours is None:
        return Session(day, "holiday", reason="IBKR reports the market closed today "
                                              "(not in market_calendar.json)")
    open_, close = max(found.open, hours[0]), min(found.close, hours[1])
    if open_ >= close:
        return Session(day, "holiday", reason=f"IBKR hours {hours[0]:%H:%M}-{hours[1]:%H:%M} "
                                              "leave no session today")
    if (open_, close) == (found.open, found.close):
        return found
    reasons = [found.reason] if found.reason else []
    if open_ > found.open:
        reasons.append(f"IBKR reports a late open at {open_:%H:%M}")
    if close < found.close:
        reasons.append(f"IBKR reports an early close at {close:%H:%M}")
    kind = "early_close" if close < found.close else found.kind
    return Session(day, kind, open_, close, "; ".join(reasons))


def session_at(now: dt.datetime | None = None, path: Path | None = None) -> Session:
    """The session of the New York date of `now` (default: the current time)."""
    now = now or dt.datetime.now(ET)
    local = now.astimezone(ET) if now.tzinfo else now
    return session(local.date(), path)


def weekday_session(now: dt.datetime) -> Session:
    """The pre-calendar rule: every weekday is a regular 09:30-16:00 session. Used by
    the backtester, whose results must not change with this calendar."""
    local = now.astimezone(ET) if now.tzinfo else now
    day = local.date()
    if day.weekday() >= 5:
        return Session(day, "weekend", reason=day.strftime("%A"))
    return Session(day, "regular", REGULAR_OPEN, REGULAR_CLOSE)


def shift(t: dt.time | None, day: Session) -> dt.time | None:
    """A strategy time on `day`: on an early close, a time at or after that close moves
    earlier by the time the session lost (15:51 -> 12:51 on a 13:00 close), so it keeps
    its distance to the close. Earlier times and regular days are unchanged."""
    if t is None or day.kind != "early_close" or t < day.close:
        return t
    lost = dt.datetime.combine(day.day, REGULAR_CLOSE) - dt.datetime.combine(day.day, day.close)
    return (dt.datetime.combine(day.day, t) - lost).time()


def coverage_end(path: Path | None = None) -> dt.date | None:
    try:
        return dt.date(max(load(path)["years"]), 12, 31)
    except CalendarError:
        return None


def coverage_warning(today: dt.date, path: Path | None = None) -> str | None:
    """An ALERT text when the calendar ends within COVERAGE_WARNING_DAYS (or is broken)."""
    try:
        load(path)
    except CalendarError as e:
        return f"market calendar unusable, trading days are treated as closed: {e}"
    end = coverage_end(path)
    if end is not None and (end - today).days <= COVERAGE_WARNING_DAYS:
        return (f"market_calendar.json covers dates only until {end}; add the next year's "
                "NYSE holidays and early closes (uncovered days are treated as closed)")
    return None


def next_trading_day(after: dt.date, path: Path | None = None, limit: int = 14) -> Session | None:
    day = after
    for _ in range(limit):
        day += dt.timedelta(days=1)
        found = session(day, path)
        if found.trading:
            return found
    return None


# ---------------------------------------------------------------- IBKR cross-check

_HOURS_RANGE = re.compile(r"^(\d{8}):(\d{2})(\d{2})-(\d{8}):(\d{2})(\d{2})$")
_HOURS_CLOSED = re.compile(r"^(\d{8}):CLOSED$")


def parse_liquid_hours(text: str, day: dt.date) -> tuple[dt.time, dt.time] | None | str:
    """`day` from an IBKR liquidHours string
    ("20261127:0930-20261127:1300;20261126:CLOSED;..."): (open, close), None when CLOSED,
    "missing" when the day is not listed, or "malformed" when an entry for the day does
    not parse (bad digits, an impossible time, a close not after the open). Never raises."""
    stamp = day.strftime("%Y%m%d")
    opens, closes = [], []
    for part in str(text or "").split(";"):
        part = part.strip()
        if not part.startswith(stamp):
            continue
        if _HOURS_CLOSED.match(part):
            return None
        match = _HOURS_RANGE.match(part)
        if not match:
            return "malformed"
        start_day, sh, sm, end_day, eh, em = match.groups()
        if start_day != stamp or end_day < start_day:
            return "malformed"
        try:
            opened = dt.time(int(sh), int(sm))
            close = dt.time(int(eh), int(em)) if end_day == stamp else dt.time(23, 59)
        except ValueError:  # hour 25, minute 61, ...
            return "malformed"
        if close <= opened:
            return "malformed"
        opens.append(opened)
        closes.append(close)
    if not opens:
        return "missing"
    return min(opens), max(closes)


def compare(day: Session, ibkr) -> str | None:
    """A mismatch between the file's session and IBKR's hours, as text, or None."""
    if ibkr in ("missing", "malformed"):
        return None
    if ibkr is None:
        return None if not day.trading else (
            f"IBKR reports {day.day} CLOSED but market_calendar.json says {day.kind}; "
            "treating today as closed")
    if not day.trading:
        if day.kind in ("weekend",):
            return None
        return (f"IBKR reports a session {ibkr[0]:%H:%M}-{ibkr[1]:%H:%M} ET on {day.day} but "
                f"market_calendar.json says {day.kind}; staying closed (update the calendar)")
    if ibkr[0] >= day.close or ibkr[1] <= day.open:
        return (f"IBKR hours {ibkr[0]:%H:%M}-{ibkr[1]:%H:%M} ET on {day.day} leave no session "
                f"within market_calendar.json {day.open:%H:%M}-{day.close:%H:%M}; "
                "treating today as closed")
    stricter = []
    if ibkr[0] > day.open:
        stricter.append(f"a late open at {ibkr[0]:%H:%M} ET (calendar {day.open:%H:%M}); "
                        f"no trading before {ibkr[0]:%H:%M}")
    if ibkr[1] < day.close:
        stricter.append(f"an early close at {ibkr[1]:%H:%M} ET (calendar {day.close:%H:%M}); "
                        f"using {ibkr[1]:%H:%M}")
    if stricter:
        return f"IBKR reports {' and '.join(stricter)} on {day.day}"
    if ibkr[1] > day.close or ibkr[0] != day.open:
        return (f"IBKR hours {ibkr[0]:%H:%M}-{ibkr[1]:%H:%M} ET on {day.day} differ from "
                f"market_calendar.json {day.open:%H:%M}-{day.close:%H:%M}; keeping the calendar's")
    return None


def apply_ibkr_hours(day: dt.date, ibkr) -> None:
    """Remember IBKR's hours for `day` (only ever makes the session stricter).
    "missing" and "malformed" answers are ignored."""
    if ibkr is None or (isinstance(ibkr, tuple) and len(ibkr) == 2):
        _ibkr[day] = ibkr


def cross_check_with_ibkr(ib, now: dt.datetime | None = None, timeout: float = 10.0) -> str | None:
    """Ask IBKR for SPY's liquid hours, apply them to today and return a mismatch text
    (the caller logs it as an ALERT). Never raises: on any failure the calendar file
    stays the only source and a warning is logged."""
    import asyncio

    from ib_async import Stock

    now = (now or dt.datetime.now(ET)).astimezone(ET)
    try:
        details = ib.run(asyncio.wait_for(
            ib.reqContractDetailsAsync(Stock("SPY", "SMART", "USD")), timeout))
        detail = details[0] if isinstance(details, list) and details else None
        hours = getattr(detail, "liquidHours", None)
        zone = getattr(detail, "timeZoneId", "") or ""
    except Exception as e:  # noqa: BLE001 - the cross-check must never stop the bot
        log.warning("market hours cross-check with IBKR skipped: %r", e)
        return None
    if not isinstance(hours, str) or not hours:
        log.warning("market hours cross-check with IBKR skipped: no SPY liquid hours")
        return None
    if not isinstance(zone, str) or (zone and zone not in _ET_ZONE_IDS):
        log.warning("market hours cross-check skipped: IBKR time zone %s", zone)
        return None
    try:
        ibkr = parse_liquid_hours(hours, now.date())
        if ibkr == "malformed":
            log.warning("market hours cross-check skipped: malformed IBKR liquid hours %r; "
                        "keeping the calendar's session", hours[:300])
            return None
        mismatch = compare(_file_session(now.date(), None), ibkr)
        apply_ibkr_hours(now.date(), ibkr)
    except Exception as e:  # noqa: BLE001 - the cross-check must never stop the bot
        log.warning("market hours cross-check skipped: cannot use IBKR liquid hours %r: %r",
                    hours[:300], e)
        return None
    if mismatch is None:
        log.info("market hours confirmed with IBKR: %s", session(now.date()).describe())
    return mismatch
