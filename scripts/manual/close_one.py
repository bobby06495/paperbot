"""Close the MU position on the IB Gateway PAPER account with a market SELL.

    python close_one.py

Refuses to run unless the connection is on the Gateway paper port and every
managed account is a paper account (IDs start with "D"). Exits 0 with
"NO POSITION TO CLOSE" if there is no long MU position, 1 if the connection
fails, 2 if the order is rejected. Nothing happens on import.
"""
import sys

from ib_async import IB, MarketOrder, StartupFetch, StartupFetchALL

HOST, PORT, CLIENT_ID = "127.0.0.1", 4002, 11  # 11 so it doesn't collide with the buy script's 10
SYMBOL = "MU"
SETTLE_TIMEOUT = 10  # seconds to wait for a final fill status
DONE = {"Filled", "Cancelled", "ApiCancelled", "Inactive"}


def main():
    ib = IB()
    try:
        # Skip the startup executions download; it isn't needed to place an order.
        ib.connect(HOST, PORT, clientId=CLIENT_ID, timeout=15,
                   fetchFields=StartupFetchALL & ~StartupFetch.EXECUTIONS)
    except Exception as e:
        print(f"Connection failed: {e!r}")
        sys.exit(1)

    try:
        accounts = ib.managedAccounts()
        if PORT != 4002 or not accounts or not all(a.startswith("D") for a in accounts):
            raise SystemExit(f"Refusing to trade: not a paper account {accounts}")
        account = accounts[0]

        position = next((p for p in ib.positions(account)
                         if p.contract.symbol == SYMBOL and p.position > 0), None)
        if position is None:
            print("NO POSITION TO CLOSE")
            sys.exit(0)

        contract = position.contract
        contract.exchange = "SMART"  # positions come back without a routing exchange
        quantity = position.position
        order = MarketOrder("SELL", quantity, account=account, outsideRth=True)
        print(f"Paper account {account}: SELL {quantity:g} {SYMBOL} MKT, outsideRth")
        trade = ib.placeOrder(contract, order)

        for _ in range(SETTLE_TIMEOUT * 2):
            if trade.orderStatus.status in DONE:
                break
            ib.sleep(0.5)

        status = trade.orderStatus.status
        filled = trade.orderStatus.filled
        print("Sold quantity:", f"{filled:g}")
        print("Fill price:", f"{trade.orderStatus.avgFillPrice:.2f}" if filled else "pending")
        print("Status:", status)

        if status in ("Cancelled", "ApiCancelled", "Inactive"):
            print("Order was rejected/cancelled. trade.log:")
            for entry in trade.log:
                print(f"  {entry.time} {entry.status} {entry.errorCode or ''} {entry.message}")
            sys.exit(2)
    finally:
        ib.disconnect()


if __name__ == "__main__":
    main()
