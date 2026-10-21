"""Live index constituents from Wikipedia, in Interactive Brokers ticker format.

    from paperbot.market.sp500_tickers import get_live_index_tickers, get_live_sp500_tickers
    symbols = get_live_sp500_tickers()                 # ~503 symbols, sorted, e.g. "BRK B"
    symbols = get_live_index_tickers("Nasdaq-100")     # ~101 symbols (QQQ's holdings)

The indexes (rules.json universe_filters.index) are listed in PAGES: each has its
Wikipedia page, the ticker column and the fewest rows a healthy table has.

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
NASDAQ100_URL = "https://en.wikipedia.org/wiki/List_of_NASDAQ-100_companies"
USER_AGENT = "paperbot/1.0 (personal paper-trading research script; python-requests)"
MIN_EXPECTED = 450  # the index has ~503 lines; far fewer means the page layout changed

# index name (rules.json universe_filters.index) -> (page, ticker column, fewest rows)
PAGES = {
    "S&P 500": (SP500_URL, "Symbol", MIN_EXPECTED),
    "Nasdaq-100": (NASDAQ100_URL, "Ticker", 95),  # ~101 lines (some share classes)
}


def to_ibkr(symbol: str) -> str:
    """'BRK.B' / 'BRK-B' -> 'BRK B'."""
    return symbol.strip().upper().replace(".", " ").replace("-", " ")


def to_yahoo(symbol: str) -> str:
    """'BRK B' / 'BRK.B' -> 'BRK-B'."""
    return symbol.strip().upper().replace(" ", "-").replace(".", "-")


def get_live_index_tickers(index: str, timeout: float = 20) -> list[str]:
    """Today's constituents of `index` (a PAGES key) in IBKR format, sorted.

    Raises KeyError for an unknown index, requests.RequestException on network
    errors and ValueError if no table on the page looks like the member list.
    """
    url, column, fewest = PAGES[index]
    response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    response.raise_for_status()
    tables = [t for t in pd.read_html(StringIO(response.text)) if column in t.columns]
    if not tables:
        raise ValueError(f"no {index} table with a {column!r} column on {url}")
    table = max(tables, key=len)
    symbols = sorted({to_ibkr(s) for s in table[column].astype(str) if s.strip()})
    if len(symbols) < fewest:
        raise ValueError(f"expected at least {fewest} {index} symbols, got {len(symbols)}")
    return symbols


def get_live_sp500_tickers(timeout: float = 20) -> list[str]:
    """Today's S&P 500 constituents in IBKR format, sorted alphabetically."""
    return get_live_index_tickers("S&P 500", timeout)
