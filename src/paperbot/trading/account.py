"""Account value in USD, whatever the account's base currency.

The bot sizes positions and checks risk in USD (rules.json percentages of net
liquidation, config.json dollar caps). IBKR reports account values in the
account's BASE currency, so an account based in e.g. SGD has no USD
NetLiquidation row. net_liquidation_usd() returns, in order:

  1. IBKR's USD NetLiquidation, when it reports one (a USD-based account).
     Used as is; no conversion, no network. This applies automatically as
     soon as the account's base currency is USD.
  2. Otherwise, if config.json "fx_fallback" is "auto" (the default): the
     base-currency NetLiquidation divided by the rate (base units per USD):
       a. IBKR's ExchangeRate row for USD, when the account reports one,
       b. Yahoo Finance "<CCY>=X", fetched at most once per ET day and saved
          in logs/fx_rate.json,
       c. that saved rate, if it is at most MAX_RATE_AGE_DAYS old.
  3. Otherwise None, as before: the entry scan is skipped and the risk guard
     blocks entries. An ALERT is logged once per day.

"fx_fallback": "off" restores the original USD-only behaviour.
last_conversion() describes the latest result for telemetry/the dashboard.
"""
import datetime as dt
import json
import logging
import math
import threading
from zoneinfo import ZoneInfo

from paperbot import paths
from paperbot.runtime.runstate import StateCorrupt, read_json_state, write_json_atomic

ET = ZoneInfo("America/New_York")
RATE_FILE = paths.LOGS_DIR / "fx_rate.json"
MAX_RATE_AGE_DAYS = 5
FETCH_TIMEOUT_SECONDS = 10
RATE_BOUNDS = (1e-4, 1e5)  # anything outside is treated as a bad quote

log = logging.getLogger("paperbot")
_last: dict | None = None  # latest result in this process, for telemetry
_last_daily: dict | None = None  # latest daily_pnl() decision, for telemetry


def _float(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def fallback_mode() -> str:
    """config.json "fx_fallback": "auto" (default) or "off"."""
    try:
        mode = json.loads(paths.CONFIG_FILE.read_text()).get("fx_fallback", "auto")
    except (OSError, ValueError):
        mode = "auto"
    return "off" if str(mode).lower() == "off" else "auto"


def _valid(rate) -> bool:
    return rate is not None and RATE_BOUNDS[0] < rate < RATE_BOUNDS[1]


def fetch_yahoo_rate(currency: str) -> float | None:
    """Units of `currency` per USD from Yahoo Finance ("SGD=X"), or None.
    Runs in a thread with a hard timeout so a slow request cannot stall the bot."""
    result: list = []

    def work():
        try:
            import yfinance as yf  # imported only when a conversion is needed
            result.append(_float(yf.Ticker(f"{currency}=X").fast_info.last_price))
        except Exception:  # noqa: BLE001 - any failure means "no rate"
            result.append(None)

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    thread.join(FETCH_TIMEOUT_SECONDS)
    return result[0] if result else None


def _saved_rate(currency: str) -> dict:
    try:
        saved = read_json_state(RATE_FILE) or {}
    except StateCorrupt:
        return {}
    return saved if saved.get("currency") == currency else {}


def _rate(currency: str, values: list, today: dt.date) -> tuple[float | None, str]:
    """(base units per USD, source) or (None, reason)."""
    ibkr = next((_float(v.value) for v in values
                 if v.tag == "ExchangeRate" and v.currency == "USD"), None)
    if _valid(ibkr):
        return ibkr, "IBKR ExchangeRate"
    saved = _saved_rate(currency)
    if saved.get("date") == today.isoformat() and _valid(_float(saved.get("rate"))):
        return float(saved["rate"]), f"Yahoo {saved['date']}"
    if saved.get("last_attempt") != today.isoformat() or not saved:
        fresh = fetch_yahoo_rate(currency)
        if _valid(fresh):
            write_json_atomic(RATE_FILE, {"currency": currency, "rate": fresh,
                                          "date": today.isoformat(), "source": "yahoo",
                                          "last_attempt": today.isoformat(),
                                          "fetched": dt.datetime.now(ET).isoformat(timespec="seconds")})
            return fresh, f"Yahoo {today.isoformat()}"
        saved = {**saved, "currency": currency, "last_attempt": today.isoformat()}
        write_json_atomic(RATE_FILE, saved)  # retry Yahoo tomorrow, not every pass
    try:
        age = (today - dt.date.fromisoformat(saved.get("date", ""))).days
    except ValueError:
        age = None
    if age is not None and age <= MAX_RATE_AGE_DAYS and _valid(_float(saved.get("rate"))):
        return float(saved["rate"]), f"saved Yahoo rate from {saved['date']}"
    return None, "no USD rate available (IBKR has none, Yahoo failed, no recent saved rate)"


def _alert_once(currency: str, today: dt.date, reason: str):
    saved = _saved_rate(currency) or {"currency": currency}
    if saved.get("alerted") == today.isoformat():
        log.warning("account value in %s cannot be converted to USD: %s; entries blocked",
                    currency, reason)
        return
    write_json_atomic(RATE_FILE, {**saved, "alerted": today.isoformat()})
    log.error("ALERT account value is in %s and cannot be converted to USD (%s); "
              "entry scan skipped and entries blocked until a rate is available",
              currency, reason)


def net_liquidation_usd(ib, account: str, fetch: bool = True) -> float | None:
    """NetLiquidation in USD (see module doc). With fetch=False only this
    process's latest result is returned: no file, no network (for telemetry)."""
    global _last
    if not fetch:
        return _last["usd"] if _last else None
    values = list(ib.accountValues(account))
    usd = next((_float(v.value) for v in values
                if v.tag == "NetLiquidation" and v.currency == "USD"), None)
    if usd is not None:
        _last = {"usd": usd, "currency": "USD", "base": usd, "rate": 1.0, "source": "IBKR (USD)"}
        return usd
    base = next(((v.currency, _float(v.value)) for v in values
                 if v.tag == "NetLiquidation" and v.currency not in ("USD", "BASE", "")
                 and _float(v.value) is not None), None)
    if base is None:
        _last = None
        return None  # nothing reported yet: same as before
    currency, amount = base
    if fallback_mode() == "off":
        _last = {"usd": None, "currency": currency, "base": amount, "rate": None,
                 "source": "fx_fallback off"}
        return None
    today = dt.datetime.now(ET).date()
    rate, source = _rate(currency, values, today)
    if rate is None:
        _alert_once(currency, today, source)
        _last = {"usd": None, "currency": currency, "base": amount, "rate": None, "source": source}
        return None
    usd = round(amount / rate, 2)
    if not _last or _last.get("source") != source or _last.get("usd") != usd:
        log.info("account reports %s; using NetLiquidation USD %.2f (%s %.2f / %.4f %s per USD, %s)",
                 currency, usd, currency, amount, rate, currency, source)
    _last = {"usd": usd, "currency": currency, "base": amount, "rate": rate, "source": source}
    return usd


def daily_pnl(ib, account: str, pnl) -> float | None:
    """Today's P&L for the risk guard.

    IBKR's reqPnL figure when it sends one. IBKR sends no update at all while
    the account is flat, which left dailyPnL NaN and blocked every first entry
    of the day. So: 0.0 when the account provably has nothing to report (no
    position and no execution today), otherwise None, which keeps entries
    blocked exactly as before.
    """
    global _last_daily
    value = _float(getattr(pnl, "dailyPnL", None))
    if value is not None:
        _last_daily = {"value": value, "source": "IBKR"}
        return value
    _last_daily = {"value": None, "source": "IBKR sent none; account holds a position"}
    try:
        if any(p.position for p in ib.positions(account)):
            return None  # holding something: its P&L is unknown
        today = dt.datetime.now(ET).date()
        for fill in ib.reqExecutions():
            e = fill.execution
            if e.acctNumber in ("", None, account) and e.time.astimezone(ET).date() == today:
                _last_daily["source"] = "IBKR sent none; account traded today"
                return None  # traded today: realized P&L and commissions are unknown
    except Exception as exc:  # noqa: BLE001 - cannot prove flat: fail closed
        log.warning("daily P&L fallback skipped: %r", exc)
        _last_daily["source"] = f"IBKR sent none; flat check failed ({type(exc).__name__})"
        return None
    _last_daily = {"value": 0.0, "source": "flat today (no position, no execution); IBKR sends none"}
    return 0.0


def last_daily_pnl() -> dict | None:
    """The latest daily_pnl() decision in this process, or None."""
    return dict(_last_daily) if _last_daily else None


def last_conversion() -> dict | None:
    """The latest net_liquidation_usd() result in this process, or None."""
    return dict(_last) if _last else None
