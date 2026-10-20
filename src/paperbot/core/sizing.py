"""Position sizing and price rounding, shared by every strategy.

A strategy supplies only the entry signal (its stop) and the live ask; this
module turns that into whole shares and tick-rounded prices:

  shares   floor(min(risk %, position %, share cap, order-value cap, buying power)),
           whole shares only, so rounding can never exceed a limit; < 1 share = skip.
           Then the commission-drag check on the rounded share count.
  prices   to the $0.01 tick (to_tick).
  partial  split_shares(): nearest share, always leaving at least one.
"""
import datetime as dt
import math

from paperbot.core.common import log
from paperbot.core.risk import Limits, OrderIntent, commission_drag

LIMIT_SLIPPAGE = 0.002  # marketable limits: 0.2% through the quote (x attempt on force close)
TICK = 0.01  # US stocks and ETFs priced at $1 or more


def to_tick(price: float, side: str) -> float:
    """price on the $0.01 tick, rounded in the safe direction for `side`:
      "BUY"   limit rounds UP   (stays marketable through the ask)
      "SELL"  limit rounds DOWN (stays marketable through the bid)
      "STOP"  long stop rounds DOWN (never tighter than the rule asked for)
    A price already on the tick (within float noise) is left unchanged."""
    if side not in ("BUY", "SELL", "STOP"):
        raise ValueError(f"unknown side {side!r}")
    cents = price / TICK
    nearest = round(cents)
    if abs(cents - nearest) < 1e-6:  # on the tick: 100.2 is 10019.999999999998 cents
        return round(nearest * TICK, 2)
    step = math.ceil(cents) if side == "BUY" else math.floor(cents)
    return round(step * TICK, 2)


def split_shares(quantity: int, fraction: float) -> int:
    """Shares to sell for a partial exit: the nearest share (0.3333 of 9 = 3, not 2),
    always leaving a remainder. 0 means the position is too small to split."""
    return min(int(quantity * fraction + 0.5), quantity - 1)


def size_entry(symbol: str, signal: dict, limits: Limits, net_liq: float, buying_power: float,
               ask: float, reason: str, good_till: dt.datetime | None = None) -> OrderIntent | None:
    """A marketable BUY limit 0.2% through the live ask, sized to the smallest of the
    limits; None (logged) when below one share or when the commission is too large a
    share of the trade's R."""
    limit = to_tick(ask * (1 + LIMIT_SLIPPAGE), "BUY")
    per_share_risk = limit - signal["stop"]
    if per_share_risk <= 0:
        return None
    bounds = [limits.max_quantity_per_order, limits.max_order_value / limit, buying_power / limit]
    if limits.max_risk_per_trade_pct is not None:  # optional limits: None = not enforced
        bounds.append(net_liq * limits.max_risk_per_trade_pct / 100 / per_share_risk)
    if limits.max_position_size_pct_of_portfolio is not None:
        bounds.append(net_liq * limits.max_position_size_pct_of_portfolio / 100 / limit)
    qty = math.floor(min(bounds))
    if qty < 1:
        log.info("signal %s skipped: 1 share at %.2f exceeds the order caps or buying power "
                 "%.2f", symbol, limit, buying_power)
        return None
    drag = commission_drag(qty, limit, signal["stop"], limits.max_commission_pct_of_risk)
    if drag:
        log.info("signal %s skipped: %s", symbol, drag)
        return None
    return OrderIntent(symbol, "BUY", qty, limit, reason=reason, stop_price=signal["stop"],
                       good_till=good_till)


def closing_sell(symbol: str, quantity: int, price: float, reason: str,
                 slippage: float = LIMIT_SLIPPAGE, oca_group: str | None = None) -> OrderIntent:
    """A marketable SELL limit `slippage` below `price` that reduces a long position."""
    limit = to_tick(price * (1 - slippage), "SELL")
    return OrderIntent(symbol, "SELL", quantity, limit, reason=reason, closing=True,
                       oca_group=oca_group)
