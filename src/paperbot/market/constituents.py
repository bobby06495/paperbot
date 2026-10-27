"""Index members for the morning prefilter: live from Wikipedia, cached, combined.

    from paperbot.market import constituents
    found = constituents.resolve(["S&P 500", "Nasdaq-100"], cache_dir=data_dir / "constituents")
    found.symbols        # sorted IBKR tickers, each once ("BRK B")
    found.members["AAPL"]  # ("S&P 500", "Nasdaq-100"): the indexes a ticker came from

Each index is fetched live (sp500_tickers.get_live_index_tickers: the page, the
ticker column and the fewest rows a healthy table has). A good list is saved
to <cache_dir>/<index>.json with its time and source. When the live fetch fails
the saved list is used if it is at most MAX_CACHE_AGE old (an alert says so);
older or missing, that index is left out (alert) and the others still count.
No index at all -> found.symbols is empty and the caller fails the run, so the
bot keeps an earlier watchlist from today or its fallback file.

Tickers are upper-cased and de-duplicated across indexes; anything that does not
look like a US ticker is set aside in found.invalid with the reason (logged by
the prefilter, never sent to IBKR). Read-only web access; never connects to IBKR.
"""
import dataclasses
import datetime as dt
import json
import os
import re
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
MAX_CACHE_AGE = dt.timedelta(days=7)
TICKER = re.compile(r"^[A-Z][A-Z0-9]{0,5}( [A-Z0-9]{1,2})?$")  # IBKR format: "BRK B"


@dataclasses.dataclass
class Found:
    members: dict[str, tuple[str, ...]] = dataclasses.field(default_factory=dict)
    sources: dict[str, dict] = dataclasses.field(default_factory=dict)  # index -> how it was got
    failed: dict[str, str] = dataclasses.field(default_factory=dict)  # index -> why it is missing
    invalid: dict[str, str] = dataclasses.field(default_factory=dict)  # raw ticker -> why
    alerts: list[str] = dataclasses.field(default_factory=list)

    @property
    def symbols(self) -> list[str]:
        return sorted(self.members)


def cache_file(cache_dir: Path, index: str) -> Path:
    slug = re.sub(r"[^a-z0-9]+", "_", index.lower()).strip("_")
    return cache_dir / f"{slug}.json"


def read_cache(cache_dir: Path, index: str) -> tuple[list[str], dt.datetime] | None:
    try:
        saved = json.loads(cache_file(cache_dir, index).read_text())
        fetched = dt.datetime.fromisoformat(saved["fetched_at"])
        symbols = [str(s) for s in saved["symbols"]]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if saved.get("index") != index or not symbols or fetched.tzinfo is None:
        return None
    return symbols, fetched


def write_cache(cache_dir: Path, index: str, symbols: list[str], now: dt.datetime, source: str):
    path = cache_file(cache_dir, index)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"index": index, "fetched_at": now.isoformat(timespec="seconds"),
                               "source": source, "count": len(symbols), "symbols": symbols},
                              indent=1))
    os.replace(tmp, path)


def resolve(indexes, fetch: Callable[[str], list[str]], cache_dir: Path,
            now: dt.datetime | None = None, max_age: dt.timedelta = MAX_CACHE_AGE,
            source_of: Callable[[str], str] = lambda index: "wikipedia") -> Found:
    """Members of every index in `indexes`, live where possible, cached otherwise."""
    now = now or dt.datetime.now(ET)
    found = Found()
    for index in indexes:
        try:
            symbols = list(fetch(index))
        except Exception as e:  # noqa: BLE001 - one index down must not stop the others
            cached = read_cache(cache_dir, index)
            age = now - cached[1] if cached else None
            if cached and age <= max_age:
                symbols = cached[0]
                found.sources[index] = {"source": "cache", "count": len(symbols),
                                        "fetched_at": cached[1].isoformat(timespec="seconds")}
                found.alerts.append(f"{index}: live member list unavailable ({e!r}); using the "
                                    f"list cached {cached[1]:%Y-%m-%d %H:%M} "
                                    f"({age.days} day(s) old)")
            else:
                why = (f"cached list too old ({cached[1]:%Y-%m-%d}, max {max_age.days} days)"
                       if cached else "no cached list")
                found.failed[index] = f"live member list unavailable ({e!r}); {why}"
                found.alerts.append(f"{index} left out of today's screen: {found.failed[index]}")
                continue
        else:
            found.sources[index] = {"source": source_of(index), "count": len(symbols),
                                    "fetched_at": now.isoformat(timespec="seconds")}
            try:
                write_cache(cache_dir, index, symbols, now, source_of(index))
            except OSError as e:
                found.alerts.append(f"{index}: member list not cached ({e!r})")
        for raw in symbols:
            symbol = str(raw).strip().upper()
            if not TICKER.match(symbol):
                found.invalid.setdefault(str(raw), f"not a valid ticker (from {index})")
                continue
            found.members[symbol] = found.members.get(symbol, ()) + (index,)
    return found
