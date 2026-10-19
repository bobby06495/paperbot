"""Paper-only trading bot with built-in risk limits and broker-held stops.

Usage (project venv, after `pip install -e .`; `paperbot-bot` is the same command):
    python -m paperbot.trading.bot                 dry run (default): logs what it would do
    python -m paperbot.trading.bot --submit        sends orders to the IB Gateway PAPER account
    python -m paperbot.trading.bot --reset-state   moves corrupt state files aside
    python -m paperbot.trading.bot --symbol NVDA --check-only
                                  evaluates one symbol (position, entry window, every
                                  filter) and prints why it passes or fails; read-only
                                  connection, never sends orders, can run next to the bot

All log and journal timestamps are US Eastern Time, whatever the PC's time zone.

Notifications (notify.py, if Telegram or ntfy is configured in .env): every
"ALERT" log line and error, BUY / STOP / PARTIAL / EXIT for fills of the bot's
own orders (from IBKR's execution report, once each), and a crash message.
Each of those fills is also appended to trades.csv (read by compute_perf.py).
After every loop, logs/heartbeat.json and dashboard/index.html are refreshed.

How it works:
  * Refuses to start if the STOP file exists, another bot is running
    (logs/bot.pid), a state file is corrupt, or the account is not paper.
  * Every loop_seconds it asks strategy.decide() for requests (entries,
    exits, stop updates, cancels) and runs each through risk.py.
  * Each entry is sent as a bracket: a BUY limit order plus a child SELL STP
    (GTC) held at IBKR, so the position stays protected if this process,
    the connection or the computer fails. The strategy only moves that stop
    up (breakeven, trail) and re-creates it if it is ever missing.
  * Cancels are confirmed before any follow-up order for the same symbol is
    sent, so a replaced order can never double-sell.

Stopping:
  * paperbot-stop (or create a file named STOP in the project root): on its
    next loop the bot cancels its working entry/exit orders, KEEPS the
    protective stops, and exits. Delete STOP before restarting.
  * Ctrl+C: exits immediately and leaves every order, including stops, in place.
  * paperbot-kill [--all]: see paperbot/runtime/kill_switch.py.
"""
import argparse
import asyncio
import csv
import datetime as dt
import json
import logging
import math
import sys
from pathlib import Path

from ib_async import (IB, LimitOrder, MarketOrder, Stock, StartupFetch, StartupFetchALL,
                      StopOrder, Trade)

from paperbot import paths
from paperbot.reporting.notify import AlertHandler, notify
from paperbot.runtime import runstate
from paperbot.trading import strategy
from paperbot.trading.risk import (ET, REF_ENTRY, REF_EXIT, REF_STOP, AccountState,
                                   CancelRequest, OrderIntent, RiskGuard, StopUpdate,
                                   load_limits, verify_paper)

RULES = paths.RULES_FILE
CONFIG = paths.CONFIG_FILE
LOGS = paths.LOGS_DIR
RISK_STATE = LOGS / "state.json"
NOTIFY_STATE = LOGS / "notify_state.json"  # executions already pushed today
TRADES_CSV = paths.TRADES_CSV  # fill ledger: one row per execution of our own orders
LEDGER_FIELDS = ["timestamp_iso", "symbol", "side", "size", "fill_price", "order_id",
                 "status", "order_ref", "exec_id", "commission", "stop_price"]
UNSET_PNL = 1e100  # IBKR reports "no realized P&L" (opening fills) as ~1.8e308
CANCEL_WAIT_SECONDS = 5  # how long to wait for IBKR to confirm a cancel

log = logging.getLogger("paperbot")


class ETFormatter(logging.Formatter):
    """Log lines stamped [YYYY-MM-DD HH:MM:SS ET], whatever the PC's time zone."""

    def formatTime(self, record, datefmt=None):
        return dt.datetime.fromtimestamp(record.created, ET).strftime("%Y-%m-%d %H:%M:%S ET")


def setup_logging():
    LOGS.mkdir(exist_ok=True)
    fmt = ETFormatter("[%(asctime)s] %(levelname)s %(message)s")
    for handler in (logging.StreamHandler(), logging.FileHandler(LOGS / "bot.log")):
        handler.setFormatter(fmt)
        log.addHandler(handler)
    log.addHandler(AlertHandler())  # "ALERT" lines and errors -> Telegram / ntfy
    log.setLevel(logging.INFO)


def journal(event: str, **data):
    """Append one line per order event to logs/orders.jsonl (ET timestamps)."""
    entry = {"time": dt.datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S ET"),
             "event": event, **data}
    with open(LOGS / "orders.jsonl", "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def qualify(ib: IB, symbol: str, timeout: float) -> Stock | None:
    contract = Stock(symbol, "SMART", "USD")
    try:
        ib.run(asyncio.wait_for(ib.qualifyContractsAsync(contract), timeout))
    except asyncio.TimeoutError:
        log.warning("contract lookup for %s timed out (IBKR maintenance?)", symbol)
        return None
    return contract if contract.conId else None


def cancel_working(ib: IB, why: str):
    """Cancel the bot's working entry and exit orders. Protective stops stay at IBKR."""
    for trade in ib.openTrades():
        if trade.order.orderRef in (REF_ENTRY, REF_EXIT) and not trade.isDone():
            log.warning("cancelling order %s (%s)", trade.order.orderId, why)
            ib.cancelOrder(trade.order)
            journal("cancel", orderId=trade.order.orderId, why=why)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--submit", action="store_true",
                        help="send orders to the paper account (default: dry run)")
    parser.add_argument("--reset-state", action="store_true",
                        help="move corrupt state files aside and exit")
    parser.add_argument("--symbol", help="symbol to evaluate (requires --check-only)")
    parser.add_argument("--check-only", action="store_true",
                        help="evaluate --symbol once and exit; never sends orders")
    args = parser.parse_args()
    if args.check_only and not args.symbol:
        parser.error("--check-only needs --symbol")
    if args.symbol and not args.check_only:
        parser.error("--symbol is only supported with --check-only")
    if args.check_only and args.submit:
        parser.error("--check-only never sends orders; drop --submit")
    setup_logging()

    if args.check_only:
        sys.exit(check_only(args.symbol))

    if args.reset_state:
        backups = runstate.reset_corrupt([RISK_STATE, strategy.STATE])
        for b in backups:
            log.warning("moved corrupt state file to %s", b.name)
        if not backups:
            print("No corrupt state files; nothing changed.")
        if any(b.name.startswith(strategy.STATE.name) for b in backups):
            print("Strategy state was reset: positions with a working paperbot stop are "
                  "re-adopted from that stop; others are left alone and reported.")
        return

    cfg = json.loads(CONFIG.read_text())
    limits = load_limits(cfg, RULES)
    guard = RiskGuard(limits, RISK_STATE)  # fails closed on a corrupt file
    strategy.load_state()  # same check for the strategy state, before connecting
    if runstate.stop_requested():
        raise SystemExit(f"{runstate.STOP_FILE.name} file present; delete it to start the bot")
    runstate.claim_pid()

    ib = IB()
    try:
        connect(ib, cfg, cfg["client_id"],
                fetchFields=StartupFetchALL & ~StartupFetch.EXECUTIONS)
        account = verify_paper(cfg["port"], ib.managedAccounts())
        mode = "SUBMIT" if args.submit else "DRY RUN"
        log.info("connected to paper account %s, mode %s, limits %s", account, mode, limits)

        pnl = ib.reqPnL(account)
        ib.sleep(3)  # give IBKR time to send the first P&L update
        ib.orderStatusEvent += lambda t: journal(
            "status", orderId=t.order.orderId, status=t.orderStatus.status,
            filled=t.orderStatus.filled)
        ib.execDetailsEvent += lambda t, fill: journal(
            "fill", orderId=fill.execution.orderId, symbol=fill.contract.symbol,
            side=fill.execution.side, shares=fill.execution.shares,
            price=fill.execution.price)

        while True:
            if runstate.stop_requested():
                cancel_working(ib, "STOP file")
                log.warning("STOP file found, shutting down (protective stops left at IBKR)")
                break

            daily = pnl.dailyPnL
            daily = None if daily is None or math.isnan(daily) else daily
            was_halted = guard.halted
            guard.update_daily_pnl(daily)
            if guard.halted and not was_halted:
                cancel_working(ib, guard.state["halt_reason"])
            notify_fills_safely(ib, account)

            held_back: set[str] = set()  # symbols whose cancel was not confirmed
            for request in strategy.decide(ib, account):
                symbol = request.symbol.upper()
                if symbol in held_back:
                    log.warning("skipping %s: waiting for a cancel on %s", request, symbol)
                    continue
                if not handle_request(ib, guard, cfg, account, request, daily, args.submit):
                    held_back.add(symbol)
            after_pass(ib, account, "bot.py", args.submit)

            ib.sleep(cfg["loop_seconds"])
    except KeyboardInterrupt:
        log.info("stopped by Ctrl+C (open orders and protective stops left in place)")
    except Exception as e:
        try:
            notify("Bot CRASHED", f"{type(e).__name__}: {e}"[:500], "high")
            runstate.mark_heartbeat("crashed", f"{type(e).__name__}: {e}")
        except Exception:  # noqa: BLE001 - never mask the original error
            pass
        raise
    finally:
        ib.disconnect()
        runstate.release_pid()


def connect(ib: IB, cfg: dict, client_id: int, **kwargs):
    """Connect to IB Gateway, or log a clear message and exit with code 1."""
    try:
        ib.connect(cfg["host"], cfg["port"], clientId=client_id, timeout=15, **kwargs)
    except (OSError, asyncio.TimeoutError, TimeoutError, ConnectionError) as e:
        log.error("cannot connect to IB Gateway at %s:%s (clientId %s): %r. Is Gateway "
                  "running, logged in to the paper account, with the API enabled?",
                  cfg["host"], cfg["port"], client_id, e)
        raise SystemExit(1) from None


def check_only(symbol: str) -> int:
    """bot.py --symbol X --check-only: evaluate one symbol and print the result.

    Uses a read-only connection with its own clientId (client_id + 2), so it
    can run while the bot is running and can never place an order.
    """
    cfg = json.loads(CONFIG.read_text())
    ib = IB()
    try:
        connect(ib, cfg, cfg["client_id"] + 2, readonly=True)
        account = verify_paper(cfg["port"], ib.managedAccounts())
        ib.reqAllOpenOrders()  # see the bot's working orders too
        result = strategy.check_symbol(ib, account, symbol)
    finally:
        ib.disconnect()
    verdict = "PASS" if result["pass"] else "NO ENTRY"
    log.info("CHECK %s: %s at %.2f", result["symbol"], verdict, result["price"])
    for reason in result["reasons"]:
        log.info("  %s", reason)
    if result.get("quantity") is not None:
        log.info("  would buy %s shares, stop %.2f (not sent: check-only)",
                 result["quantity"], result["stop"])
    log.info("  logged to %s", strategy.SAFETY_LOG.relative_to(paths.PROJECT_ROOT))
    return 0


def notify_fills(ib: IB, account: str):
    """Record and push today's new fills of our own orders, once each.

    Every new execution is appended to the trades.csv ledger (read by
    compute_perf.py), then BUY / STOP / PARTIAL / EXIT is pushed per order.
    Read from IBKR's execution report (not live fill events), so fills that
    happened while nothing was connected, e.g. a stop between two cycle.py
    runs, are covered too. Already-handled execIds are kept for the day in
    logs/notify_state.json.
    """
    today = dt.datetime.now(ET).date().isoformat()
    try:
        saved = runstate.read_json_state(NOTIFY_STATE) or {}
    except runstate.StateCorrupt:
        saved = {}  # worst case: a fill is pushed twice
    seen = set(saved.get("exec_ids", [])) if saved.get("date") == today else set()

    by_order: dict[int, list] = {}
    for fill in ib.reqExecutions():
        e = fill.execution
        if (e.execId in seen or e.orderRef not in (REF_ENTRY, REF_STOP, REF_EXIT)
                or (e.acctNumber and e.acctNumber != account)):
            continue
        by_order.setdefault(e.orderId, []).append(fill)
    if not by_order:
        return

    append_ledger([f for fills in by_order.values() for f in fills])
    held = {p.contract.symbol.upper(): p.position for p in ib.positions(account)}
    for fills in by_order.values():
        title, body = fill_message(fills, held)
        notify(title, body, "default")
        seen.update(f.execution.execId for f in fills)
    runstate.write_json_atomic(NOTIFY_STATE, {"date": today, "exec_ids": sorted(seen)})


def append_ledger(fills: list, path: Path | None = None):
    """Append one trades.csv row per execution; execIds already in the file are skipped.

    Entry (BUY) rows carry stop_price, the initial protective stop from the
    strategy state, so compute_perf.py can measure each trade in R.
    """
    path = path or TRADES_CSV
    existing: set[str] = set()
    fields = LEDGER_FIELDS
    if path.exists():
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            existing = {row.get("exec_id", "") for row in reader}
            fields = reader.fieldnames or LEDGER_FIELDS  # keep an older file's columns
    try:
        positions = strategy.load_state()["positions"]
    except Exception:  # noqa: BLE001 - the ledger must not depend on it
        positions = {}
    rows = []
    for fill in fills:
        e, report = fill.execution, fill.commissionReport
        if e.execId in existing:
            continue
        reported = bool(report and report.execId)
        symbol = fill.contract.symbol.upper()
        stop = positions.get(symbol, {}).get("initial_stop") if e.orderRef == REF_ENTRY else None
        rows.append({
            "timestamp_iso": e.time.astimezone(ET).isoformat(timespec="seconds"),
            "symbol": symbol,
            "side": "BUY" if e.side in ("BOT", "BUY") else "SELL",
            "size": f"{e.shares:g}",
            "fill_price": f"{e.price:.4f}",
            "order_id": e.orderId,
            "status": "filled",
            "order_ref": e.orderRef,
            "exec_id": e.execId,
            "commission": f"{report.commission:.4f}" if reported else "",
            "stop_price": f"{stop:.4f}" if stop else "",
        })
    if not rows:
        return
    rows.sort(key=lambda r: r["timestamp_iso"])  # executions arrive grouped by order
    new_file = not path.exists() or path.stat().st_size == 0
    path.parent.mkdir(parents=True, exist_ok=True)  # data/ on first fill
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if new_file:
            writer.writeheader()
        writer.writerows(rows)


def heartbeat_positions(ib: IB, account: str) -> list[dict]:
    """Open stock positions from IBKR's portfolio data, with the strategy's stops."""
    try:
        owned = strategy.load_state()["positions"]
    except Exception:  # noqa: BLE001
        owned = {}
    positions = []
    for item in ib.portfolio(account):
        if item.contract.secType != "STK" or item.position <= 0:
            continue
        symbol = item.contract.symbol.upper()
        st, price = owned.get(symbol, {}), item.marketPrice
        positions.append({
            "symbol": symbol, "qty": float(item.position), "entry": float(item.averageCost),
            "stop": st.get("stop"), "initial_stop": st.get("initial_stop"),
            "market_price": float(price) if price and not math.isnan(price) and price > 0 else None,
            "managed": symbol in owned})
    return positions


def after_pass(ib: IB, account: str, source: str, submit: bool):
    """After every bot loop / cycle pass: heartbeat, telemetry, live dashboard. Never raises."""
    try:
        runstate.write_heartbeat({
            "time": dt.datetime.now(ET).isoformat(timespec="seconds"), "source": source,
            "mode": "SUBMIT" if submit else "DRY RUN", "status": "ok", "error": "",
            "positions": heartbeat_positions(ib, account)})
    except Exception as e:  # noqa: BLE001
        log.warning("heartbeat skipped: %r", e)
    try:
        from paperbot.reporting import telemetry  # read-only: cached ib data + log files
        telemetry.record_pass(ib, account, source, submit)
    except Exception as e:  # noqa: BLE001
        log.warning("telemetry skipped: %r", e)
    try:
        from paperbot.reporting import compute_perf  # pandas: imported only when needed
        compute_perf.refresh_dashboard()
    except Exception as e:  # noqa: BLE001
        log.warning("dashboard refresh skipped: %r", e)


def notify_fills_safely(ib: IB, account: str):
    """notify_fills, but a notification problem never interrupts trading."""
    try:
        notify_fills(ib, account)
    except Exception as e:  # noqa: BLE001
        log.warning("fill notifications skipped: %r", e)


def fill_message(fills: list, held: dict[str, float]) -> tuple[str, str]:
    """(title, body) for the executions of one order."""
    first = fills[0].execution
    symbol = fills[0].contract.symbol.upper()
    shares = sum(f.execution.shares for f in fills)
    price = sum(f.execution.shares * f.execution.price for f in fills) / shares
    reported = [f.commissionReport.realizedPNL for f in fills
                if f.commissionReport and f.commissionReport.execId
                and abs(f.commissionReport.realizedPNL) < UNSET_PNL]
    pnl = f"P&L ${sum(reported):+.2f}" if reported else "P&L n/a"

    if first.orderRef == REF_ENTRY:
        try:
            stop = strategy.load_state()["positions"].get(symbol, {}).get("stop")
        except Exception:  # noqa: BLE001
            stop = None
        stop_text = f"${stop:.2f}" if stop else "n/a"
        return f"BUY {symbol}", f"@ ${price:.2f}, stop {stop_text}, qty {shares:g}"
    if first.orderRef == REF_STOP:
        return f"STOP {symbol}", f"exit ${price:.2f}, {pnl}"
    remaining = held.get(symbol, 0)
    if remaining > 0:
        return f"PARTIAL {symbol}", f"sold {shares:g}/{shares + remaining:g} @ ${price:.2f}"
    return f"EXIT {symbol}", f"@ ${price:.2f}, {pnl}"


def account_state(ib: IB, account: str, open_trades: list[Trade]) -> AccountState:
    net_liq = next((float(v.value) for v in ib.accountValues(account)
                    if v.tag == "NetLiquidation" and v.currency == "USD"), None)
    longs = {p.contract.symbol.upper(): p.position for p in ib.positions(account)
             if p.contract.secType == "STK" and p.position > 0}
    buys = {t.contract.symbol.upper() for t in open_trades if t.order.action == "BUY"}
    return AccountState(net_liq, longs, buys)


def handle_request(ib: IB, guard: RiskGuard, cfg: dict, account: str, request,
                   daily: float | None, submit: bool) -> bool:
    """Run one strategy request. Returns False only if a cancel was not confirmed."""
    if isinstance(request, CancelRequest):
        return handle_cancel(ib, request, submit)
    if isinstance(request, StopUpdate):
        handle_stop(ib, guard, cfg, account, request, submit)
    else:
        handle_intent(ib, guard, cfg, account, request, daily, submit)
    return True


def handle_cancel(ib: IB, req: CancelRequest, submit: bool) -> bool:
    """Cancel one of our orders and wait for IBKR to confirm it is done."""
    trade = next((t for t in ib.openTrades() if t.order.orderId == req.order_id), None)
    if trade is None or trade.isDone():
        return True
    if not submit:
        log.info("DRY RUN would cancel order %s (%s)", req.order_id, req.reason)
        return True
    ib.cancelOrder(trade.order)
    journal("cancel", orderId=req.order_id, symbol=req.symbol, why=req.reason)
    for _ in range(CANCEL_WAIT_SECONDS * 4):
        if trade.isDone():
            log.info("cancelled order %s (%s)", req.order_id, req.reason)
            return True
        ib.sleep(0.25)
    log.warning("cancel of order %s not confirmed within %ss; holding further %s orders "
                "until the next loop", req.order_id, CANCEL_WAIT_SECONDS, req.symbol)
    return False


def _find_stop(ib: IB, symbol: str) -> Trade | None:
    return next((t for t in ib.openTrades()
                 if t.order.orderRef == REF_STOP and not t.isDone()
                 and t.contract.symbol.upper() == symbol.upper()), None)


def handle_stop(ib: IB, guard: RiskGuard, cfg: dict, account: str,
                req: StopUpdate, submit: bool):
    """Place the protective SELL STP (GTC) for a position, or modify the working one."""
    open_trades = ib.reqAllOpenOrders()
    problems = guard.check_stop(req, account_state(ib, account, open_trades))
    if problems:
        log.warning("BLOCKED %s: %s", req, "; ".join(problems))
        journal("blocked", stop=req.__dict__, problems=problems)
        return
    if not submit:
        log.info("DRY RUN would set stop %s %s @ %.2f (%s)",
                 req.symbol, req.quantity, req.stop_price, req.reason)
        journal("dry_run", stop=req.__dict__)
        return

    existing = _find_stop(ib, req.symbol)
    if existing is not None:
        order = existing.order
        order.auxPrice, order.totalQuantity = req.stop_price, req.quantity
        ib.placeOrder(existing.contract, order)  # same orderId = modify
        log.info("MODIFIED stop %s: %s %s @ %.2f (%s)", order.orderId, req.symbol,
                 req.quantity, req.stop_price, req.reason)
    else:
        contract = qualify(ib, req.symbol, cfg["lookup_timeout_seconds"])
        if contract is None:
            log.error("ALERT could not place protective stop for %s: contract lookup failed",
                      req.symbol)
            journal("blocked", stop=req.__dict__, problems=["contract lookup failed"])
            return
        order = StopOrder("SELL", req.quantity, req.stop_price, account=account,
                          tif="GTC", orderRef=REF_STOP)
        ib.placeOrder(contract, order)
        log.info("PLACED stop %s: %s %s @ %.2f (%s)", order.orderId, req.symbol,
                 req.quantity, req.stop_price, req.reason)
    journal("stop", orderId=order.orderId, stop=req.__dict__)


def handle_intent(ib: IB, guard: RiskGuard, cfg: dict, account: str,
                  intent: OrderIntent, daily: float | None, submit: bool):
    """Send an entry (as a bracket with its protective stop) or a closing exit."""
    open_trades = ib.reqAllOpenOrders()
    working = sum(1 for t in open_trades if t.order.orderRef != REF_STOP)
    problems = guard.check(intent, working, daily, account_state(ib, account, open_trades))
    if problems:
        log.warning("BLOCKED %s: %s", intent, "; ".join(problems))
        journal("blocked", intent=intent.__dict__, problems=problems)
        return

    contract = qualify(ib, intent.symbol, cfg["lookup_timeout_seconds"])
    if contract is None:
        journal("blocked", intent=intent.__dict__, problems=["contract lookup failed"])
        return

    if not submit:
        stop = f", stop {intent.stop_price:.2f}" if intent.stop_price else ""
        log.info("DRY RUN would send %s %s %s @ %s%s (%s)", intent.action, intent.quantity,
                 contract.symbol, intent.limit_price or "MKT", stop, intent.reason)
        journal("dry_run", intent=intent.__dict__)
        return

    if intent.action == "BUY":
        orders = entry_bracket(ib, intent, account, cfg["time_in_force"])
    else:
        orders = [_order(intent, account, cfg["time_in_force"], REF_EXIT)]
    for order in orders:
        ib.placeOrder(contract, order)
    if intent.action == "BUY":
        guard.record_order()
    ids = [o.orderId for o in orders]
    log.info("SENT order(s) %s: %s %s %s @ %s (%s)", ids, intent.action, intent.quantity,
             contract.symbol, intent.limit_price or "MKT", intent.reason)
    journal("sent", orderIds=ids, intent=intent.__dict__)
    if hasattr(strategy, "on_sent"):
        strategy.on_sent(intent)


def _order(intent: OrderIntent, account: str, tif: str, ref: str):
    if intent.limit_price is None:
        return MarketOrder(intent.action, intent.quantity, account=account, tif=tif,
                           orderRef=ref)
    return LimitOrder(intent.action, intent.quantity, intent.limit_price,
                      account=account, tif=tif, orderRef=ref)


def entry_bracket(ib: IB, intent: OrderIntent, account: str, tif: str) -> list:
    """Parent BUY plus a child SELL STP (GTC). IBKR activates the stop when the
    parent fills, and cancels it if the parent is cancelled unfilled."""
    parent = _order(intent, account, tif, REF_ENTRY)
    parent.orderId = ib.client.getReqId()
    parent.transmit = False  # sent together with the child below
    stop = StopOrder("SELL", intent.quantity, intent.stop_price, account=account,
                     tif="GTC", orderRef=REF_STOP, parentId=parent.orderId, transmit=True)
    return [parent, stop]


if __name__ == "__main__":
    main()
