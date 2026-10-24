"""Risk limits for the paper bot. Every request from the strategy passes here first.

The strategy returns three kinds of requests, all defined in this module:

  OrderIntent    a BUY entry (sent as a bracket with a protective stop) or a
                 closing SELL limit order (exit)
  StopUpdate     place or modify the protective stop held at IBKR
  CancelRequest  cancel one of the bot's own working orders

RiskGuard.check() approves OrderIntents and RiskGuard.check_stop() approves
StopUpdates. Entry limits (symbols, size, order counts, daily-loss halt,
drawdown breaker, buying power, commission drag and the rules.json risk
section) apply only to BUYs. Exits and stops only have to be valid and no
larger than the shares held, so they are never blocked by entry limits or a
halt, and can never turn a long position into a short.

Limits come from two files (load_limits): the safety rails allowed_symbols and
limit_orders_only from config.json "limits", every quantitative threshold from
the "risk" section of the chosen strategy in rules.json ("strategies.<key>.risk").
Both are type-checked at startup and a missing, invalid or misplaced key stops
the bot (fail closed).

Daily counters live in logs/state.json, written atomically and read fail
closed (see runstate.py), and roll over at midnight US Eastern Time. The
multi-day drawdown breaker (DrawdownBreaker) keeps its own file,
logs/drawdown_state.json, read fail closed too; once tripped it blocks
entries until reset by hand (paperbot-bot --reset-drawdown). This module
never connects to IBKR.
"""
import datetime as dt
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from paperbot.runtime.runstate import StateCorrupt, read_json_state, write_json_atomic

PAPER_PORTS = {4002, 7497}  # IB Gateway paper, TWS paper
ET = ZoneInfo("America/New_York")


def today_et(now: dt.datetime | None = None) -> dt.date:
    """The trading date in US Eastern Time, whatever time zone this PC is in.

    The daily counters must roll over at ET midnight; the PC's local date
    (e.g. Singapore) would reset them in the middle of the US session.
    """
    return (now or dt.datetime.now(ET)).astimezone(ET).date()

# orderRef tags, so the bot only ever manages its own orders. These are Trend Join
# Long's (order_ref "paperbot"); other strategies use "paperbot:<key>:<role>".
REF_ENTRY, REF_STOP, REF_EXIT = "paperbot:entry", "paperbot:stop", "paperbot:exit"
ROLES = ("entry", "stop", "exit")


def ref_role(ref) -> str | None:
    """"entry", "stop" or "exit" for any paperbot strategy's orderRef ("paperbot:stop",
    "paperbot:<key>:stop"); None for manual or other orders."""
    if not isinstance(ref, str) or not ref.startswith("paperbot:"):
        return None
    role = ref.rsplit(":", 1)[-1]
    return role if role in ROLES else None

# IBKR Fixed pricing for US stocks, per order: $0.005/share, min $1.00, max 1% of value
COMMISSION_PER_SHARE, COMMISSION_MIN, COMMISSION_MAX_PCT = 0.005, 1.00, 1.0


def round_trip_commission(quantity: int, price: float) -> float:
    """Estimated commission to buy and later sell `quantity` shares at `price`."""
    per_side = max(COMMISSION_MIN, COMMISSION_PER_SHARE * quantity)
    return 2 * min(per_side, quantity * price * COMMISSION_MAX_PCT / 100)


def commission_drag(quantity: int, limit: float, stop: float, max_pct: float | None) -> str | None:
    """Why the commission is too large a share of the trade's risk R, or None if it is fine.
    R = quantity x (limit - stop), the dollars lost if the protective stop fills."""
    if max_pct is None:
        return None
    risk = quantity * (limit - stop)
    fees = round_trip_commission(quantity, limit)
    if risk <= 0 or fees > risk * max_pct / 100:
        return (f"commission {fees:.2f} > {max_pct:g}% of trade risk R {max(risk, 0):.2f} "
                f"({quantity} shares)")
    return None


@dataclass
class Limits:
    # Safety rails from config.json "limits".
    allowed_symbols: list[str]  # ["*"] = any symbol
    # Required thresholds from the "risk" section of rules.json.
    max_quantity_per_order: int
    max_order_value: float
    max_orders_per_day: int  # entries (BUYs) sent per day
    max_open_orders: int  # working entry/exit orders; protective stops not counted
    max_daily_loss: float
    limit_orders_only: bool = True  # config.json; entries/exits only, protective stops are STP
    # Optional, from the "risk" section of rules.json; None = not enforced.
    max_risk_per_trade_pct: float | None = None
    max_position_size_pct_of_portfolio: float | None = None
    max_concurrent_positions: int | None = None
    max_commission_pct_of_risk: float | None = None  # round-trip fees vs. R
    max_drawdown_usd: float | None = None  # multi-day breaker (DrawdownBreaker)

    def allows_symbol(self, symbol: str) -> bool:
        allowed = {s.upper() for s in self.allowed_symbols}
        return "*" in allowed or symbol.upper() in allowed


@dataclass
class OrderIntent:
    symbol: str
    action: str  # "BUY" or "SELL"
    quantity: int
    limit_price: float | None  # None = market order
    reason: str = ""
    stop_price: float | None = None  # BUY: protective stop sent with the entry
    closing: bool = False  # True = SELL that reduces an existing long position
    good_till: dt.datetime | None = None  # BUY: IBKR expires it then (GTD) if still unfilled
    oca_group: str | None = None  # SELL: joins the protective stop's OCA group at IBKR


@dataclass
class StopUpdate:
    """Place the protective SELL STP for `symbol`, or modify the working one."""
    symbol: str
    quantity: int
    stop_price: float
    reason: str = ""


@dataclass
class CancelRequest:
    """Cancel one of the bot's own working orders."""
    order_id: int
    symbol: str
    reason: str = ""


@dataclass
class AccountState:
    """What the guard needs to know about the account before approving an order."""
    net_liquidation: float | None
    long_positions: dict[str, float] = field(default_factory=dict)  # SYMBOL -> shares
    open_buy_symbols: set[str] = field(default_factory=set)  # symbols with a working BUY
    buying_power: float | None = None  # USD; None = unknown, which blocks entries

    def concurrent_positions(self) -> int:
        return len(set(self.long_positions) | self.open_buy_symbols)


class ConfigError(ValueError):
    """config.json / rules.json limits are missing, misplaced or invalid."""


# Where each limit lives, and its type. config.json "limits" holds only the safety
# rails; every quantitative threshold lives in the "risk" section of rules.json.
CONFIG_LIMITS = {"allowed_symbols": list, "limit_orders_only": bool}
RULES_REQUIRED = {"max_quantity_per_order": int, "max_order_value": float,
                  "max_orders_per_day": int, "max_open_orders": int, "max_daily_loss": float}
RULES_OPTIONAL = {"max_risk_per_trade_pct": float, "max_position_size_pct_of_portfolio": float,
                  "max_concurrent_positions": int, "max_commission_pct_of_risk": float,
                  "max_drawdown_usd": float}


def _cast(where: str, key: str, value, kind):
    """value as `kind`, or ConfigError. JSON booleans are never accepted as numbers,
    numeric strings are never accepted at all, and every threshold must be > 0."""
    if kind is list:
        if not (isinstance(value, list) and value
                and all(isinstance(s, str) and s.strip() for s in value)):
            raise ConfigError(f"{where} {key} must be a non-empty list of symbols, got {value!r}")
        return [s.strip() for s in value]
    if kind is bool:
        if not isinstance(value, bool):
            raise ConfigError(f"{where} {key} must be true or false, got {value!r}")
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where} {key} must be a number, got {value!r}")
    if kind is int and value != int(value):
        raise ConfigError(f"{where} {key} must be a whole number, got {value!r}")
    if value <= 0:
        raise ConfigError(f"{where} {key} must be greater than 0, got {value!r}")
    return kind(value)


def strategy_section(rules: dict, strategy: str, rules_name: str = "rules.json") -> dict:
    """The rules.json "strategies.<strategy>" section, or ConfigError."""
    strategies = rules.get("strategies")
    if not isinstance(strategies, dict) or not strategies:
        raise ConfigError(f'{rules_name} needs a "strategies" object with at least one strategy')
    if strategy not in strategies:
        raise ConfigError(f"{rules_name} has no strategy {strategy!r}; "
                          f"available: {', '.join(sorted(strategies))}")
    section = strategies[strategy]
    if not isinstance(section, dict):
        raise ConfigError(f"{rules_name} strategies.{strategy} must be an object")
    return section


def load_limits(config: dict, rules_path: Path, strategy: str) -> Limits:
    """Limits from the safety rails in config.json "limits" (CONFIG_LIMITS) and the
    thresholds in the "risk" section of one strategy in rules.json
    ("strategies.<strategy>.risk": RULES_REQUIRED, RULES_OPTIONAL).

    Fails closed with ConfigError: a missing required key, a wrong type, a value
    <= 0, an unknown key, or a threshold still in config.json (one source per cap,
    so a stale config.json can never silently override rules.json). There are no
    defaults for the caps; only limit_orders_only defaults to true. Optional
    rules.json keys may be null (not enforced).
    """
    cfg_limits = config.get("limits")
    if not isinstance(cfg_limits, dict):
        raise ConfigError('config.json needs a "limits" object with allowed_symbols')
    misplaced = sorted(k for k in cfg_limits if k in RULES_REQUIRED or k in RULES_OPTIONAL)
    if misplaced:
        raise ConfigError(f"config.json limits still contains {misplaced}; these now live in each "
                          f'strategy\'s "risk" section of {rules_path.name}. Move them there and restart.')
    unknown = sorted(set(cfg_limits) - set(CONFIG_LIMITS))
    if unknown:
        raise ConfigError(f"config.json limits has unknown keys {unknown}")
    try:
        rules = json.loads(rules_path.read_text())
    except (OSError, ValueError) as e:
        raise ConfigError(f"cannot read {rules_path.name}: {e}") from None
    where = f"{rules_path.name} strategies.{strategy}.risk"
    risk = strategy_section(rules, strategy, rules_path.name).get("risk")
    if not isinstance(risk, dict):
        raise ConfigError(f'{where} must be an object')
    unknown = sorted(set(risk) - set(RULES_REQUIRED) - set(RULES_OPTIONAL))
    if unknown:
        raise ConfigError(f"{where} has unknown keys {unknown}")
    missing = sorted(k for k in RULES_REQUIRED if k not in risk)
    if missing:
        raise ConfigError(f"{where} is missing {missing}")
    if "allowed_symbols" not in cfg_limits:
        raise ConfigError("config.json limits is missing allowed_symbols")

    values = {k: _cast("config.json limits", k, v, CONFIG_LIMITS[k]) for k, v in cfg_limits.items()}
    values.update({k: _cast(where, k, risk[k], kind) for k, kind in RULES_REQUIRED.items()})
    values.update({k: None if risk[k] is None else _cast(where, k, risk[k], kind)
                   for k, kind in RULES_OPTIONAL.items() if k in risk})
    return Limits(**values)


def require_limits(config: dict, rules_path: Path, strategy: str) -> Limits:
    """load_limits() for a process that is starting: a ConfigError is logged as an
    ALERT (pushed by notify.py) and stops the process before it connects or trades."""
    try:
        return load_limits(config, rules_path, strategy)
    except ConfigError as e:
        logging.getLogger("paperbot").error("ALERT refusing to start: %s", e)
        raise SystemExit(f"Refusing to start: {e}") from None


def verify_paper(port: int, accounts: list[str]) -> str:
    """Return the paper account ID, or stop if anything looks like a live account."""
    if port not in PAPER_PORTS:
        raise SystemExit(f"Refusing to run: port {port} is not a paper port {PAPER_PORTS}")
    if len(accounts) != 1 or not accounts[0].startswith("D"):
        raise SystemExit(f"Refusing to run: expected one paper account, got {accounts}")
    return accounts[0]


class DrawdownBreaker:
    """Multi-day circuit breaker on the cumulative daily P&L.

    Every update stores today's IBKR daily P&L under today's ET date, so the
    last value of each day is kept. The cumulative sum of all days is the
    equity curve; when it falls max_drawdown_usd or more below its peak the
    breaker trips and stays tripped across days and restarts until reset()
    (paperbot-bot --reset-drawdown). Daily P&L is used instead of net
    liquidation so FX moves of a non-USD account and deposits cannot trip or
    hide it. The file is written atomically and read fail closed.
    """

    def __init__(self, path: Path, max_drawdown_usd: float, today=today_et):
        self.path = path
        self.max_drawdown = abs(max_drawdown_usd)
        self._today = today
        state = read_json_state(path)
        if state is None:
            state = {"days": {}, "peak": 0.0, "tripped": False, "reason": ""}
        elif (not isinstance(state.get("days"), dict) or not isinstance(state.get("tripped"), bool)
              or not isinstance(state.get("peak"), (int, float))):
            raise StateCorrupt(path, "days/peak/tripped missing or invalid")
        self.state = state

    @property
    def tripped(self) -> bool:
        return self.state["tripped"]

    @property
    def reason(self) -> str:
        return self.state.get("reason", "")

    def cumulative(self) -> float:
        return sum(float(v) for v in self.state["days"].values())

    def update(self, daily_pnl: float | None) -> bool:
        """Record today's P&L; returns True if this update tripped the breaker."""
        if daily_pnl is None:
            return False
        day = self._today().isoformat()
        if self.state["days"].get(day) == daily_pnl:
            return False  # nothing changed: no write
        self.state["days"][day] = daily_pnl
        cum = self.cumulative()
        self.state["peak"] = max(float(self.state["peak"]), cum)
        drawdown = self.state["peak"] - cum
        newly = False
        if not self.state["tripped"] and drawdown >= self.max_drawdown:
            self.state["tripped"], newly = True, True
            self.state["reason"] = (f"drawdown {drawdown:.2f} from peak cumulative P&L "
                                    f"{self.state['peak']:.2f} reached limit {self.max_drawdown:.2f}")
            self.state["tripped_at"] = dt.datetime.now(ET).isoformat(timespec="seconds")
        write_json_atomic(self.path, self.state)
        return newly

    def reset(self):
        """Re-arm after a trip: the current cumulative P&L becomes the new peak."""
        self.state.update({"tripped": False, "reason": "", "peak": self.cumulative()})
        self.state.pop("tripped_at", None)
        write_json_atomic(self.path, self.state)


class RiskGuard:
    """Daily order count, daily-loss halt, drawdown breaker and per-order checks.

    The state file is loaded fail closed: a corrupt file raises StateCorrupt
    instead of resetting today's count or halt. "Today" is the ET date.
    The drawdown breaker is active when limits.max_drawdown_usd is set and a
    drawdown_path is given.
    """
    STATE_KEYS = {"date": str, "orders_today": int, "halted": bool, "halt_reason": str}

    def __init__(self, limits: Limits, state_path: Path, today=today_et,
                 drawdown_path: Path | None = None):
        self.limits = limits
        self.state_path = state_path
        self._today = today
        self.state = self._load()
        self.breaker = (DrawdownBreaker(drawdown_path, limits.max_drawdown_usd, today)
                        if drawdown_path is not None and limits.max_drawdown_usd else None)

    def _load(self) -> dict:
        today = self._today().isoformat()
        state = read_json_state(self.state_path)
        if state is not None:
            for key, kind in self.STATE_KEYS.items():
                if not isinstance(state.get(key), kind):
                    raise StateCorrupt(self.state_path, f"missing or invalid {key!r}")
        if state is None or state["date"] != today:
            state = {"date": today, "orders_today": 0, "halted": False, "halt_reason": ""}
        return state

    def _save(self):
        write_json_atomic(self.state_path, self.state)

    def _roll_day(self):
        if self.state["date"] != self._today().isoformat():
            self.state = self._load()
            self._save()

    @property
    def halted(self) -> bool:
        """True while entries are stopped: today's loss halt or a tripped drawdown breaker."""
        self._roll_day()
        return self.state["halted"] or bool(self.breaker and self.breaker.tripped)

    @property
    def halt_reason(self) -> str:
        if self.state["halted"]:
            return self.state["halt_reason"]
        return self.breaker.reason if self.breaker and self.breaker.tripped else ""

    def halt(self, reason: str):
        self.state["halted"] = True
        self.state["halt_reason"] = reason
        self._save()

    def update_daily_pnl(self, daily_pnl: float | None):
        """Halt for the rest of the day once the daily loss limit is reached, and feed
        the multi-day drawdown breaker."""
        if self.breaker is not None:
            self.breaker.update(daily_pnl)
        if daily_pnl is not None and daily_pnl <= -abs(self.limits.max_daily_loss):
            if not self.state["halted"]:
                self.halt(f"daily loss {daily_pnl:.2f} reached limit "
                          f"-{abs(self.limits.max_daily_loss):.2f}")

    def check(self, intent: OrderIntent, open_orders: int,
              daily_pnl: float | None, account: AccountState | None = None) -> list[str]:
        """Return the reasons this order is blocked (empty list = allowed).

        open_orders counts working entry/exit orders (not protective stops).
        Closing orders (SELLs that reduce a long position) only have to be valid
        and no larger than the position, so exits are never blocked by the
        entry limits, the order counters or a daily-loss halt.
        """
        self._roll_day()
        self.update_daily_pnl(daily_pnl)
        lim, problems = self.limits, []

        if intent.action not in ("BUY", "SELL"):
            problems.append(f"invalid action {intent.action!r}")
        if not isinstance(intent.quantity, int) or intent.quantity <= 0:
            problems.append(f"invalid quantity {intent.quantity!r}")
        if intent.limit_price is None:
            if lim.limit_orders_only:
                problems.append("market orders not allowed (limit_orders_only)")
        elif intent.limit_price <= 0:
            problems.append(f"invalid limit price {intent.limit_price}")

        if intent.closing:
            if intent.action != "SELL":
                problems.append("closing orders must be SELL (long only)")
            problems += _exceeds_position(intent.symbol, intent.quantity, account)
            return problems

        if intent.action == "SELL":
            problems.append("opening SELL would go short (long only); mark exits closing=True")
        if intent.action == "BUY" and not _valid_stop(intent):
            problems.append("BUY needs a protective stop_price above 0 and below the limit price")
        if intent.good_till is not None and dt.datetime.now(ET) >= intent.good_till:
            problems.append(f"entry window closed (good till {intent.good_till:%H:%M:%S} ET)")
        if self.state["halted"]:
            problems.append(f"trading halted today: {self.state['halt_reason']}")
        if self.breaker is not None and self.breaker.tripped:
            problems.append(f"drawdown breaker tripped: {self.breaker.reason} "
                            "(reset with paperbot-bot --reset-drawdown)")
        if daily_pnl is None:
            problems.append("daily P&L unavailable, cannot check loss limit")
        if not lim.allows_symbol(intent.symbol):
            problems.append(f"{intent.symbol} not in allowed symbols {lim.allowed_symbols}")
        if isinstance(intent.quantity, int) and intent.quantity > lim.max_quantity_per_order:
            problems.append(f"quantity {intent.quantity} > max {lim.max_quantity_per_order}")
        if (intent.limit_price is not None and intent.limit_price > 0
                and isinstance(intent.quantity, int)):
            value = intent.quantity * intent.limit_price
            if value > lim.max_order_value:
                problems.append(f"order value {value:.2f} > max {lim.max_order_value:.2f}")
        if self.state["orders_today"] >= lim.max_orders_per_day:
            problems.append(f"already sent {self.state['orders_today']} entries today "
                            f"(max {lim.max_orders_per_day})")
        if open_orders >= lim.max_open_orders:
            problems.append(f"{open_orders} open orders (max {lim.max_open_orders})")
        if intent.action == "BUY":
            problems += self._check_rules_risk(intent, account)
            problems += self._check_costs(intent, account)
        return problems

    def check_stop(self, update: StopUpdate, account: AccountState | None) -> list[str]:
        """Approve a protective stop: valid price, and never more shares than held."""
        problems = []
        if not isinstance(update.quantity, int) or update.quantity <= 0:
            problems.append(f"invalid stop quantity {update.quantity!r}")
        if update.stop_price is None or update.stop_price <= 0:
            problems.append(f"invalid stop price {update.stop_price!r}")
        return problems + _exceeds_position(update.symbol, update.quantity, account)

    def _check_rules_risk(self, intent: OrderIntent, account: AccountState | None) -> list[str]:
        """Position sizing and concurrency limits from the "risk" section of rules.json."""
        lim, problems = self.limits, []
        if (lim.max_risk_per_trade_pct is None
                and lim.max_position_size_pct_of_portfolio is None
                and lim.max_concurrent_positions is None):
            return problems
        if account is None or not account.net_liquidation or account.net_liquidation <= 0:
            return ["account value unavailable, cannot check position sizing"]
        if not isinstance(intent.quantity, int) or intent.quantity <= 0:
            return problems  # already reported
        if intent.limit_price is None or intent.limit_price <= 0:
            return ["no valid limit price, cannot check position sizing"]

        net_liq, price = account.net_liquidation, intent.limit_price
        if lim.max_position_size_pct_of_portfolio is not None:
            value = intent.quantity * price
            cap = net_liq * lim.max_position_size_pct_of_portfolio / 100
            if value > cap:
                problems.append(f"position value {value:.2f} > "
                                f"{lim.max_position_size_pct_of_portfolio:g}% of portfolio "
                                f"({cap:.2f})")
        if lim.max_risk_per_trade_pct is not None and _valid_stop(intent):
            risk = intent.quantity * (price - intent.stop_price)
            cap = net_liq * lim.max_risk_per_trade_pct / 100
            if risk > cap:
                problems.append(f"trade risk {risk:.2f} > {lim.max_risk_per_trade_pct:g}% "
                                f"of portfolio ({cap:.2f})")
        if lim.max_concurrent_positions is not None:
            symbol = intent.symbol.upper()
            is_new = (symbol not in account.long_positions
                      and symbol not in account.open_buy_symbols)
            count = account.concurrent_positions()
            if is_new and count >= lim.max_concurrent_positions:
                problems.append(f"{count} positions/entries open "
                                f"(max {lim.max_concurrent_positions})")
        return problems

    def _check_costs(self, intent: OrderIntent, account: AccountState | None) -> list[str]:
        """Buying power (when the account is known) and commission drag, for a BUY."""
        problems = []
        priced = (intent.limit_price is not None and intent.limit_price > 0
                  and isinstance(intent.quantity, int) and intent.quantity > 0)
        if account is not None and priced:
            value = intent.quantity * intent.limit_price
            if account.buying_power is None:
                problems.append("buying power unavailable, cannot check margin")
            elif value > account.buying_power:
                problems.append(f"order value {value:.2f} > buying power {account.buying_power:.2f}")
        if priced and _valid_stop(intent):
            drag = commission_drag(intent.quantity, intent.limit_price, intent.stop_price,
                                   self.limits.max_commission_pct_of_risk)
            if drag:
                problems.append(drag)
        return problems

    def record_order(self):
        """Count one entry against max_orders_per_day."""
        self._roll_day()
        self.state["orders_today"] += 1
        self._save()


def _valid_stop(intent: OrderIntent) -> bool:
    stop = intent.stop_price
    if stop is None or stop <= 0:
        return False
    return intent.limit_price is None or stop < intent.limit_price


def _exceeds_position(symbol: str, quantity, account: AccountState | None) -> list[str]:
    held = account.long_positions.get(symbol.upper(), 0) if account else 0
    if isinstance(quantity, int) and quantity > held:
        return [f"{quantity} shares but only {held:g} held"]
    return []
