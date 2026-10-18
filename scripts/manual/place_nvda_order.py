"""Place a BUY 1 MU market order (outsideRth) on the IB Gateway PAPER account.

    python place_nvda_order.py

Refuses to run unless the connection is on the Gateway paper port and every
managed account is a paper account (IDs start with "D"). Exits 1 if the
connection fails, 2 if the order is rejected. Nothing happens on import.
"""
import asyncio
import sys

from ib_async import IB, MarketOrder, Stock, StartupFetch, StartupFetchALL

HOST, PORT, CLIENT_ID = "127.0.0.1", 4002, 10  # 4002 = Gateway paper port
SYMBOL, QUANTITY = "MU", 1
LOOKUP_TIMEOUT = 20  # seconds to wait for IBKR's contract lookup
SETTLE_TIMEOUT = 10  # seconds to wait for the order status to leave PendingSubmit
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

        contract = Stock(SYMBOL, "SMART", "USD")
        try:
            ib.run(asyncio.wait_for(ib.qualifyContractsAsync(contract), LOOKUP_TIMEOUT))
        except asyncio.TimeoutError:
            raise SystemExit(
                f"IBKR did not answer the {SYMBOL} contract lookup within "
                f"{LOOKUP_TIMEOUT}s (usually weekend/nightly server maintenance). "
                "Nothing was submitted; try again later.")
        if not contract.conId:
            raise SystemExit(f"Could not find contract {SYMBOL}. Nothing was submitted.")

        order = MarketOrder("BUY", QUANTITY, account=account, outsideRth=True)
        print(f"Paper account {account}: BUY {QUANTITY} {contract.symbol} "
              f"({contract.primaryExchange}) MKT, outsideRth")
        trade = ib.placeOrder(contract, order)

        # Wait until the status settles past PendingSubmit (or reaches a final state).
        for _ in range(SETTLE_TIMEOUT * 2):
            status = trade.orderStatus.status
            if status in DONE or (status and status != "PendingSubmit"):
                break
            ib.sleep(0.5)

        status = trade.orderStatus.status
        fill = trade.orderStatus.avgFillPrice if trade.orderStatus.filled else None
        print("Order ID:", trade.order.orderId)
        print("Fill price:", f"{fill:.2f}" if fill else "pending")
        print("Final status:", status)

        if status in ("Cancelled", "ApiCancelled", "Inactive"):
            print("Order was rejected/cancelled. trade.log:")
            for entry in trade.log:
                print(f"  {entry.time} {entry.status} {entry.errorCode or ''} {entry.message}")
            sys.exit(2)
    finally:
        ib.disconnect()


if __name__ == "__main__":
    main()
