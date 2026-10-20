"""The paper bot runner: broker session, risk guard, orders. Strategy-agnostic.

Usage (`paperbot-bot` is the same command):
    python -m paperbot.core.engine                 dry run (default): logs what it would do
    python -m paperbot.core.engine --submit        sends orders to the IB Gateway PAPER account
        --strategy KEY     run this rules.json strategy (no menu)
        --yes              skip the start-up confirmation
    python -m paperbot.core.engine --reset-state   moves corrupt state files aside
    python -m paperbot.core.engine --reset-drawdown
                                  re-arms a tripped multi-day drawdown breaker
    python -m paperbot.core.engine --symbol NVDA --check-only [--strategy KEY]
                                  evaluates one symbol (position, entry window, every
                                  entry block) and prints why it passes or fails; read-only
                                  connection, never sends orders, can run next to the bot

Before anything connects, the strategy is chosen (core/selector.py: menu in a
terminal, --strategy, or the saved choice), its rules.json section is validated
and loaded (core/profile.py), and the risk guard is built from its limits.
preflight() then logs what will run (connection, strategy, rules.json hash,
every effective limit) and refuses to start if any of those disagree.

All log and journal timestamps are US Eastern Time, whatever the PC's time zone.

Notifications (notify.py, if Telegram or ntfy is configured in .env): every
"ALERT" log line and error, BUY / STOP / PARTIAL / EXIT for fills of the bot's
own orders (from IBKR's execution report, once each), and a crash message.
Each of those fills is also appended to trades.csv (read by compute_perf.py).
After every loop, logs/heartbeat.json and dashboard/index.html are refreshed;
the dashboard rebuild (files only) runs in a background thread so it can never
delay the next pass.

How it works:
  * Refuses to start if the STOP file exists, another bot is running
    (logs/bot.pid), a state file is corrupt, or the account is not paper.
  * Every loop_seconds it asks strategy.decide() for requests (entries,
    exits, stop updates, cancels) and runs each through risk.py.
  * Each entry is sent as a bracket: a BUY limit order (GTD: IBKR expires it
    at the end of the entry window) plus a child SELL STP (GTC) held at
    IBKR, so the position stays protected if this process, the connection or
    the computer fails. The strategy only moves that stop up (breakeven,
    trail) and re-creates it if it is ever missing.
  * Every protective stop carries its own OCA group (ocaType 2, reduce with
    block). Force-close SELLs join it, so IBKR guarantees the stop and the
    SELL never both fill while the stop stays live until the SELL fills.
  * Cancels are confirmed before any follow-up order for the same symbol is
    sent, so a replaced order can never double-sell.
  * After the close, await_day_orders_done() waits until IBKR confirms every
    DAY entry/exit dead before the stop reconcile sizes the overnight stop.

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
import hashlib
import json
import logging
import math
import sys
import threading
import time
import uuid
from pathlib import Path

from ib_async import (IB, LimitOrder, MarketOrder, Stock, StartupFetch, StartupFetchALL,
                      StopOrder, Trade)

from paperbot import paths
from paperbot.reporting.notify import AlertHandler, notify
from paperbot.runtime import runstate
from paperbot.core import broker, clock
from paperbot.core import market_data as md
from paperbot.core import profile as profiles
from paperbot.core import selector
from paperbot.core.account import buying_power_usd, daily_pnl, net_liquidation_usd
from paperbot.core.risk import (ET, AccountState, CancelRequest, DrawdownBreaker, OrderIntent,
                                RiskGuard, StopUpdate, ref_role, require_limits)
from paperbot.strategies import rule_strategy as strategy
from paperbot.system import procs

RULES = paths.RULES_FILE
CONFIG = paths.CONFIG_FILE
LOGS = paths.LOGS_DIR
RISK_STATE = LOGS / "state.json"
DRAWDOWN_STATE = LOGS / "drawdown_state.json"  # multi-day drawdown breaker (risk.py)
NOTIFY_STATE = LOGS / "notify_state.json"  # executions already pushed today
TRADES_CSV = paths.TRADES_CSV  # fill ledger: one row per execution of our own orders
LEDGER_FIELDS = ["timestamp_iso", "symbol", "side", "size", "fill_price", "order_id",
                 "status", "order_ref", "exec_id", "commission", "stop_price"]
UNSET_PNL = 1e100  # IBKR reports "no realized P&L" (opening fills) as ~1.8e308
CANCEL_WAIT_SECONDS = 5  # how long to wait for IBKR to confirm a cancel
DAY_ORDER_WAIT_SECONDS = 90  # after the close: how long IBKR gets to expire DAY orders
POST_CLOSE_UNTIL = dt.time(16, 10)  # end of the post-close protect window (ET)
OCA_REDUCE_WITH_BLOCK = 2  # IBKR ocaType: fills shrink the other orders, overfill blocked
RECONNECT_FIRST_SECONDS, RECONNECT_MAX_SECONDS = 30, 300  # back-off while Gateway is down
SOURCE = "paper-bot"  # heartbeat/telemetry source of the long-running runner

log = logging.getLogger("paperbot")


class ETFormatter(logging.Formatter):
    """Log lines stamped [YYYY-MM-DD HH:MM:SS ET], whatever the PC's time zone."""

    def formatTime(self, record, datefmt=None):
        return dt.datetime.fromtimestamp(record.created, ET).strftime("%Y-%m-%d %H:%M:%S ET")


LOG_FORMAT = ETFormatter("[%(asctime)s] %(levelname)s %(message)s")


def _file_handler() -> logging.FileHandler:
    handler = logging.FileHandler(LOGS / "bot.log", encoding="utf-8")
    handler.setFormatter(LOG_FORMAT)
    return handler


def setup_logging():
    LOGS.mkdir(parents=True, exist_ok=True)
    console = logging.StreamHandler()
    console.setFormatter(LOG_FORMAT)
    log.addHandler(console)
    log.addHandler(_file_handler())
    log.addHandler(AlertHandler())  # "ALERT" lines and errors -> Telegram / ntfy
    log.setLevel(logging.INFO)


def rotate_logs_now():
    """Scheduled job: archive earlier days' logs. bot.log is closed for the move (Windows
    cannot move an open file) and reopened afterwards."""
    import contextlib
    import io
    from paperbot.ops import rotate_logs
    files = [h for h in log.handlers if isinstance(h, logging.FileHandler)]
    for handler in files:
        log.removeHandler(handler)
        handler.close()
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            rotate_logs.main()
    finally:
        if files:
            log.addHandler(_file_handler())
    for line in out.getvalue().splitlines():
        log.info("log rotation: %s", line.strip())


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
        if ref_role(trade.order.orderRef) in ("entry", "exit") and not trade.isDone():
            log.warning("cancelling order %s (%s)", trade.order.orderId, why)
            ib.cancelOrder(trade.order)
            journal("cancel", orderId=trade.order.orderId, why=why)


def post_close(now: dt.datetime) -> bool:
    """Weekday, from the 16:00 ET close until POST_CLOSE_UNTIL."""
    now = now.astimezone(ET)
    return now.weekday() < 5 and dt.time(16, 0) <= now.time() < POST_CLOSE_UNTIL


def await_day_orders_done(ib: IB, submit: bool, timeout: float = DAY_ORDER_WAIT_SECONDS) -> bool:
    """After the close: wait until IBKR confirms every paperbot entry/exit dead
    (Cancelled, Filled or Inactive) before the stop reconcile computes the
    unprotected shares. A DAY exit IBKR has not expired yet would otherwise make
    the stop target 0 and leave the position without a stop overnight.

    Polls the broker's order stream; at the timeout it cancels whatever is
    left (submit only) and waits CANCEL_WAIT_SECONDS more. Returns False with an
    ALERT if an order is still unconfirmed or the connection fails; the
    reconcile then still counts that order and places no stop next to it,
    because a stop beside a live exit could double-sell.
    """
    def live() -> list[Trade]:
        return [t for t in ib.openTrades()
                if ref_role(t.order.orderRef) in ("entry", "exit") and not t.isDone()]

    try:
        ib.reqAllOpenOrders()
        deadline = time.monotonic() + timeout
        while live() and time.monotonic() < deadline:
            ib.sleep(1)
        pending = live()
        if pending and submit:
            for t in pending:
                log.warning("order %s still live %ss after the close; cancelling it",
                            t.order.orderId, timeout)
                ib.cancelOrder(t.order)
                journal("cancel", orderId=t.order.orderId, symbol=t.contract.symbol,
                        why="post-close: DAY order not expired")
            deadline = time.monotonic() + CANCEL_WAIT_SECONDS
            while live() and time.monotonic() < deadline:
                ib.sleep(0.25)
            pending = live()
    except (ConnectionError, OSError, asyncio.TimeoutError) as e:
        log.error("ALERT post-close order check failed (%r); a position with a live closing "
                  "order gets no restored stop until a later pass confirms it dead", e)
        return False
    if pending:
        log.error("ALERT post-close: IBKR has not confirmed order(s) %s dead; their positions "
                  "get no restored stop until it does", [t.order.orderId for t in pending])
        return False
    log.info("post-close: every paperbot entry/exit confirmed done; reconciling stops")
    return True


def reset_drawdown() -> int:
    """paperbot-bot --reset-drawdown: re-arm a tripped breaker (the current
    cumulative P&L becomes the new peak)."""
    try:
        key = selector.unattended_key()
    except profiles.ConfigError as e:
        raise SystemExit(f"Refusing: {e}") from None
    limits = require_limits(json.loads(CONFIG.read_text()), RULES, key)
    if not limits.max_drawdown_usd:
        print("max_drawdown_usd is not set in rules.json; no breaker to reset.")
        return 0
    pid = runstate.running_pid()
    if pid is not None:  # its in-memory breaker would write the trip straight back
        print(f"paperbot is running (pid {pid}); stop it before resetting the breaker.")
        return 1
    breaker = DrawdownBreaker(DRAWDOWN_STATE, limits.max_drawdown_usd)
    was = breaker.reason if breaker.tripped else ""
    breaker.reset()
    log.warning("drawdown breaker reset by hand%s; new peak %.2f",
                f" (was: {was})" if was else " (it was not tripped)", breaker.cumulative())
    return 0


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--submit", action="store_true",
                        help="send orders to IBKR (default: dry run)")
    parser.add_argument("--reset-state", action="store_true",
                        help="move corrupt state files aside and exit")
    parser.add_argument("--reset-drawdown", action="store_true",
                        help="re-arm a tripped multi-day drawdown breaker and exit")
    parser.add_argument("--symbol", help="symbol to evaluate (requires --check-only)")
    parser.add_argument("--check-only", action="store_true",
                        help="evaluate --symbol once and exit; never sends orders")
    parser.add_argument("--strategy", metavar="KEY",
                        help="rules.json strategy to run (default: menu, or the saved choice)")
    parser.add_argument("--yes", action="store_true", help="skip the start-up confirmation")
    parser.add_argument("--clear-stop", action="store_true",
                        help="delete a STOP file left by `paper-bot stop` before starting")
    args = parser.parse_args(argv)
    if args.check_only and not args.symbol:
        parser.error("--check-only needs --symbol")
    if args.symbol and not args.check_only:
        parser.error("--symbol is only supported with --check-only")
    if args.check_only and args.submit:
        parser.error("--check-only never sends orders; drop --submit")
    setup_logging()

    if args.check_only:
        sys.exit(check_only(args.symbol, args.strategy))

    if args.reset_drawdown:
        sys.exit(reset_drawdown())

    if args.reset_state:
        strategy_states = [p.state_file for p in selector.load_each().values()
                           if not isinstance(p, profiles.ConfigError)]
        backups = runstate.reset_corrupt([RISK_STATE, DRAWDOWN_STATE, *strategy_states])
        for b in backups:
            log.warning("moved corrupt state file to %s", b.name)
        if not backups:
            print("No corrupt state files; nothing changed.")
        if any(b.name.startswith("strategy_state.json") for b in backups):
            print("Strategy state was reset: positions with a working paperbot stop are "
                  "re-adopted from that stop; others are left alone and reported.")
        return

    # Everything below is checked BEFORE any IBKR connection: broker mode and
    # connection, the strategy (menu / --strategy / saved choice), its limits.
    cfg = json.loads(CONFIG.read_text())
    conn = connection_settings(cfg)
    interactive = sys.stdin.isatty()
    if runstate.stop_requested():
        if not args.clear_stop:
            log.info("STOP file present: not starting (delete it, or `paper-bot start` clears it)")
            return
        runstate.STOP_FILE.unlink(missing_ok=True)
        log.info("STOP file removed: starting")
    profile = selector.choose(args.strategy, interactive=interactive, assume_yes=args.yes,
                              mode_text=conn.mode.upper())
    broker.confirm_live(conn, interactive)
    strategy.configure(profile)
    guard = RiskGuard(profile.limits, RISK_STATE, drawdown_path=DRAWDOWN_STATE)  # fails closed
    strategy.load_state()  # same check for the strategy state, before connecting
    preflight(conn, profile, guard, cfg)
    runstate.claim_pid()
    try:
        with procs.keep_awake():
            run_session(conn, cfg, profile, guard, args.submit)
    finally:
        wait_for_dashboard()
        runstate.release_pid()


def preflight(conn: broker.Connection, profile, guard: RiskGuard, cfg: dict,
              rules_path: Path | None = None):
    """Log what is about to run and refuse to start (ALERT + SystemExit) unless the
    selected strategy (saved choice, else default), the strategy module, the risk
    guard and rules.json on disk all agree with `profile`. Called after configure(),
    before anything connects."""
    rules_path = rules_path or RULES
    try:
        text = rules_path.read_bytes()
        on_disk = profiles.load(profile.key, rules_path, cfg)
    except (OSError, profiles.ConfigError) as e:
        problems = [f"cannot re-check {rules_path.name}: {e}"]
        text, on_disk = b"", None
    else:
        problems = []
    if on_disk is not None and (on_disk.raw != profile.raw or on_disk.limits != profile.limits):
        problems.append(f"{rules_path.name} changed since strategy {profile.key} was loaded")
    try:
        selected = selector.unattended_key(rules_path)  # the saved choice, else the default
    except profiles.ConfigError as e:
        selected = f"invalid ({e})"
    if selected != profile.key:
        problems.append(f"selected strategy {selected!r} is not {profile.key!r}")
    if strategy.load_profile() is not profile:
        problems.append(f"strategy module runs {strategy.load_profile().key!r}, not {profile.key!r}")
    if guard.limits != profile.limits:
        problems.append("risk guard limits differ from the strategy's limits")

    digest = hashlib.sha256(text).hexdigest()[:12]
    log.info("PREFLIGHT %s", conn.describe())
    log.info("PREFLIGHT strategy %s (%s) from %s sha256 %s", profile.key, profile.label,
             rules_path.name, digest)
    for line in profile.describe()[1:]:
        log.info("PREFLIGHT %s", line.strip())
    log.info("PREFLIGHT limits %s", ", ".join(f"{k}={v}" for k, v in vars(profile.limits).items()))
    if problems:
        for problem in problems:
            log.error("ALERT refusing to start: %s", problem)
        raise SystemExit(f"Refusing to start: {problems[0]}")


def connection_settings(cfg: dict) -> broker.Connection:
    """config.json's broker connection for its mode; an ALERT and SystemExit if invalid."""
    try:
        return broker.settings(cfg)
    except profiles.ConfigError as e:
        log.error("ALERT refusing to start: %s", e)
        raise SystemExit(f"Refusing to start: {e}") from None


def open_session(ib: IB, conn: broker.Connection) -> tuple[str, object] | None:
    """Connect, check the account matches the mode, start P&L. None if Gateway is not
    reachable (retried by the caller); SystemExit if the account is wrong."""
    try:
        ib.connect(conn.host, conn.port, clientId=conn.client_id, timeout=15,
                   fetchFields=StartupFetchALL & ~StartupFetch.EXECUTIONS)
    except (OSError, asyncio.TimeoutError, TimeoutError, ConnectionError) as e:
        log.warning("cannot connect to IB Gateway at %s:%s (clientId %s): %r",
                    conn.host, conn.port, conn.client_id, e)
        ib.disconnect()
        return None
    account = broker.verify_account(conn, ib.managedAccounts())
    md._tickers.clear()  # streaming quotes of an old connection are dead
    pnl = ib.reqPnL(account)
    ib.sleep(3)  # give IBKR time to send the first P&L update
    return account, pnl


def run_session(conn: broker.Connection, cfg: dict, profile, guard: RiskGuard, submit: bool,
                ib: IB | None = None, max_passes: int | None = None):
    """The long-running loop: one strategy pass every loop_seconds, the built-in
    scheduler's daily jobs, and automatic reconnection when IB Gateway restarts or
    the network drops (protective stops stay at IBKR meanwhile). Ends on a STOP
    file or Ctrl+C. max_passes is for tests."""
    ib = ib or IB()
    jobs = clock.Clock(clock.default_jobs(profile, rotate_logs_now))
    ib.orderStatusEvent += lambda t: journal(
        "status", orderId=t.order.orderId, status=t.orderStatus.status,
        filled=t.orderStatus.filled)
    ib.execDetailsEvent += lambda t, fill: journal(
        "fill", orderId=fill.execution.orderId, symbol=fill.contract.symbol,
        side=fill.execution.side, shares=fill.execution.shares,
        price=fill.execution.price)
    account = pnl = None
    failures, passes = 0, 0
    day_orders_confirmed: dt.date | None = None  # post-close check done for this date
    try:
        while max_passes is None or passes < max_passes:
            jobs.tick()
            if runstate.stop_requested():
                if ib.isConnected():
                    cancel_working(ib, "STOP file")
                log.warning("STOP file found, shutting down (protective stops left at IBKR)")
                break
            if not ib.isConnected():
                session = open_session(ib, conn)
                if session is None:
                    failures += 1
                    wait = min(RECONNECT_MAX_SECONDS, RECONNECT_FIRST_SECONDS * 2 ** (failures - 1))
                    if failures == 1:
                        log.error("ALERT IB Gateway not reachable at %s:%s; retrying every "
                                  "%s-%ss (stops stay at IBKR)", conn.host, conn.port,
                                  RECONNECT_FIRST_SECONDS, RECONNECT_MAX_SECONDS)
                    _wait(ib, wait)
                    continue
                if account is not None and session[0] != account:
                    raise SystemExit(f"Refusing to continue: account changed from {account} "
                                     f"to {session[0]} after reconnecting")
                account, pnl = session
                log.info("%s to %s account %s, mode %s, strategy %s, limits %s",
                         "connection to IB Gateway restored; connected" if failures else "connected",
                         conn.mode, account, "SUBMIT" if submit else "DRY RUN", profile.key,
                         profile.limits)
                failures = 0
            try:
                day_orders_confirmed = one_pass(ib, guard, cfg, account, pnl, submit,
                                                day_orders_confirmed)
            except (ConnectionError, OSError, asyncio.TimeoutError) as e:
                log.error("ALERT connection to IB Gateway lost (%r); reconnecting", e)
                ib.disconnect()
                continue
            passes += 1
            _wait(ib, cfg["loop_seconds"])
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


def _wait(ib: IB, seconds: float):
    """Sleep in short steps (keeps IBKR events flowing; a STOP file is seen quickly)."""
    deadline = time.monotonic() + seconds
    while (left := deadline - time.monotonic()) > 0 and not runstate.stop_requested():
        ib.sleep(min(5.0, left))


def one_pass(ib: IB, guard: RiskGuard, cfg: dict, account: str, pnl, submit: bool,
             day_orders_confirmed: dt.date | None = None) -> dt.date | None:
    """One strategy pass: loss checks, fills, post-close check, decide -> guard -> IBKR."""
    daily = daily_pnl(ib, account, pnl)  # 0.0 when provably flat today; None blocks entries
    was_halted = guard.halted
    guard.update_daily_pnl(daily)
    if guard.halted and not was_halted:
        cancel_working(ib, guard.halt_reason)
    notify_fills_safely(ib, account)
    now = dt.datetime.now(ET)
    if post_close(now) and day_orders_confirmed != now.date():
        if await_day_orders_done(ib, submit):
            day_orders_confirmed = now.date()

    held_back: set[str] = set()  # symbols whose cancel was not confirmed
    for request in strategy.decide(ib, account):
        symbol = request.symbol.upper()
        if symbol in held_back:
            log.warning("skipping %s: waiting for a cancel on %s", request, symbol)
            continue
        if not handle_request(ib, guard, cfg, account, request, daily, submit):
            held_back.add(symbol)
    after_pass(ib, account, SOURCE, submit)
    return day_orders_confirmed


def connect(ib: IB, cfg: dict, client_id: int | None = None, **kwargs) -> broker.Connection:
    """Connect to IB Gateway for config.json's mode (client_id None = the configured one),
    or log a clear message and exit 1."""
    conn = connection_settings(cfg)
    client_id = conn.client_id if client_id is None else client_id
    try:
        ib.connect(conn.host, conn.port, clientId=client_id, timeout=15, **kwargs)
    except (OSError, asyncio.TimeoutError, TimeoutError, ConnectionError) as e:
        log.error("cannot connect to IB Gateway at %s:%s (clientId %s): %r. Is Gateway "
                  "running, logged in to the %s account, with the API enabled?",
                  conn.host, conn.port, client_id, e, conn.mode)
        raise SystemExit(1) from None
    return conn


def check_only(symbol: str, key: str | None = None) -> int:
    """--symbol X --check-only: evaluate one symbol and print the result.

    Uses the given strategy (else the saved choice) without changing the saved
    choice, and a read-only connection with its own clientId (client_id + 2), so
    it can run while the bot is running and can never place an order.
    """
    try:
        key = key or selector.unattended_key()
    except profiles.ConfigError as e:
        raise SystemExit(f"Refusing: {e}") from None
    strategy.configure(profiles.require(key))
    cfg = json.loads(CONFIG.read_text())
    conn = connection_settings(cfg)
    ib = IB()
    try:
        connect(ib, cfg, conn.client_id + 2, readonly=True)
        account = broker.verify_account(conn, ib.managedAccounts())
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
    log.info("  logged to %s", strategy.safety_log_path())
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
        if (e.execId in seen or ref_role(e.orderRef) is None
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
        stop = (positions.get(symbol, {}).get("initial_stop")
                if ref_role(e.orderRef) == "entry" else None)
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


AFTER_PASS_WARN_SECONDS = 2.0  # heartbeat + telemetry slower than this is logged
_dashboard_worker: threading.Thread | None = None


def _refresh_dashboard():
    try:
        from paperbot.reporting import compute_perf  # pandas: imported only when needed
        compute_perf.refresh_dashboard()
    except Exception as e:  # noqa: BLE001
        log.warning("dashboard refresh skipped: %r", e)


def refresh_dashboard_in_background():
    """Rebuild the dashboard (files only, no IBKR calls) in one background thread;
    skipped while the previous rebuild is still running."""
    global _dashboard_worker
    if _dashboard_worker is not None and _dashboard_worker.is_alive():
        log.debug("dashboard refresh still running; this pass skips it")
        return
    _dashboard_worker = threading.Thread(target=_refresh_dashboard, name="dashboard",
                                         daemon=True)
    _dashboard_worker.start()


def wait_for_dashboard(timeout: float = 30.0):
    """Let a running dashboard rebuild finish (a process about to exit)."""
    if _dashboard_worker is not None:
        _dashboard_worker.join(timeout)


def after_pass(ib: IB, account: str, source: str, submit: bool):
    """After every bot loop / cycle pass: heartbeat and telemetry (inline: they read
    the ib cache, which is not thread-safe), then the dashboard in the background.
    Never raises."""
    started = time.monotonic()
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
    elapsed = time.monotonic() - started
    if elapsed > AFTER_PASS_WARN_SECONDS:
        log.warning("heartbeat + telemetry took %.1f s (over %.0f s)", elapsed,
                    AFTER_PASS_WARN_SECONDS)
    refresh_dashboard_in_background()


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

    if ref_role(first.orderRef) == "entry":
        try:
            stop = strategy.load_state()["positions"].get(symbol, {}).get("stop")
        except Exception:  # noqa: BLE001
            stop = None
        stop_text = f"${stop:.2f}" if stop else "n/a"
        return f"BUY {symbol}", f"@ ${price:.2f}, stop {stop_text}, qty {shares:g}"
    if ref_role(first.orderRef) == "stop":
        return f"STOP {symbol}", f"exit ${price:.2f}, {pnl}"
    remaining = held.get(symbol, 0)
    if remaining > 0:
        return f"PARTIAL {symbol}", f"sold {shares:g}/{shares + remaining:g} @ ${price:.2f}"
    return f"EXIT {symbol}", f"@ ${price:.2f}, {pnl}"


def account_state(ib: IB, account: str, open_trades: list[Trade]) -> AccountState:
    net_liq = net_liquidation_usd(ib, account)  # converted from the base currency if needed
    longs = {p.contract.symbol.upper(): p.position for p in ib.positions(account)
             if p.contract.secType == "STK" and p.position > 0}
    buys = {t.contract.symbol.upper() for t in open_trades if t.order.action == "BUY"}
    return AccountState(net_liq, longs, buys, buying_power_usd(ib, account))


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
                 if t.order.orderRef == strategy.refs()[1] and not t.isDone()
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
                          tif="GTC", orderRef=strategy.refs()[1], ocaGroup=oca_group(req.symbol),
                          ocaType=OCA_REDUCE_WITH_BLOCK)
        ib.placeOrder(contract, order)
        log.info("PLACED stop %s: %s %s @ %.2f (%s)", order.orderId, req.symbol,
                 req.quantity, req.stop_price, req.reason)
    journal("stop", orderId=order.orderId, stop=req.__dict__)


def handle_intent(ib: IB, guard: RiskGuard, cfg: dict, account: str,
                  intent: OrderIntent, daily: float | None, submit: bool):
    """Send an entry (as a bracket with its protective stop) or a closing exit."""
    open_trades = ib.reqAllOpenOrders()
    working = sum(1 for t in open_trades if ref_role(t.order.orderRef) != "stop")
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
        orders = [_order(intent, account, cfg["time_in_force"], strategy.refs()[2])]
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


def oca_group(symbol: str) -> str:
    """A new, unique OCA group name for one protective stop. Never reused: IBKR
    cancels new orders sent into a group in which an order has already filled."""
    return f"paperbot:{symbol.upper()}:{dt.datetime.now(ET):%Y%m%d}:{uuid.uuid4().hex[:12]}"


def _order(intent: OrderIntent, account: str, tif: str, ref: str):
    extra = {}
    if intent.good_till is not None:
        tif = "GTD"  # IBKR expires it at good_till even if this bot is down
        extra["goodTillDate"] = intent.good_till.astimezone(ET).strftime("%Y%m%d %H:%M:%S US/Eastern")
    if intent.oca_group:
        extra.update(ocaGroup=intent.oca_group, ocaType=OCA_REDUCE_WITH_BLOCK)
    if intent.limit_price is None:
        return MarketOrder(intent.action, intent.quantity, account=account, tif=tif,
                           orderRef=ref, **extra)
    return LimitOrder(intent.action, intent.quantity, intent.limit_price,
                      account=account, tif=tif, orderRef=ref, **extra)


def entry_bracket(ib: IB, intent: OrderIntent, account: str, tif: str) -> list:
    """Parent BUY plus a child SELL STP (GTC) in its own OCA group. IBKR activates
    the stop when the parent fills, and cancels it if the parent is cancelled or
    expires (GTD) unfilled."""
    ref_entry, ref_stop, _ = strategy.refs()
    parent = _order(intent, account, tif, ref_entry)
    parent.orderId = ib.client.getReqId()
    parent.transmit = False  # sent together with the child below
    stop = StopOrder("SELL", intent.quantity, intent.stop_price, account=account,
                     tif="GTC", orderRef=ref_stop, parentId=parent.orderId, transmit=True,
                     ocaGroup=oca_group(intent.symbol), ocaType=OCA_REDUCE_WITH_BLOCK)
    return [parent, stop]


if __name__ == "__main__":
    main()
