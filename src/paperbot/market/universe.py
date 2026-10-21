"""Which index a watchlist strategy's morning prefilter screens (rules.json).

A strategy whose universe is the morning watchlist names its index there:

    "universe": {"source": "watchlist", "index": "S&P 500", "top_n": 100,
                 "fallback_file": "universe.txt"}

  index           "S&P 500" (default) or "Nasdaq-100" (QQQ's holdings); the live
                  member list comes from Wikipedia (sp500_tickers.get_live_index_tickers)
  top_n           the largest members by market cap the prefilter screens (default 100;
                  the Nasdaq-100 has 101 lines, so 101 screens all of them)
  fallback_file   the config/ list the bot scans when there is no watchlist for today's
                  index (default universe.txt; config/universe_nasdaq100.txt ships with
                  the Nasdaq-100 members)

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

INDEXES = ("S&P 500", "Nasdaq-100")
DEFAULT_INDEX = "S&P 500"
DEFAULT_TOP_N = 100
DEFAULT_FALLBACK = "universe.txt"
DEFAULT_MIN_GAP = 3.0
DEFAULT_MIN_PRICE = 3.0
WATCHLIST_KEYS = {"source", "index", "top_n", "fallback_file"}


@dataclasses.dataclass(frozen=True)
class Settings:
    strategy: str
    index: str
    top_n: int
    fallback_file: Path
    min_gap: float
    min_price: float

    def describe(self) -> str:
        return (f"{self.index} (top {self.top_n} by market cap), gap >= {self.min_gap:g}%, "
                f"price >= ${self.min_price:g}; fallback {self.fallback_file.name}")


def validate(where: str, value: dict) -> dict:
    """A watchlist universe with its defaults filled in. ConfigError if invalid."""
    unknown = sorted(set(value) - WATCHLIST_KEYS)
    if unknown:
        raise ConfigError(f"{where} has unknown keys {unknown}; valid: {', '.join(sorted(WATCHLIST_KEYS))}")
    index = value.get("index", DEFAULT_INDEX)
    if index not in INDEXES:
        raise ConfigError(f"{where}.index {index!r} is not supported; use one of: {', '.join(INDEXES)}")
    top_n = value.get("top_n", DEFAULT_TOP_N)
    if isinstance(top_n, bool) or not isinstance(top_n, int) or top_n < 1:
        raise ConfigError(f"{where}.top_n must be a whole number of at least 1, got {top_n!r}")
    fallback = value.get("fallback_file", DEFAULT_FALLBACK)
    if not isinstance(fallback, str) or not fallback.strip():
        raise ConfigError(f"{where}.fallback_file must be a file name in config/, got {fallback!r}")
    return {**value, "index": index, "top_n": top_n, "fallback_file": fallback}


def _block_value(blocks, type_name: str, attr: str):
    block = next((b for b in blocks if getattr(b, "type", None) == type_name), None)
    return getattr(block, attr, None) if block is not None else None


def settings(profile) -> Settings:
    """What the prefilter screens for one (validated) watchlist strategy profile."""
    uni = profile.universe
    gap = _block_value(profile.entry, "min_gap_pct", "min_pct")
    price = (_block_value(profile.entry, "min_price", "min_usd")
             or _block_value(profile.entry, "min_prior_close", "min_usd"))
    return Settings(strategy=profile.key, index=uni.get("index", DEFAULT_INDEX),
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
