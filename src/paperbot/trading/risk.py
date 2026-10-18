"""Risk limits for the paper bot. Every request from the strategy passes here first.

The strategy returns three kinds of requests, all defined in this module:

  OrderIntent    a BUY entry (sent as a bracket with a protective stop) or a
                 closing SELL limit order (exit)
  StopUpdate     place or modify the protective stop held at IBKR
  CancelRequest  cancel one of the bot's own working orders

RiskGuard.check() approves OrderIntents and RiskGuard.check_stop() approves
StopUpdates. Entry limits (symbols, size, order counts, daily-loss halt and
the rules.json risk section) apply only to BUYs. Exits and stops only have to
be valid and no larger than the shares held, so they are never blocked by
entry limits or a halt, and can never turn a long position into a short.

Daily counters live in logs/state.json, written atomically and read fail
closed (see runstate.py), and roll over at midnight US Eastern Time. This
module never connects to IBKR.
"""
import datetime as dt
import json
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

# orderRef tags, so the bot only ever manages its own orders
REF_ENTRY, REF_STOP, REF_EXIT = "paperbot:entry", "paperbot:stop", "paperbot:exit"


@dataclass
class Limits:
    allowed_symbols: list[str]  # ["*"] = any symbol
    max_quantity_per_order: int
    max_order_value: float
    max_orders_per_day: int  # entries (BUYs) sent per day
    max_open_orders: int  # working entry/exit orders; protective stops not counted
    max_daily_loss: float
    limit_orders_only: bool = True  # applies to entries/exits; protective stops are STP
    # From the "risk" section of rules.json; None = not enforced.
    max_risk_per_trade_pct: float | None = None
    max_position_size_pct_of_portfolio: float | None = None
    max_concurrent_positions: int | None = None

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

    def concurrent_positions(self) -> int:
        return len(set(self.long_positions) | self.open_buy_symbols)


def load_limits(config: dict, rules_path: Path) -> Limits:
    """Limits from config.json, plus the "risk" section of rules.json if present."""
    risk = {}
    if rules_path.exists():
        risk = json.loads(rules_path.read_text()).get("risk", {})
    return Limits(**{**config["limits"], **risk})  # rules.json wins on overlap


def verify_paper(port: int, accounts: list[str]) -> str:
    """Return the paper account ID, or stop if anything looks like a live account."""
    if port not in PAPER_PORTS:
        raise SystemExit(f"Refusing to run: port {port} is not a paper port {PAPER_PORTS}")
    if len(accounts) != 1 or not accounts[0].startswith("D"):
        raise SystemExit(f"Refusing to run: expected one paper account, got {accounts}")
    return accounts[0]


class RiskGuard:
    """Daily order count, daily-loss halt and per-order checks.

    The state file is loaded fail closed: a corrupt file raises StateCorrupt
    instead of resetting today's count or halt. "Today" is the ET date.
    """
    STATE_KEYS = {"date": str, "orders_today": int, "halted": bool, "halt_reason": str}

    def __init__(self, limits: Limits, state_path: Path, today=today_et):
        self.limits = limits
        self.state_path = state_path
        self._today = today
        self.state = self._load()

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
        self._roll_day()
        return self.state["halted"]

    def halt(self, reason: str):
        self.state["halted"] = True
        self.state["halt_reason"] = reason
        self._save()

    def update_daily_pnl(self, daily_pnl: float | None):
        """Halt for the rest of the day once the daily loss limit is reached."""
        if daily_pnl is not None and daily_pnl <= -abs(self.limits.max_daily_loss):
            if not self.halted:
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
        if self.state["halted"]:
            problems.append(f"trading halted today: {self.state['halt_reason']}")
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
