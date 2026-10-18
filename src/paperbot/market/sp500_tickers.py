"""Live S&P 500 constituents from Wikipedia, in Interactive Brokers ticker format.

    from paperbot.market.sp500_tickers import get_live_sp500_tickers
    symbols = get_live_sp500_tickers()   # ~503 symbols, sorted, e.g. "BRK B"

Share classes use a space in IBKR ("BRK B"), a period on Wikipedia ("BRK.B")
and a hyphen on Yahoo Finance ("BRK-B"); to_ibkr() and to_yahoo() convert.

Wikipedia rejects Python's default user agent, so the page is fetched with
requests and a descriptive User-Agent, then parsed with pandas.read_html.
Read-only web access; never connects to IBKR.
"""
from io import StringIO

import pandas as pd
import requests

SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
USER_AGENT = "paperbot/1.0 (personal paper-trading research script; python-requests)"
MIN_EXPECTED = 450  # the index has ~503 lines; far fewer means the page layout changed


def to_ibkr(symbol: str) -> str:
    """'BRK.B' / 'BRK-B' -> 'BRK B'."""
    return symbol.strip().upper().replace(".", " ").replace("-", " ")


def to_yahoo(symbol: str) -> str:
    """'BRK B' / 'BRK.B' -> 'BRK-B'."""
    return symbol.strip().upper().replace(" ", "-").replace(".", "-")


def get_live_sp500_tickers(timeout: float = 20) -> list[str]:
    """Today's S&P 500 constituents in IBKR format, sorted alphabetically.

    Raises requests.RequestException on network errors and ValueError if the
    table does not look like the S&P 500 list.
    """
    response = requests.get(SP500_URL, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    response.raise_for_status()
    table = pd.read_html(StringIO(response.text))[0]
    if "Symbol" not in table.columns:
        raise ValueError(f"unexpected Wikipedia table columns: {list(table.columns)}")
    symbols = sorted({to_ibkr(s) for s in table["Symbol"].astype(str) if s.strip()})
    if len(symbols) < MIN_EXPECTED:
        raise ValueError(f"expected ~503 S&P 500 symbols, got {len(symbols)}")
    return symbols
