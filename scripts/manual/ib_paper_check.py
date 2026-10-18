"""Read-only ib_async connection check against the IB Gateway paper account.

    python ib_paper_check.py

Connects in read-only mode, prints the account summary and position count,
then disconnects. Never places, modifies or cancels orders. Nothing happens
on import.
"""
from ib_async import IB

HOST, PORT, CLIENT_ID = "127.0.0.1", 4002, 1  # 4002 = Gateway paper port


def main():
    ib = IB()
    ib.connect(HOST, PORT, clientId=CLIENT_ID, readonly=True, timeout=15)
    try:
        accounts = ib.managedAccounts()
        if not accounts or not all(a.startswith("D") for a in accounts):
            raise SystemExit(f"Refusing to continue: non-paper account(s) {accounts}")
        print("Connected (read-only) to paper account(s):", accounts)
        print("Server time:", ib.reqCurrentTime())
        summary = {v.tag: v.value for v in ib.accountSummary(accounts[0])
                   if v.tag in ("NetLiquidation", "TotalCashValue", "BuyingPower")}
        print("Account summary:", summary)
        print("Positions:", len(ib.positions()))
    finally:
        ib.disconnect()


if __name__ == "__main__":
    main()
