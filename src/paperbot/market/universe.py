"""Which index the morning prefilter screens, read from rules.json.

    "universe_filters": {"index": "S&P 500", "min_price_usd": 3.0,
                         "top_n": 100, "fallback_file": "universe.txt"}

  index           "S&P 500" or "Nasdaq-100" (QQQ's holdings); its live member list
                  comes from Wikipedia (sp500_tickers.get_live_index_tickers)
  top_n           optional, default 100: the largest members by market cap to screen
  fallback_file   optional, default universe.txt: the config/ file the bot scans
                  when there is no watchlist for today's index
  min_price_usd   the prefilter's price floor (also checked again at entry)

The prefilter's gap threshold is the entry rule D3 (daily_filters
"D3_min_gap_pct_from_prior_close"), so one edit moves both.
settings() fails closed with ConfigError on an unknown index or a bad value.
Standard library only, so the strategy and the setup check can use it cheaply.
"""
import logging
from dataclasses import dataclass
from pathlib import Path

from paperbot import paths
from paperbot.trading.risk import ConfigError

INDEXES = ("S&P 500", "Nasdaq-100")
DEFAULT_INDEX = "S&P 500"
DEFAULT_TOP_N = 100
DEFAULT_FALLBACK = "universe.txt"
KEYS = {"index", "min_price_usd", "top_n", "fallback_file"}


@dataclass(frozen=True)
class Settings:
    index: str
    top_n: int
    fallback_file: Path
    min_price: float
    min_gap: float

    def describe(self) -> str:
        return f"{self.index} (top {self.top_n}), gap >= {self.min_gap:g}%"


def _number(where: str, value, whole=False, zero_ok=False) -> float:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or value < 0 or (value == 0 and not zero_ok)):
        floor = "0 or more" if zero_ok else "greater than 0"
        raise ConfigError(f"{where} must be a number {floor}, got {value!r}")
    if whole and int(value) != value:
        raise ConfigError(f"{where} must be a whole number, got {value!r}")
    return int(value) if whole else float(value)


def settings(rules: dict) -> Settings:
    """The prefilter/universe settings of a parsed rules.json. ConfigError if invalid."""
    uf = rules.get("universe_filters", {})
    if not isinstance(uf, dict):
        raise ConfigError("rules.json universe_filters must be an object")
    unknown = sorted(set(uf) - KEYS)
    if unknown:
        raise ConfigError(f"rules.json universe_filters has unknown keys {unknown}; "
                          f"valid: {', '.join(sorted(KEYS))}")
    index = uf.get("index", DEFAULT_INDEX)
    if index not in INDEXES:
        raise ConfigError(f"rules.json universe_filters.index {index!r} is not supported; "
                          f"use one of: {', '.join(INDEXES)}")
    fallback = uf.get("fallback_file", DEFAULT_FALLBACK)
    if not isinstance(fallback, str) or not fallback.strip() or Path(fallback).name != fallback:
        raise ConfigError("rules.json universe_filters.fallback_file must be a file name "
                          f"in config/, got {fallback!r}")
    gap = rules.get("daily_filters", {}).get("D3_min_gap_pct_from_prior_close", 3.0)
    return Settings(
        index=index,
        top_n=_number("rules.json universe_filters.top_n", uf.get("top_n", DEFAULT_TOP_N), True),
        fallback_file=paths.CONFIG_DIR / fallback,
        min_price=_number("rules.json universe_filters.min_price_usd", uf.get("min_price_usd", 3.0)),
        min_gap=_number("rules.json daily_filters.D3_min_gap_pct_from_prior_close", gap,
                        zero_ok=True))


def require(rules: dict) -> Settings:
    """settings() for a process that is starting: a ConfigError is logged as an ALERT
    and stops the process before it connects or trades."""
    try:
        return settings(rules)
    except ConfigError as e:
        logging.getLogger("paperbot").error("ALERT refusing to start: %s", e)
        raise SystemExit(f"Refusing to start: {e}") from None
