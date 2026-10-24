"""Order and position machinery shared by every strategy.

Works on the strategy's own orders only (its orderRef tags, profile.refs) and
on its per-position state. Every function returns requests (risk.py) for the
runner to send through the risk guard; nothing here talks to IBKR directly
except reading the open orders and live quotes.

  working_orders   the strategy's open orders by symbol and role
  adopt_and_forget drop state of closed positions, re-adopt after a state reset
  cancel_orphan_stops  a stop whose position is gone (it could open a short)
  reconcile_stops  every position has a correctly sized stop at IBKR
  raise_stops      breakeven / swing-low trail (manage blocks)
  partial_profits  partial exit with retry state machine (manage block)
  force_close      end-of-day flatten in the stop's OCA group (exit block)
  sweep_entries    cancel unfilled entries after the entry window
"""
import dataclasses
import datetime as dt
import logging

from ib_async import IB, Trade

from paperbot.core import market_data as md
from paperbot.core.common import log, push, warn_once
from paperbot.core.risk import CancelRequest, StopUpdate
from paperbot.core.sizing import LIMIT_SLIPPAGE, closing_sell, split_shares, to_tick
from paperbot.indicators import swing_low

ALERT_AT = dt.time(15, 58)  # still holding after this = force close is failing
EXIT_STALE_SECONDS = 60  # an exit order still working after this is cancelled/replaced
PARTIAL_MAX_ATTEMPTS = 3  # partial-profit sends per position before giving up

_orphans: dict[str, int] = {}  # SYMBOL -> passes in a row with a stop but no position


@dataclasses.dataclass
class Working:
    """The strategy's own open orders for one symbol, by role (orderRef)."""
    stop: Trade | None = None
    exits: list[Trade] = dataclasses.field(default_factory=list)
    entries: list[Trade] = dataclasses.field(default_factory=list)


def working_orders(ib: IB, refs: tuple[str, str, str]) -> dict[str, Working]:
    ref_entry, ref_stop, ref_exit = refs
    out: dict[str, Working] = {}
    for t in ib.openTrades():
        ref = t.order.orderRef or ""
        if t.isDone() or ref not in refs:
            continue
        w = out.setdefault(t.contract.symbol.upper(), Working())
        if ref == ref_stop:
            w.stop = t
        elif ref == ref_exit:
            w.exits.append(t)
        else:
            w.entries.append(t)
    return out


def remaining(trade: Trade) -> int:
    return int(trade.order.totalQuantity - trade.orderStatus.filled)


def age_seconds(trade: Trade) -> float:
    if not trade.log:
        return 0.0
    return (dt.datetime.now(dt.timezone.utc) - trade.log[0].time).total_seconds()


def sweep_entries(working: dict[str, Working]) -> list:
    """After the entry window: cancel every unfilled entry (GTD should already have
    expired it at IBKR; this covers an order IBKR has not expired yet)."""
    return [CancelRequest(t.order.orderId, symbol, "entry window closed")
            for symbol, w in working.items() for t in w.entries]


def dedupe_cancels(requests: list) -> list:
    seen, out = set(), []
    for r in requests:
        if isinstance(r, CancelRequest):
            if r.order_id in seen:
                continue
            seen.add(r.order_id)
        out.append(r)
    return out


def adopt_and_forget(state: dict, held: dict, working: dict[str, Working]):
    """Drop state for closed positions; re-adopt our positions after a state reset."""
    for symbol in [s for s in state["positions"] if s not in held and s not in working]:
        del state["positions"][symbol]
    for symbol, pos in held.items():
        if symbol in state["positions"]:
            continue
        stop = working.get(symbol, Working()).stop
        if stop is not None:
            aux = stop.order.auxPrice
            state["positions"][symbol] = {"initial_stop": aux, "stop": aux,
                                          "breakeven": aux >= pos.avgCost,
                                          "partial_done": True}
            log.warning("ALERT re-adopted %s from its working stop %.2f (strategy state "
                        "was missing; no further partial profit)", symbol, aux)
        else:
            warn_once(f"unmanaged:{symbol}", logging.WARNING,
                      "%s position is not managed by paperbot (no state, no paperbot stop); "
                      "leaving it alone", symbol)


def cancel_orphan_stops(held: dict, working: dict[str, Working]) -> list:
    """Cancel a paperbot stop whose position is gone (it would open a short if it
    triggered). Requires two passes in a row, so a stop whose entry has just filled
    is not cancelled before IBKR's position update arrives."""
    requests = []
    for symbol, w in working.items():
        if w.stop is not None and symbol not in held and not w.entries:
            _orphans[symbol] = _orphans.get(symbol, 0) + 1
            if _orphans[symbol] >= 2:
                log.warning("ALERT cancelling paperbot stop for %s: no position held", symbol)
                requests.append(CancelRequest(w.stop.order.orderId, symbol,
                                              "orphan stop, no position"))
        else:
            _orphans.pop(symbol, None)
    return requests


def reconcile_stops(state: dict, held: dict, working: dict[str, Working]) -> list:
    """Make sure every position we own has the right protective stop at IBKR."""
    requests = []
    for symbol, pos in held.items():
        st = state["positions"].get(symbol)
        w = working.get(symbol, Working())
        if st is None or w.entries:
            continue  # not ours, or the bracket is still working (IBKR manages its child)
        qty = int(pos.position) - sum(remaining(t) for t in w.exits)
        if qty <= 0:
            continue  # fully covered by working exits
        if w.stop is None:
            log.error("ALERT %s has no protective stop at IBKR; placing one at %.2f",
                      symbol, st["stop"])
            requests.append(StopUpdate(symbol, qty, st["stop"], "restore protective stop"))
            continue
        price = max(st["stop"], w.stop.order.auxPrice)
        if int(w.stop.order.totalQuantity) != qty or w.stop.order.auxPrice < price:
            requests.append(StopUpdate(symbol, qty, price, "sync protective stop"))
    return requests


def raise_stops(profile, state, held, working, bars):
    """Breakeven at +xR, then trail to the latest swing low (manage blocks).
    Stops only move up."""
    breakeven = profile.block("manage", "breakeven")
    trail = profile.block("manage", "trail_swing_low")
    if breakeven is None and trail is None:
        return
    for symbol, pos in held.items():
        st, rth = state["positions"].get(symbol), bars.get(symbol)
        if st is None or not rth or working.get(symbol, Working()).entries:
            continue
        price, entry = rth[-1].close, pos.avgCost
        risk = entry - st["initial_stop"]
        if risk <= 0:
            continue
        if breakeven is not None and not st["breakeven"] and price >= entry + breakeven.at_R * risk:
            st["breakeven"] = True
            st["stop"] = max(st["stop"], to_tick(entry, "STOP"))
            log.info("%s reached %gR, stop to breakeven %.2f", symbol, breakeven.at_R, st["stop"])
            push(f"BE {symbol}", lambda: f"stop -> ${st['stop']:.2f}")
        if trail is not None and st["breakeven"]:
            swing = swing_low(rth, trail.left, trail.right)
            if swing is not None and swing > st["stop"] and swing < price:
                old = st["stop"]
                st["stop"] = swing
                log.info("%s trailing stop raised to swing low %.2f", symbol, swing)
                push(f"TRAIL {symbol}", lambda: f"stop ${old:.2f} -> ${swing:.2f}")


def partial_profits(ib, profile, state, held, working, bars) -> list:
    """Sell a fraction at +xR, priced from the live bid. The stop is shrunk first
    so stop + exit = position.

    State machine per position: idle -> pending (order sent; on_sent records the
    share count it was sent against) -> done or back to idle. A partial order not
    filled within EXIT_STALE_SECONDS is cancelled (DAY orders also expire). Once
    no partial order is working: fewer shares than when it was sent = filled,
    done; the same shares = expired/cancelled unfilled, so the state resets and
    the target is recalculated and retried on a later pass (the reconcile first
    restores the stop to the full position), up to PARTIAL_MAX_ATTEMPTS sends."""
    partial = profile.block("manage", "partial_profit")
    if partial is None:
        return []
    requests = []
    for symbol, pos in held.items():
        st, rth = state["positions"].get(symbol), bars.get(symbol)
        w = working.get(symbol, Working())
        if st is None or w.entries:
            continue
        stale = [t for t in w.exits if age_seconds(t) >= EXIT_STALE_SECONDS]
        for t in stale:
            requests.append(CancelRequest(t.order.orderId, symbol,
                                          "partial-profit order not filled in time"))
        if st.get("partial_pending") is not None and not w.exits:
            settle_partial(symbol, st, int(pos.position))
            continue  # a retry waits for the next pass, after the stop is resized
        if st["partial_done"] or w.exits or not rth or w.stop is None:
            continue
        price, qty, entry = rth[-1].close, int(pos.position), pos.avgCost
        risk = entry - st["initial_stop"]
        if risk <= 0 or price < entry + partial.at_R * risk:
            continue
        part = split_shares(qty, partial.fraction)
        if part < 1:
            st["partial_done"] = True  # position too small to split
            continue
        quote = md.quote(ib, symbol)
        if quote is None:
            log.warning("%s partial profit deferred: no live quote", symbol)
            continue
        st["partial_basis"] = qty  # on_sent turns this into partial_pending
        requests.append(StopUpdate(symbol, qty - part, max(st["stop"], w.stop.order.auxPrice),
                                   "shrink stop before partial profit"))
        requests.append(closing_sell(symbol, part, quote[0], "partial profit"))
    return requests


def settle_partial(symbol: str, st: dict, shares: int):
    """The partial order is gone: decide whether it filled, or reset it for a retry."""
    basis = st.pop("partial_pending")
    attempts = st.get("partial_attempts", 0)
    if shares < basis:
        st["partial_done"] = True
        log.info("%s partial profit filled (%d -> %d shares)", symbol, basis, shares)
    elif attempts >= PARTIAL_MAX_ATTEMPTS:
        st["partial_done"] = True
        log.warning("%s partial profit not filled after %d attempts; no further partial",
                    symbol, attempts)
    else:
        log.info("%s partial profit order ended unfilled (attempt %d/%d); target will be "
                 "recalculated and retried", symbol, attempts, PARTIAL_MAX_ATTEMPTS)


def _close_position(ib, profile, symbol, pos, st, w, now, label: str, attempts_key: str,
                    reason: str) -> list:
    """Requests that flatten one position: cancel its working exits and entries, then
    a marketable limit SELL (from the live bid, wider each attempt) for every share.

    The SELL joins the protective stop's OCA group (ocaType 2: reduce with block),
    so the stop stays live until IBKR fills the SELL and IBKR itself makes sure the
    stop and the SELL can never both fill. A stop without an OCA group (placed
    before OCA linking) is cancelled first and the runner confirms the cancel
    before sending the SELL. [] when there is no price to send it at (retried)."""
    quote = md.quote(ib, symbol)
    if quote is not None:
        price = quote[0]
    else:
        timeframe = profile.timeframe if profile.timeframe != "1d" else "5m"
        rth = md.rth_bars(ib, symbol, now, timeframe)
        if not rth:
            log.error("ALERT %s: no live quote and no price data, cannot %s this loop",
                      symbol, label)
            return []
        price = rth[-1].close
        key = "fc-quote" if label == "force close" else "exit-quote"
        warn_once(f"{key}:{symbol}:{now.date()}", logging.ERROR,
                  "ALERT %s: no live quote; %s limit priced from the last %s close %.2f",
                  symbol, label.replace(" ", "-"), timeframe, price)
    requests = []
    oca = getattr(w.stop.order, "ocaGroup", "") if w.stop is not None else ""
    for t in w.exits + w.entries:
        requests.append(CancelRequest(t.order.orderId, symbol, f"{label}: replace"))
    if w.stop is not None and not oca:
        requests.append(CancelRequest(w.stop.order.orderId, symbol,
                                      f"{label}: stop replaced by closing order (no OCA group)"))
    st[attempts_key] = st.get(attempts_key, 0) + 1
    slippage = LIMIT_SLIPPAGE * min(st[attempts_key], 5)
    requests.append(closing_sell(symbol, int(pos.position), price,
                                 reason.format(attempt=st[attempts_key]), slippage,
                                 oca_group=oca or None))
    return requests


def force_close(ib, profile, state, held, working, now) -> list:
    """Close every position we own from force_close_et (see _close_position).
    Re-priced each pass until flat; ALERT from 15:58 while anything is open."""
    requests, first_attempts = [], 0
    for symbol, pos in held.items():
        st = state["positions"].get(symbol)
        if st is None:
            continue
        if now.time() >= ALERT_AT:
            log.error("ALERT %s: %g shares still open at %s; force close has not filled",
                      symbol, pos.position, now.strftime("%H:%M"))
        w = working.get(symbol, Working())
        if any(age_seconds(t) < EXIT_STALE_SECONDS for t in w.exits):
            continue  # the current close attempt is still fresh
        closing = _close_position(ib, profile, symbol, pos, st, w, now, "force close",
                                  "force_attempts", "force close (attempt {attempt})")
        requests += closing
        if closing and st["force_attempts"] == 1:
            first_attempts += 1
    if first_attempts:
        push("EOD Force Close", lambda: f"flattening {first_attempts} positions", "high")
    return requests


def _opened(state: dict, symbol: str, st: dict) -> dt.date | None:
    """ET date the position was entered (opened_at, else the strategy's last entry date)."""
    raw = st.get("opened_at") or state["entered"].get(symbol)
    try:
        return dt.date.fromisoformat(str(raw)[:10]) if raw else None
    except ValueError:
        return None


def signal_exits(ib, profile, state, held, working, now) -> list:
    """Exit blocks (blocks/exit.py, other than force_close) on every position we own.

    The first block that fires decides the exit; the reason is kept in the
    position state, so a decided exit is carried through re-pricing and, if it
    does not fill before the close, into the next session. Multi-day strategies
    never exit on the entry day. Closing works like the force close: a SELL in
    the stop's OCA group, the stop stays live, re-priced every EXIT_STALE_SECONDS."""
    from paperbot.blocks.context import ExitContext
    blocks = profile.signal_exits
    if not blocks:
        return []
    requests, decided = [], []
    for symbol, pos in held.items():
        st = state["positions"].get(symbol)
        w = working.get(symbol, Working())
        if st is None or w.entries:
            continue
        if st.get("exit_reason") is None:
            opened = _opened(state, symbol, st)
            if profile.holding == "multi_day" and opened == now.date():
                continue
            ctx = ExitContext(ib, profile, symbol, pos, st, opened, now)
            try:
                reason = next((r for r in (b.check(ctx) for b in blocks) if r), None)
            except Exception as e:  # noqa: BLE001 - one broken check must not block the others
                warn_once(f"exit-block:{symbol}:{now.date()}", logging.WARNING,
                          f"{symbol}: exit check error {e!r}; its stop stays at IBKR")
                continue
            if reason is None:
                continue
            st["exit_reason"] = reason
            decided.append(f"{symbol}: {reason}")
            log.info("%s exit signal: %s", symbol, reason)
        if any(age_seconds(t) < EXIT_STALE_SECONDS for t in w.exits):
            continue  # the current exit order is still fresh
        requests += _close_position(ib, profile, symbol, pos, st, w, now, "exit",
                                    "exit_attempts", "exit (attempt {attempt}): " + st["exit_reason"])
    if decided:
        push("EXIT signal", lambda: "; ".join(decided))
    return requests
