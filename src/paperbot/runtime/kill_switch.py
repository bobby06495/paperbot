"""Kill switch: stop the bot and cancel its working orders on the paper account.

    paperbot-kill          stop the bot; cancel entries/exits, KEEP protective stops
    paperbot-kill --all    stop the bot; cancel EVERY open order, stops included
    (same as python -m paperbot.runtime.kill_switch)

Always creates the STOP file first, so a running bot exits on its next loop
and will not start again until STOP is deleted.

Default mode keeps the protective stops at IBKR so open positions stay
protected:
  * bot running  -> the bot itself cancels its entry/exit orders on its next
                    loop (it owns them); nothing else is done here
  * bot stopped  -> connects with the bot's clientId (only that client may
                    cancel the bot's orders) and cancels them directly

--all sends a global cancel for the whole paper account. Every open position
is then UNPROTECTED until a stop is placed again (the bot re-creates stops for
its own positions the next time it runs).

Nothing happens on import; the actions run only from the command line.
"""
import argparse
import json

from ib_async import IB, StartupFetch

from paperbot import paths
from paperbot.runtime import runstate
from paperbot.core import broker
from paperbot.core.risk import ConfigError, ref_role


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description="Stop paperbot and cancel its orders.")
    parser.add_argument("--all", action="store_true",
                        help="cancel every open order on the account, protective stops included")
    args = parser.parse_args(argv)

    runstate.request_stop()
    print("STOP file created; a running bot exits on its next loop.")
    bot_pid = runstate.running_pid()
    if bot_pid is not None and not args.all:
        print(f"Bot is running (pid {bot_pid}): it cancels its entry/exit orders itself and "
              "keeps the protective stops. Run paperbot-stop to wait until it has exited.")
        return

    cfg = json.loads(paths.CONFIG_FILE.read_text())
    # The bot's clientId can cancel the bot's orders; while the bot is running
    # that id is taken, and a global cancel works from any client.
    try:
        conn = broker.settings(cfg)
    except ConfigError as e:
        raise SystemExit(f"Refusing: {e}") from None
    client_id = conn.client_id if bot_pid is None else conn.client_id + 1
    ib = IB()
    ib.connect(conn.host, conn.port, clientId=client_id, timeout=15,
               fetchFields=StartupFetch.ORDERS_OPEN | StartupFetch.POSITIONS)
    try:
        account = broker.verify_account(conn, ib.managedAccounts())
        open_trades = ib.reqAllOpenOrders()
        if args.all:
            print(f"{conn.mode.title()} account {account}: GLOBAL CANCEL of {len(open_trades)} open order(s)")
            ib.reqGlobalCancel()
        else:
            ours = [t for t in ib.openTrades()
                    if ref_role(t.order.orderRef) in ("entry", "exit") and not t.isDone()]
            print(f"{conn.mode.title()} account {account}: cancelling {len(ours)} paperbot entry/exit "
                  "order(s); protective stops are kept")
            for t in ours:
                ib.cancelOrder(t.order)
        ib.sleep(3)

        remaining = ib.reqAllOpenOrders()
        print("Still open:", len(remaining))
        for t in remaining:
            role = ("protective stop" if ref_role(t.order.orderRef) == "stop"
                    else t.order.orderRef or "manual")
            print(f"  #{t.order.orderId} {t.order.action} {t.order.totalQuantity:g} "
                  f"{t.contract.symbol} {t.order.orderType} ({role})")
        if args.all:
            for p in ib.positions(account):
                if p.position:
                    print(f"WARNING: {p.contract.symbol} {p.position:g} shares now have "
                          "no protective stop")
    finally:
        ib.disconnect()


if __name__ == "__main__":
    main()
