"""Passive per-pass telemetry for the dashboard. Reporting only.

record_pass() is called from bot.after_pass() at the end of every bot.py loop
and every cycle.py pass, after the trading work is done. It never places,
modifies or cancels orders and never sends a request to IBKR. It reads:
  * ib_async objects already cached in memory (portfolio, trades, account
    values, P&L subscription),
  * logs/state.json and logs/strategy_state.json (read only),
  * the lines the bot appended to bot.log, safety_log.jsonl and orders.jsonl
    since the previous pass (byte offsets in logs/telemetry_state.json),
and appends ONE JSON line per pass to logs/telemetry.jsonl. The line format
is the contract in docs/telemetry.schema.json (SCHEMA_VERSION below).
bot.after_pass() wraps the call, so a telemetry failure is logged and trading
continues. paperbot-rotate archives telemetry.jsonl with the other *.jsonl.

The parsing helpers (phase, classify_failure, parse_log_line ...) are also
used by dashboard.py, which turns the telemetry into the dashboard pages.
"""
import datetime as dt
import json
import math
import os
import re
from pathlib import Path
from zoneinfo import ZoneInfo

from paperbot import paths
from paperbot.runtime import runstate
from paperbot.trading.risk import REF_ENTRY, REF_EXIT, REF_STOP

SCHEMA_VERSION = 1
ET = ZoneInfo("America/New_York")
LOGS = paths.LOGS_DIR
TELEMETRY_FILE = LOGS / "telemetry.jsonl"
STATE_FILE = LOGS / "telemetry_state.json"
BOT_LOG = LOGS / "bot.log"
SAFETY_LOG = LOGS / "safety_log.jsonl"
ORDERS_LOG = LOGS / "orders.jsonl"
RISK_STATE = LOGS / "state.json"
STRATEGY_STATE = LOGS / "strategy_state.json"

FIRST_READ_BYTES = 256 * 1024  # no saved offset yet: look at the tail only
FIRST_READ_WINDOW = dt.timedelta(minutes=15)  # ... and only at recent lines
ROLES = {REF_ENTRY: "entry", REF_STOP: "stop", REF_EXIT: "exit"}

# ---------------------------------------------------------------- session model

SESSION_OPEN, SESSION_CLOSE, RUN_UNTIL, ALERT_AT = (dt.time(9, 30), dt.time(16, 0),
                                                     dt.time(16, 5), dt.time(15, 58))
PHASES = {
    "closed": "Market closed",
    "pre_open": "Pre-open",
    "opening": "Opening: protect & manage exits, no entries yet",
    "entry_window": "Entry window: manage exits + scan for entries",
    "manage_only": "Exit management only (entries closed)",
    "force_close": "Force close: flattening positions",
    "post_close": "Post-close protect pass",
}


def hhmm(text: str, default: str) -> dt.time:
    try:
        hour, minute = map(int, str(text).split(":"))
        return dt.time(hour, minute)
    except (TypeError, ValueError):
        hour, minute = map(int, default.split(":"))
        return dt.time(hour, minute)


def session_times(rules: dict) -> dict[str, dt.time]:
    tf = (rules or {}).get("time_filter", {})
    return {"open": SESSION_OPEN,
            "entry_start": hhmm(tf.get("earliest_entry_et"), "10:05"),
            "entry_end": hhmm(tf.get("latest_entry_et"), "15:30"),
            "force_close": hhmm(tf.get("force_close_et"), "15:51"),
            "alert": ALERT_AT, "close": SESSION_CLOSE, "run_until": RUN_UNTIL}


def phase(now: dt.datetime, times: dict[str, dt.time]) -> str:
    """Which part of the trading day an ET datetime falls in (holidays not modelled)."""
    if now.weekday() >= 5:
        return "closed"
    t = now.time()
    if t < times["open"]:
        return "pre_open"
    if t < times["entry_start"]:
        return "opening"
    if t <= times["entry_end"]:
        return "entry_window"
    if t < times["force_close"]:
        return "manage_only"
    if t < times["close"]:
        return "force_close"
    if t < times["run_until"]:
        return "post_close"
    return "closed"


# ---------------------------------------------------------------- parsing helpers

LOG_LINE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) ET\] ([A-Z]+) (.*)$")
KEPT_INFO = re.compile(
    r"^(cycle start|cycle skipped|cycle done|connected to paper|signal |SENT |PLACED |"
    r"MODIFIED |cancelled |DRY RUN |scanning |entry scan paused|stopped by)"
    r"|reached .*R, stop to breakeven|trailing stop raised")


def parse_et(text: str | None) -> dt.datetime | None:
    """ET datetime from '2026-10-19 10:48:40 ET', '2026-10-19 10:48:40' or ISO."""
    if not text:
        return None
    text = str(text).replace(" ET", "").strip()
    try:
        value = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    return value.replace(tzinfo=ET) if value.tzinfo is None else value.astimezone(ET)


def parse_log_line(line: str) -> dict | None:
    """{'time', 'level', 'msg'} for an ET-stamped bot.log line, else None."""
    match = LOG_LINE.match(line.rstrip())
    if not match:
        return None
    when = parse_et(match.group(1))
    return {"time": when.isoformat(timespec="seconds"), "level": match.group(2),
            "msg": match.group(3)} if when else None


# The filters in strategy.evaluate(), in the order it runs them (it stops at
# the first failure). "static" failures cannot change later in the day.
STEPS = [("contract", "Contract at IBKR"), ("daily_history", "Daily history"),
         ("prior_price", "Prior close >= min price"), ("D2", "D2 prior close > SMA200"),
         ("bars", ">= 2 completed 5m bars"), ("price", "Price >= min price"),
         ("D3", "D3 opening gap >= min"), ("D1", "D1 above prior-day high"),
         ("I1", "I1 above premarket high"), ("I2", "I2 above today's high"),
         ("I3", "I3 relative volume >= min")]
STEP_INDEX = {key: i for i, (key, _) in enumerate(STEPS)}
_NUM = r"(-?\d+(?:\.\d+)?)"
FAILURES = [  # (pattern on the text after "FAIL ", step, static, kind)
    (re.compile(r"^contract not found"), "contract", True, None),
    (re.compile(r"^no daily history"), "daily_history", True, None),
    (re.compile(rf"^prior close {_NUM} < min {_NUM}"), "prior_price", True, "above"),
    (re.compile(r"^D2 "), "D2", True, None),
    (re.compile(r"^fewer than 2 completed"), "bars", False, None),
    (re.compile(rf"^price {_NUM} < min {_NUM}"), "price", False, "above"),
    (re.compile(rf"^D3 gap {_NUM}% < {_NUM}%"), "D3", True, "above"),
    (re.compile(rf"^D1 price not above prior-day high {_NUM}"), "D1", False, "price_above"),
    (re.compile(r"^I1 no premarket bars"), "I1", True, None),
    (re.compile(rf"^I1 price not above premarket high {_NUM}"), "I1", False, "price_above"),
    (re.compile(rf"^I2 price not above today's high {_NUM}"), "I2", False, "price_above"),
    (re.compile(rf"^I3 RVOL (n/a|{_NUM[1:-1]}) < {_NUM}"), "I3", False, "rvol"),
]
LEVELS = [  # values the passed checks reveal
    (re.compile(rf"^D2 prior close {_NUM} > SMA200 {_NUM}"), ("prior_close", "sma200")),
    (re.compile(rf"^D1 above prior-day high {_NUM}"), ("prior_high",)),
    (re.compile(rf"^I1 above premarket high {_NUM}"), ("premarket_high",)),
    (re.compile(rf"^I2 above today's high {_NUM}"), ("today_high",)),
]


def _float(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) or math.isinf(number) else number


def classify_failure(fail: str | None, price: float | None, reasons: list[str]) -> dict:
    """Which step stopped a symbol and how far it is from passing it.

    distance_pct > 0 is how much the value must still rise (price checks:
    relative to the price; RVOL: relative to the current RVOL).
    """
    levels = {}
    for reason in reasons:
        for pattern, names in LEVELS:
            match = pattern.match(reason)
            if match:
                levels.update({n: float(v) for n, v in zip(names, match.groups())})
    out = {"check": None, "check_label": None, "static": False, "current": None,
           "threshold": None, "distance_pct": None, "passed_steps": len(STEPS),
           "total_steps": len(STEPS), "levels": levels}
    if fail is None:
        return out
    out.update(check="precheck", check_label=fail, passed_steps=0)
    for pattern, key, static, kind in FAILURES:
        match = pattern.match(fail)
        if not match:
            continue
        out.update(check=key, check_label=dict(STEPS)[key], static=static,
                   passed_steps=STEP_INDEX[key])
        groups = match.groups()
        if kind == "above":  # "<current> < <threshold>"
            out["current"], out["threshold"] = _float(groups[0]), _float(groups[1])
        elif kind == "price_above":
            out["current"], out["threshold"] = _float(price), _float(groups[0])
        elif kind == "rvol":
            out["current"], out["threshold"] = _float(groups[0]), _float(groups[-1])
        cur, thr = out["current"], out["threshold"]
        if cur and thr is not None and cur > 0:
            out["distance_pct"] = round((thr / cur - 1) * 100, 3)
        break
    return out


def evaluation(record: dict) -> dict:
    """One safety_log.jsonl line, reduced to what the dashboard shows."""
    reasons = [str(r) for r in record.get("reasons") or []]
    fail = next((r[5:] for r in reasons if r.startswith("FAIL ")), None)
    price = _float(record.get("price"))
    return {"symbol": str(record.get("symbol", "?")).upper(), "time": record.get("time"),
            "pass": bool(record.get("pass")), "price": price or None,
            "gap": _float(record.get("gap")), "rvol": _float(record.get("rvol")),
            "stop": _float(record.get("stop")), "fail": fail, "reasons": reasons,
            **classify_failure(fail, price, reasons)}


def order_event(record: dict) -> dict:
    """One orders.jsonl line, normalised."""
    kind = record.get("event", "?")
    when = parse_et(record.get("time"))
    out = {"time": when.isoformat(timespec="seconds") if when else record.get("time"),
           "event": kind, "order_ids": [], "symbol": None, "detail": ""}
    if kind == "sent":
        intent = record.get("intent") or {}
        out["order_ids"] = [i for i in record.get("orderIds") or [] if i is not None]
        out["symbol"] = intent.get("symbol")
        out["intent"] = {k: intent.get(k) for k in ("action", "quantity", "limit_price",
                                                     "stop_price", "reason", "closing")}
        stop = f", stop {intent['stop_price']}" if intent.get("stop_price") else ""
        out["detail"] = (f"{intent.get('action')} {intent.get('quantity')} @ "
                         f"{intent.get('limit_price') or 'MKT'}{stop} ({intent.get('reason', '')})")
    elif kind == "stop":
        stop = record.get("stop") or {}
        out["order_ids"] = [record.get("orderId")]
        out["symbol"] = stop.get("symbol")
        out["stop"] = {k: stop.get(k) for k in ("quantity", "stop_price", "reason")}
        out["detail"] = f"STP {stop.get('quantity')} @ {stop.get('stop_price')} ({stop.get('reason', '')})"
    elif kind in ("blocked", "dry_run"):
        body = record.get("intent") or record.get("stop") or {}
        out["symbol"] = body.get("symbol")
        what = (f"{body.get('action')} {body.get('quantity')} @ {body.get('limit_price') or 'MKT'}"
                if "action" in body else f"STP {body.get('quantity')} @ {body.get('stop_price')}")
        problems = "; ".join(record.get("problems") or [])
        out["detail"] = what + (f": {problems}" if problems else f" ({body.get('reason', '')})")
        out["problems"] = record.get("problems") or []
    elif kind == "status":
        out["order_ids"] = [record.get("orderId")]
        out["status"], out["filled"] = record.get("status"), record.get("filled")
        out["detail"] = f"{record.get('status')} (filled {record.get('filled')})"
    elif kind == "fill":
        out["order_ids"] = [record.get("orderId")]
        out["symbol"] = record.get("symbol")
        out["fill"] = {k: record.get(k) for k in ("side", "shares", "price")}
        out["detail"] = f"{record.get('side')} {record.get('shares')} @ {record.get('price')}"
    elif kind == "cancel":
        out["order_ids"] = [record.get("orderId")]
        out["symbol"] = record.get("symbol")
        out["detail"] = f"cancel requested ({record.get('why', '')})"
    return out


# ---------------------------------------------------------------- reading new lines

def read_new_lines(path: Path, key: str, offsets: dict) -> tuple[list[str], bool]:
    """Complete lines appended to `path` since the saved offset.

    Returns (lines, first_read). A file that shrank (rotated) is read from the
    start; without a saved offset only the last FIRST_READ_BYTES are read and
    the caller filters by time.
    """
    try:
        size = path.stat().st_size
    except OSError:
        offsets.pop(key, None)
        return [], False
    prev = offsets.get(key)
    first = prev is None
    start = max(0, size - FIRST_READ_BYTES) if first else (prev if prev <= size else 0)
    with open(path, "rb") as f:
        f.seek(start)
        data = f.read(max(0, size - start))
    cut = data.rfind(b"\n") + 1
    offsets[key] = start + cut
    lines = data[:cut].decode("utf-8", errors="replace").splitlines()
    if first and start > 0 and lines:
        lines = lines[1:]  # probably cut in the middle
    return lines, first


def _json_lines(lines: list[str]) -> list[dict]:
    out = []
    for line in lines:
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            out.append(value)
    return out


def _after(when_text, since: dt.datetime | None) -> bool:
    when = parse_et(when_text)
    return since is None or (when is not None and when >= since)


# ---------------------------------------------------------------- ib cache readers

def _safe(fn, default):
    try:
        return fn()
    except Exception:  # noqa: BLE001 - telemetry must never raise into the bot
        return default


def mask_account(account: str) -> str:
    account = str(account or "")
    return f"{account[:2]}…{account[-3:]}" if len(account) > 5 else "…"


ACCOUNT_TAGS = ("NetLiquidation", "BuyingPower", "AvailableFunds", "ExcessLiquidity",
                "GrossPositionValue")


def pnl_decision() -> dict | None:
    """The daily P&L the risk guard used in this process, if any (trading/account.py)."""
    from paperbot.trading import account as trading_account
    return trading_account.last_daily_pnl()


def fx_conversion() -> dict | None:
    """The base-currency conversion the bot made in this process, if any."""
    from paperbot.trading import account as trading_account
    return trading_account.last_conversion()


def saved_conversion(currency: str, base_value: float) -> dict | None:
    """The bot's saved rate (logs/fx_rate.json), for passes in which it did not need the
    account value (e.g. after the entry window). Reads the file only; never the network."""
    from paperbot.trading import account as trading_account
    if trading_account.fallback_mode() == "off":
        return None
    saved = trading_account._saved_rate(currency)
    rate = _float(saved.get("rate"))
    try:
        age = (dt.datetime.now(ET).date() - dt.date.fromisoformat(saved.get("date", ""))).days
    except ValueError:
        return None
    if not rate or age > trading_account.MAX_RATE_AGE_DAYS:
        return None
    return {"usd": round(base_value / rate, 2), "currency": currency, "rate": rate,
            "source": f"saved rate {saved['date']}; not needed by the bot this pass"}


def account_snapshot(ib, account: str) -> dict:
    """USD values (the currency the bot reads) plus the account's base currency values."""
    values, other, currency = {}, {}, None
    for v in _safe(lambda: list(ib.accountValues(account)), []):
        if v.tag not in ACCOUNT_TAGS:
            continue
        if v.currency == "USD":
            values[v.tag] = _float(v.value)
        elif v.currency and v.currency != "BASE":
            other[v.tag], currency = _float(v.value), v.currency
    pnl = next(iter(_safe(lambda: list(ib.pnl(account)), [])), None)
    nlv, bp, fx = values.get("NetLiquidation"), values.get("BuyingPower"), None
    if nlv is None and other.get("NetLiquidation") is not None:
        # the value the bot used this pass (trading/account.py), from memory only
        conv = _safe(fx_conversion, None) or _safe(lambda: saved_conversion(currency, other["NetLiquidation"]), None)
        if conv and conv.get("currency") == currency:
            fx = {"currency": currency, "rate": conv.get("rate"), "source": conv.get("source")}
            if conv.get("usd") is not None and conv.get("rate"):
                nlv = conv["usd"]
                bp = round(other["BuyingPower"] / conv["rate"], 2) if other.get("BuyingPower") else None
    return {"id_masked": mask_account(account),
            "base_currency": "USD" if values else currency,
            "net_liquidation_base": values.get("NetLiquidation", other.get("NetLiquidation")),
            "buying_power_base": values.get("BuyingPower", other.get("BuyingPower")),
            "net_liquidation": nlv,
            "buying_power": bp,
            "fx": fx,
            "available_funds": values.get("AvailableFunds"),
            "excess_liquidity": values.get("ExcessLiquidity"),
            "gross_position_value": values.get("GrossPositionValue"),
            "daily_pnl": _guard_pnl(_float(getattr(pnl, "dailyPnL", None))),
            "daily_pnl_source": (_safe(pnl_decision, None) or {}).get("source"),
            "unrealized_pnl": _float(getattr(pnl, "unrealizedPnL", None)),
            "realized_pnl": _float(getattr(pnl, "realizedPnL", None))}


def _guard_pnl(ibkr: float | None) -> float | None:
    """IBKR's figure, else the value the guard used (0.0 when the account is flat)."""
    if ibkr is not None:
        return ibkr
    decision = _safe(pnl_decision, None)
    return decision.get("value") if decision else None


def order_snapshot(trade) -> dict:
    order, status = trade.order, trade.orderStatus
    return {"order_id": order.orderId, "parent_id": order.parentId or None,
            "symbol": trade.contract.symbol.upper(), "role": ROLES.get(order.orderRef, "manual"),
            "action": order.action, "type": order.orderType,
            "qty": _float(order.totalQuantity), "filled": _float(status.filled),
            "remaining": _float(status.remaining),
            "limit": _float(order.lmtPrice) if order.orderType == "LMT" else None,
            "aux": _float(order.auxPrice) if order.orderType in ("STP", "STP LMT") else None,
            "tif": order.tif, "status": status.status}


def positions_snapshot(ib, account: str, strategy_state: dict, exit_rules: dict) -> list[dict]:
    owned = strategy_state.get("positions", {}) if isinstance(strategy_state, dict) else {}
    out = []
    for item in _safe(lambda: list(ib.portfolio(account)), []):
        if item.contract.secType != "STK" or item.position <= 0:
            continue
        symbol = item.contract.symbol.upper()
        st = owned.get(symbol, {})
        avg, price = _float(item.averageCost), _float(item.marketPrice)
        initial = _float(st.get("initial_stop"))
        risk = (avg - initial) if avg and initial and avg > initial else None
        triggers = {}
        if risk:
            if not st.get("partial_done"):
                triggers["partial"] = round(avg + exit_rules.get("partial_profit_trigger_R", 0.75) * risk, 2)
            if not st.get("breakeven"):
                triggers["breakeven"] = round(avg + exit_rules.get("breakeven_trigger_R", 1.0) * risk, 2)
        out.append({"symbol": symbol, "qty": float(item.position), "avg_cost": avg,
                    "market_price": price if price and price > 0 else None,
                    "unrealized_pnl": _float(item.unrealizedPNL),
                    "managed": symbol in owned, "stop": _float(st.get("stop")),
                    "initial_stop": initial, "breakeven": bool(st.get("breakeven")),
                    "partial_done": bool(st.get("partial_done")),
                    "force_attempts": int(st.get("force_attempts", 0) or 0),
                    "r_now": round((price - avg) / risk, 2) if risk and price else None,
                    "triggers": triggers})
    return out


# ---------------------------------------------------------------- validations

def _check(key, label, status, value=None, limit=None, enforced=True, detail=""):
    return {"key": key, "label": label, "status": status, "value": value, "limit": limit,
            "enforced": enforced, "detail": detail}


def validations(acct: dict, risk_state: dict, limits: dict, positions: list[dict],
                orders: list[dict], all_open: list[dict], evaluations: list[dict],
                log_lines: list[dict], drawdown: float | None, watchlist_fresh: bool,
                current_phase: str, stop_file: bool) -> list[dict]:
    """The checks the bot enforces (enforced=True) plus monitor-only ones."""
    out = []
    daily, nlv = acct.get("daily_pnl"), acct.get("net_liquidation")
    max_loss = abs(float(limits.get("max_daily_loss") or 0))
    source = acct.get("daily_pnl_source") or ""
    out.append(_check("pnl_available", "Daily P&L available", "pass" if daily is not None else "fail",
                      daily, None, True,
                      (source if source != "IBKR" else "") if daily is not None else
                      f"{source or 'IBKR sent no daily P&L (NaN)'}; entries are blocked without it"))
    if risk_state.get("halted"):
        out.append(_check("daily_loss", "Daily loss limit", "fail", daily, -max_loss, True,
                          risk_state.get("halt_reason") or "halted"))
    elif daily is None:
        out.append(_check("daily_loss", "Daily loss limit", "skip", None, -max_loss, True, "no P&L"))
    else:
        status = "warn" if max_loss and daily <= -0.5 * max_loss else "pass"
        out.append(_check("daily_loss", "Daily loss limit", status, round(daily, 2), -max_loss, True,
                          f"{daily / max_loss * -100:.0f}% of limit used" if max_loss and daily < 0 else ""))
    if drawdown is not None:
        pct = drawdown / nlv * 100 if nlv else None
        status = "warn" if max_loss and drawdown >= 0.5 * max_loss else "pass"
        out.append(_check("drawdown", "Intraday drawdown from P&L peak", status, round(drawdown, 2),
                          max_loss, False, (f"{pct:.3f}% of net liquidation; " if pct is not None else "")
                          + "monitor only, the bot enforces the daily loss limit"))
    base, base_ccy, fx = acct.get("net_liquidation_base"), acct.get("base_currency"), acct.get("fx")
    if nlv and fx:
        detail = (f"converted from {base_ccy} {base:,.2f} at {fx['rate']:.4f} {base_ccy} per USD "
                  f"({fx['source']})")
    elif nlv:
        detail = ""
    elif base and fx:
        detail = (f"account reports {base_ccy} {base:,.2f}; conversion to USD failed: {fx['source']}. "
                  "The entry scan is skipped and entries are blocked")
    elif base:
        detail = (f"account reports {base_ccy} {base:,.2f} but no USD value; the bot reads USD only, "
                  "so the entry scan is skipped and entries are blocked")
    else:
        detail = "not reported yet; the entry scan is skipped and entries are blocked"
    out.append(_check("net_liq", "Net liquidation (USD) available", "pass" if nlv else "fail", nlv,
                      None, True, detail))
    bp, max_value = acct.get("buying_power"), float(limits.get("max_order_value") or 0)
    if bp is None:
        out.append(_check("buying_power", "Buying power", "skip", None, max_value, False, "not reported"))
    else:
        out.append(_check("buying_power", "Buying power", "pass" if bp >= max_value else "warn", bp,
                          max_value, False, "monitor only; compared with max order value"))
    done, cap = int(risk_state.get("orders_today") or 0), int(limits.get("max_orders_per_day") or 0)
    out.append(_check("entries_today", "Entries sent today", "pass" if done < cap else "warn",
                      done, cap, True, "" if done < cap else "entry cap reached for today"))
    working = sum(1 for o in all_open if o["role"] != "stop")
    cap = int(limits.get("max_open_orders") or 0)
    out.append(_check("open_orders", "Working entry/exit orders", "pass" if working < cap else "warn",
                      working, cap, True, "" if working < cap else "new entries blocked"))
    busy = {p["symbol"] for p in positions} | {o["symbol"] for o in all_open
                                                if o["action"] == "BUY" and o["role"] != "stop"}
    cap = int(limits.get("max_concurrent_positions") or 0)
    if cap:
        out.append(_check("concurrency", "Concurrent positions", "pass" if len(busy) < cap else "warn",
                          len(busy), cap, True, "" if len(busy) < cap else "no new symbols"))
    uncovered = []
    for p in positions:
        if not p["managed"]:
            continue
        mine = [o for o in orders if o["symbol"] == p["symbol"]]
        if any(o["role"] == "entry" for o in mine):
            continue
        covered = sum((o["remaining"] or 0) for o in mine if o["role"] in ("stop", "exit"))
        if covered + 1e-9 < p["qty"]:
            uncovered.append(p["symbol"])
    managed = sum(1 for p in positions if p["managed"])
    out.append(_check("stop_coverage", "Protective stop coverage",
                      "fail" if uncovered else ("pass" if managed else "skip"),
                      managed - len(uncovered), managed, True,
                      ("uncovered: " + ", ".join(uncovered)) if uncovered else
                      ("" if managed else "no managed positions")))
    data_fail = [e["symbol"] for e in evaluations
                 if e["check"] in ("contract", "daily_history", "bars")]
    no_price = [l["msg"] for l in log_lines if "no price data" in l["msg"]]
    status = "warn" if data_fail or no_price else ("pass" if evaluations else "skip")
    out.append(_check("data_sanity", "Market data sanity", status, len(data_fail) + len(no_price),
                      0, True, "; ".join((["data missing: " + ", ".join(data_fail)] if data_fail else [])
                                         + no_price[:3]) or
                      ("completed 5m bars only; history and bars present" if evaluations
                       else "no symbols evaluated in this pass")))
    out.append(_check("quotes", "Bid/ask spread & quote freshness", "skip", None, None, False,
                      "not performed by the strategy (it uses completed 5m bar closes)"))
    if current_phase in ("opening", "entry_window"):
        out.append(_check("watchlist", "Today's watchlist", "pass" if watchlist_fresh else "warn",
                          None, None, False, "" if watchlist_fresh else
                          "watchlist.txt not generated today; scanning universe.txt"))
    out.append(_check("stop_file", "STOP file", "warn" if stop_file else "pass", None, None, True,
                      "present: runs skip / bot shuts down" if stop_file else "absent"))
    alerts = [l for l in log_lines if l["level"] in ("ERROR", "CRITICAL") or "ALERT" in l["msg"]]
    out.append(_check("alerts", "Alerts in this pass", "warn" if alerts else "pass", len(alerts), 0,
                      False, "; ".join(a["msg"][:120] for a in alerts[:3])))
    return out


# ---------------------------------------------------------------- record

def _load_json(path: Path, default):
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, type(default)) else default
    except (OSError, ValueError):
        return default


def _watchlist() -> tuple[str, list[str], bool]:
    """(source, symbols, generated_today), as strategy._universe() picks them."""
    from paperbot.trading import strategy
    today = dt.datetime.now(ET).date()
    try:
        if strategy.watchlist_date() == today:
            return "watchlist", strategy.read_symbols(strategy.WATCHLIST_FILE), True
        return "universe", strategy.read_symbols(strategy.UNIVERSE_FILE), False
    except OSError:
        return "none", [], False


def record_pass(ib, account: str, source: str, submit: bool,
                now: dt.datetime | None = None) -> dict:
    """Append one telemetry line for the pass that just finished. Returns it."""
    now = now or dt.datetime.now(ET)
    today = now.date().isoformat()
    try:
        state = runstate.read_json_state(STATE_FILE) or {}
    except runstate.StateCorrupt:
        state = {}  # worst case: some lines of this pass are attributed to the next one
    if state.get("date") != today:
        state = {"date": today, "offsets": state.get("offsets", {}), "peak_pnl": None,
                 "run": {}, "passes": 0}
    offsets = state.setdefault("offsets", {})
    last_end = parse_et(state.get("last_end"))
    since_first = now - FIRST_READ_WINDOW

    raw_log, first = read_new_lines(BOT_LOG, "bot.log", offsets)
    lines = [p for p in (parse_log_line(l) for l in raw_log) if p]
    if first:
        lines = [l for l in lines if _after(l["time"], since_first)]
    raw_safety, first = read_new_lines(SAFETY_LOG, "safety_log", offsets)
    evals = [evaluation(r) for r in _json_lines(raw_safety)
             if not first or _after(r.get("time"), since_first)]
    raw_orders, first = read_new_lines(ORDERS_LOG, "orders", offsets)
    events = [order_event(r) for r in _json_lines(raw_orders)
              if not first or _after(r.get("time"), since_first)]

    trades = _safe(lambda: list(ib.trades()), [])
    names = {t.order.orderId: (t.contract.symbol.upper(), ROLES.get(t.order.orderRef, "manual"))
             for t in trades if _safe(lambda: t.order.orderId, None) is not None}
    for e in events:
        known = next((names[i] for i in e["order_ids"] if i in names), None)
        if known:
            e["symbol"] = e["symbol"] or known[0]
            e["role"] = known[1]
    open_trades = [t for t in _safe(lambda: list(ib.openTrades()), []) if not t.isDone()]
    all_open = [order_snapshot(t) for t in open_trades]
    ours = [o for o in all_open if o["role"] != "manual"]

    rules = _load_json(paths.RULES_FILE, {})
    config = _load_json(paths.CONFIG_FILE, {})
    limits = {**config.get("limits", {}), **rules.get("risk", {})}
    strategy_state = _load_json(STRATEGY_STATE, {})
    risk_state = _load_json(RISK_STATE, {})
    acct = account_snapshot(ib, account)
    positions = positions_snapshot(ib, account, strategy_state, rules.get("exit", {}))

    daily = acct["daily_pnl"]
    peak = state.get("peak_pnl")
    if daily is not None:
        peak = daily if peak is None else max(peak, daily)
        state["peak_pnl"] = peak
    drawdown = round(max(0.0, peak - daily), 2) if daily is not None and peak is not None else None

    times = session_times(rules)
    current_phase = phase(now, times)
    scan_source, symbols, fresh = _watchlist()

    pid = os.getpid()
    run = state.get("run") or {}
    if run.get("pid") == pid:
        run["pass"] = int(run.get("pass", 0)) + 1
    else:
        run = {"pid": pid, "pass": 1,
               "id": f"{'cycle' if source == 'cycle.py' else 'bot'}-{pid}-{now:%H%M%S}"}
    started = None
    if run["pass"] == 1:
        start_line = next((l for l in reversed(lines)
                           if l["msg"].startswith(("cycle start", "connected to paper"))), None)
        started = parse_et(start_line["time"]) if start_line else None
        if started:  # earlier lines belong to earlier runs (skips, failed connects)
            lines = [l for l in lines if parse_et(l["time"]) >= started]
    elif lines:
        started = parse_et(lines[0]["time"])
    paused = next((l["msg"] for l in lines if l["msg"].startswith("entry scan paused")), None)
    signals = [l["msg"][len("signal "):] for l in lines if l["msg"].startswith("signal ")]
    state["run"] = run
    state["passes"] = int(state.get("passes", 0)) + 1

    record = {
        "v": SCHEMA_VERSION,
        "run_id": run["id"], "pass_in_run": run["pass"], "pass_today": state["passes"],
        "source": source, "mode": "SUBMIT" if submit else "DRY RUN", "pid": pid,
        "window_start": last_end.isoformat(timespec="seconds") if last_end else None,
        "started": started.isoformat(timespec="seconds") if started else None,
        "ended": now.isoformat(timespec="seconds"),
        "duration_s": round((now - started).total_seconds(), 1) if started else None,
        "phase": current_phase,
        "account": acct,
        "risk": {"orders_today": int(risk_state.get("orders_today") or 0),
                 "halted": bool(risk_state.get("halted")),
                 "halt_reason": risk_state.get("halt_reason") or "",
                 "peak_pnl": peak, "drawdown_from_peak": drawdown},
        "positions": positions,
        "orders": ours,
        "scan": {"source": scan_source, "size": len(symbols), "fresh": fresh,
                 "in_window": current_phase == "entry_window",
                 "evaluated": len(evals), "passed": sum(1 for e in evals if e["pass"]),
                 "paused": paused},
        "evaluations": evals,
        "signals": signals,
        "order_events": events,
        "requests": sum(1 for e in events if e["event"] in ("sent", "stop", "cancel", "dry_run",
                                                              "blocked")),
        "validations": validations(acct, risk_state, limits, positions, ours, all_open, evals,
                                   lines, drawdown, fresh, current_phase,
                                   runstate.stop_requested()),
        "log": [l for l in lines if l["level"] != "INFO" or KEPT_INFO.search(l["msg"])][-200:],
    }
    TELEMETRY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(TELEMETRY_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=str, separators=(",", ":")) + "\n")
    state["last_end"] = record["ended"]
    runstate.write_json_atomic(STATE_FILE, state)
    return record
