"""Which index(es) a watchlist strategy's morning prefilter screens (rules.json).

A strategy whose universe is the morning watchlist names its index there:

    "universe": {"source": "watchlist", "index": "S&P 500", "top_n": 100,
                 "fallback_file": "universe.txt"}

or several indexes at once (one list, de-duplicated across overlapping indexes):

    "universe": {"source": "watchlist", "fallback_file": "universe.txt",
                 "indexes": ["S&P 500", "Nasdaq-100", "S&P MidCap 400", "S&P SmallCap 600"]}

  index / indexes one of INDEXES, or a list of them (not both keys). The live member
                  lists come from Wikipedia (market/constituents.py, cached in
                  data/constituents/). Russell 2000 has no free member list and is not
                  supported for constituent screening.
  top_n           one index: the largest members by market cap the prefilter checks at
                  IBKR (default 100; the Nasdaq-100 has 101 lines, so 101 screens all).
                  several indexes: the members with the largest gap in a first, broad
                  Yahoo screen that the prefilter then checks at IBKR (default 100).
  fallback_file   the config/ list the bot scans when there is no watchlist for today's
                  index set (default universe.txt; config/universe_nasdaq100.txt ships
                  with the Nasdaq-100 members)

A strategy with a fixed symbol list (an ETF strategy) may also name indexes; each one
adds the ETF that tracks it (INDEX_ETFS), never the index members:

    "universe": {"symbols": ["XLK", ...], "indexes": ["S&P 500", "Russell 2000"]}
    -> XLK, ..., SPY, IWM

The prefilter's gap and price thresholds are the strategy's own entry blocks
(min_gap_pct, min_price / min_prior_close), so the screen and the entry checks
always agree; one edit in rules.json moves both.

validate() is used by core/profile.py when rules.json is loaded, so a typo stops
the bot and the prefilter before anything runs (fail closed). Standard library only.
"""
import dataclasses
import json
from pathlib import Path

from paperbot import paths
from paperbot.core.risk import ConfigError

INDEXES = ("S&P 500", "Nasdaq-100", "S&P MidCap 400", "S&P SmallCap 600")  # constituents
INDEX_ETFS = {  # index -> the ETF that tracks it (symbol universes: ETFs only)
    "S&P 500": "SPY",
    "Nasdaq-100": "QQQ",
    "S&P MidCap 400": "MDY",
    "S&P SmallCap 600": "IJR",
    "Russell 2000": "IWM",
}
LABEL_SEPARATOR = " + "
DEFAULT_INDEX = "S&P 500"
DEFAULT_TOP_N = 100
DEFAULT_FALLBACK = "universe.txt"
DEFAULT_MIN_GAP = 3.0
DEFAULT_MIN_PRICE = 3.0
WATCHLIST_KEYS = {"source", "index", "indexes", "top_n", "fallback_file"}


def label(indexes) -> str:
    """The canonical name of an index set, as written in the watchlist header:
    "S&P 500" for one index, "S&P 500 + Nasdaq-100 + ..." (INDEXES order) for several."""
    order = {name: i for i, name in enumerate(INDEXES)}
    return LABEL_SEPARATOR.join(sorted(set(indexes), key=lambda n: (order.get(n, 99), n)))


def parse_label(text: str) -> frozenset[str]:
    """The index set named by a watchlist header ("S&P 500 + Nasdaq-100")."""
    return frozenset(part.strip() for part in text.split(LABEL_SEPARATOR.strip()) if part.strip())


def indexes_of(uni: dict) -> tuple[str, ...]:
    """The index set of a (validated) watchlist universe, canonical order."""
    names = uni.get("indexes") or [uni.get("index", DEFAULT_INDEX)]
    return tuple(label(names).split(LABEL_SEPARATOR))


@dataclasses.dataclass(frozen=True)
class Settings:
    strategy: str
    index: str  # label(indexes): what the watchlist header records
    top_n: int
    fallback_file: Path
    min_gap: float
    min_price: float
    indexes: tuple[str, ...] = ()

    @property
    def multi(self) -> bool:
        return len(self.indexes) > 1

    def describe(self) -> str:
        how = (f"top {self.top_n} by gap in a broad Yahoo screen" if self.multi
               else f"top {self.top_n} by market cap")
        return (f"{self.index} ({how}), gap >= {self.min_gap:g}%, "
                f"price >= ${self.min_price:g}; fallback {self.fallback_file.name}")


def validate(where: str, value: dict) -> dict:
    """A watchlist universe with its defaults filled in. ConfigError if invalid."""
    unknown = sorted(set(value) - WATCHLIST_KEYS)
    if unknown:
        raise ConfigError(f"{where} has unknown keys {unknown}; valid: {', '.join(sorted(WATCHLIST_KEYS))}")
    if "index" in value and "indexes" in value:
        raise ConfigError(f'{where}: use either "index" or "indexes", not both')
    if "indexes" in value:
        names = value["indexes"]
        if not (isinstance(names, list) and names and all(isinstance(n, str) for n in names)):
            raise ConfigError(f"{where}.indexes must be a non-empty list of index names")
        if len(set(names)) != len(names):
            raise ConfigError(f"{where}.indexes lists an index twice: {names}")
        for name in names:
            if name not in INDEXES:
                raise ConfigError(f"{where}.indexes: {name!r} is not supported for constituent "
                                  f"screening; use any of: {', '.join(INDEXES)}")
        value = {**value, "indexes": list(indexes_of({"indexes": names}))}
    index = value.get("index", DEFAULT_INDEX)
    if "indexes" not in value and index not in INDEXES:
        raise ConfigError(f"{where}.index {index!r} is not supported; use one of: {', '.join(INDEXES)}")
    top_n = value.get("top_n", DEFAULT_TOP_N)
    if isinstance(top_n, bool) or not isinstance(top_n, int) or top_n < 1:
        raise ConfigError(f"{where}.top_n must be a whole number of at least 1, got {top_n!r}")
    fallback = value.get("fallback_file", DEFAULT_FALLBACK)
    if not isinstance(fallback, str) or not fallback.strip():
        raise ConfigError(f"{where}.fallback_file must be a file name in config/, got {fallback!r}")
    if "indexes" in value:
        return {**value, "top_n": top_n, "fallback_file": fallback}
    return {**value, "index": index, "top_n": top_n, "fallback_file": fallback}


def etf_symbols(where: str, symbols: list[str], names) -> list[str]:
    """A symbol universe plus the ETF tracking each named index (INDEX_ETFS), in order,
    without duplicates. Never adds index members. ConfigError for an unknown index."""
    if not (isinstance(names, list) and names and all(isinstance(n, str) for n in names)):
        raise ConfigError(f"{where}.indexes must be a non-empty list of index names")
    unknown = [n for n in names if n not in INDEX_ETFS]
    if unknown:
        raise ConfigError(f"{where}.indexes: {unknown} not supported; use any of: "
                          f"{', '.join(INDEX_ETFS)}")
    return list(dict.fromkeys([*symbols, *(INDEX_ETFS[n] for n in names)]))


def _block_value(blocks, type_name: str, attr: str):
    block = next((b for b in blocks if getattr(b, "type", None) == type_name), None)
    return getattr(block, attr, None) if block is not None else None


def settings(profile) -> Settings:
    """What the prefilter screens for one (validated) watchlist strategy profile."""
    uni = profile.universe
    gap = _block_value(profile.entry, "min_gap_pct", "min_pct")
    price = (_block_value(profile.entry, "min_price", "min_usd")
             or _block_value(profile.entry, "min_prior_close", "min_usd"))
    indexes = indexes_of(uni)
    return Settings(strategy=profile.key, index=label(indexes), indexes=indexes,
                    top_n=int(uni.get("top_n", DEFAULT_TOP_N)),
                    fallback_file=paths.CONFIG_DIR / uni.get("fallback_file", DEFAULT_FALLBACK),
                    min_gap=float(gap if gap is not None else DEFAULT_MIN_GAP),
                    min_price=float(price if price is not None else DEFAULT_MIN_PRICE))


def prefilter_profile(rules_path: Path | None = None, config: dict | None = None):
    """The watchlist strategy the morning prefilter serves: the running/saved strategy
    when its universe is the watchlist, else rules.json's default strategy, else the
    first watchlist strategy. None when no strategy uses the watchlist.
    ConfigError if rules.json is invalid."""
    from paperbot.core import profile as profiles  # here: profile.py imports this module
    from paperbot.core import selector
    rules_path = rules_path or paths.RULES_FILE
    if config is None:
        config = json.loads(paths.CONFIG_FILE.read_text())
    keys = profiles.strategy_keys(rules_path)
    candidates = [selector.saved(), profiles.default_strategy(rules_path), *keys]
    for key in dict.fromkeys(k for k in candidates if k in keys):
        profile = profiles.load(key, rules_path, config)
        if profile.universe.get("source") == "watchlist":
            return profile
    return None
