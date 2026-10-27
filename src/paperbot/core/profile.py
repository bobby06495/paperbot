"""Strategy profiles: one "strategies.<key>" section of rules.json, validated.

    {"strategies": {"trend_join_long": {...}, "macro_swing_etf": {...}},
     "default_strategy": "trend_join_long"}

load(key) checks the whole section before anything connects to IBKR and
returns a frozen StrategyProfile: every block (blocks/), the timeframe, hours,
universe, order tags and the typed risk limits (core/risk.load_limits). Any
mistake (typo in a block name, missing setting, wrong type, bad time) raises
ConfigError naming the exact place, e.g. "strategies.x.entry[1]: unknown entry
type "rsi_bellow"; valid: ...".

A section's keys:
  label, summary          menu text
  timeframe               bar size of every signal: 1m, 5m, 15m, 1h, or 1d (daily bars,
                          today's live price as the close; needs holding multi_day)
  holding                 "intraday" (flat every day; needs a force_close exit) or
                          "multi_day" (held overnight on the GTC stop until an exit
                          signal; no force close, force_close_et null)
  universe                {"source": "watchlist", "index": "S&P 500" (or "indexes": [...]),
                          "top_n": 100, "fallback_file": "universe.txt"} (market/universe.py)
                          or {"symbols": [...]} (optionally "indexes": [...], which adds
                          the ETF tracking each index) or {"file": "my_list.txt"} (config/)
  time_filter             earliest_entry_et, latest_entry_et, force_close_et ("HH:MM")
  entry, stop, manage, exit   block objects (see blocks/)
  risk                    limits (core/risk.py)
  order_ref               orderRef tag prefix; default "paperbot:<key>" ("paperbot" = legacy)
  modes                   ["continuous", "scheduled"] (default both)
  entry_reason            order note, formatted with the signal: {label} {stop} {gap} {rvol} {price}
  state_dir               under logs/; default "strategies/<key>" ("." = logs/ itself)
  max_entries_per_symbol_per_day   default 1 (re-entries after an exit, same day)

On an early-close day (market/market_calendar.py) for_session() returns a copy whose
times at or after the early close keep their distance to it: force_close_et 15:51
-> 12:51, latest_entry_et 15:15 -> 12:15, an exit's at_et 15:50 -> 12:50 on a 13:00
close. Regular days use the profile unchanged.
"""
import copy
import dataclasses
import datetime as dt
import json
import re
from pathlib import Path

from paperbot import blocks, paths
from paperbot.core.common import hhmm
from paperbot.core.market_data import TIMEFRAMES
from paperbot.core.risk import ConfigError, Limits, load_limits, strategy_section

SUPPORTED_TIMEFRAMES = ("1m", "5m", "15m", "1h", "1d")
HOLDINGS = ("intraday", "multi_day")
MODES = ("continuous", "scheduled")
SECTION_KEYS = {"label", "summary", "timeframe", "holding", "universe", "time_filter", "entry",
                "stop", "manage", "exit", "risk", "order_ref", "modes", "entry_reason",
                "state_dir", "max_entries_per_symbol_per_day"}
REQUIRED_KEYS = ("label", "timeframe", "holding", "universe", "time_filter", "entry", "stop",
                 "exit", "risk")


@dataclasses.dataclass(frozen=True)
class TimeFilter:
    earliest_entry_et: str
    latest_entry_et: str
    force_close_et: str | None
    entry_grace: dt.timedelta = dt.timedelta(minutes=1)  # unfilled entries die then

    def in_entry_window(self, now: dt.datetime) -> bool:
        """earliest <= now <= latest, to the minute: a run that starts at 15:30:04
        still scans (a process takes seconds to start)."""
        minute = now.time().replace(second=0, microsecond=0)
        return hhmm(self.earliest_entry_et) <= minute <= hhmm(self.latest_entry_et)

    def entry_cutoff(self, now: dt.datetime) -> dt.datetime:
        """When unfilled entries die: the end of the latest_entry_et minute."""
        latest = hhmm(self.latest_entry_et)
        return now.replace(hour=latest.hour, minute=latest.minute, second=0,
                           microsecond=0) + self.entry_grace

    def force_close_time(self) -> dt.time | None:
        return hhmm(self.force_close_et) if self.force_close_et else None

    def window(self) -> str:
        return f"{self.earliest_entry_et}-{self.latest_entry_et} ET"


@dataclasses.dataclass(frozen=True)
class StrategyProfile:
    key: str
    label: str
    summary: str
    timeframe: str
    holding: str
    universe: dict
    time_filter: TimeFilter
    entry: tuple
    stop: blocks.Block
    manage: tuple
    exit: tuple
    limits: Limits
    order_ref: str
    modes: tuple
    entry_reason: str
    state_dir: Path
    raw: dict = dataclasses.field(repr=False, compare=False)
    max_entries_per_symbol_per_day: int = 1

    @property
    def bar(self) -> dt.timedelta:
        return TIMEFRAMES[self.timeframe][1]

    @property
    def refs(self) -> tuple[str, str, str]:
        """orderRef tags (entry, stop, exit) of this strategy's orders."""
        return f"{self.order_ref}:entry", f"{self.order_ref}:stop", f"{self.order_ref}:exit"

    @property
    def state_file(self) -> Path:
        return self.state_dir / "strategy_state.json"

    @property
    def safety_log(self) -> Path:
        return self.state_dir / "safety_log.jsonl"

    def block(self, kind: str, type_name: str):
        """The first manage/exit block of a type, or None."""
        return next((b for b in getattr(self, kind) if b.type == type_name), None)

    @property
    def scan_every(self) -> dt.timedelta:
        """How often the universe is scanned: once per bar, at least every 5 minutes
        (a 1d strategy re-checks its symbols every 5 minutes inside its window)."""
        return min(self.bar, dt.timedelta(minutes=5))

    @property
    def signal_exits(self) -> tuple:
        return tuple(b for b in self.exit if getattr(b, "SIGNAL", False))

    def menu_line(self) -> str:
        tf = self.time_filter
        close = f"flat {tf.force_close_et}" if tf.force_close_et else "holds overnight"
        modes = "/".join(self.modes)
        return (f"{self.timeframe} | entries {tf.earliest_entry_et}-{tf.latest_entry_et} | "
                f"{close} | {modes}")

    def describe(self) -> list[str]:
        """Every setting, for the start-up confirmation."""
        lim = self.limits
        return [
            f"{self.label} ({self.key}): {self.summary}",
            f"  timeframe {self.timeframe}, {self.holding}; {self.menu_line()}",
            f"  universe: {_universe_text(self.universe)}",
            "  entry: " + " > ".join(b.label or b.type for b in self.entry),
            f"  stop: {self.stop.describe()}",
            "  manage: " + (", ".join(b.describe() for b in self.manage) or "none"),
            "  exit: " + ", ".join(b.describe() for b in self.exit),
            f"  risk: {lim.max_risk_per_trade_pct}%/trade, {lim.max_position_size_pct_of_portfolio}% "
            f"position, <={lim.max_quantity_per_order} sh / ${lim.max_order_value:,.0f} per order, "
            f"{lim.max_concurrent_positions} positions, daily loss ${lim.max_daily_loss:,.0f}",
        ]


def _universe_text(universe: dict) -> str:
    if "symbols" in universe:
        return ", ".join(universe["symbols"])
    if "file" in universe:
        return f"config/{universe['file']}"
    from paperbot.market import universe as watchlist_universe
    indexes = watchlist_universe.label(watchlist_universe.indexes_of(universe))
    return (f"today's {indexes} watchlist "
            f"(fallback config/{universe.get('fallback_file', 'universe.txt')})")


def _shifted(text: str | None, day) -> str | None:
    from paperbot.market import market_calendar
    if text is None:
        return None
    return market_calendar.shift(hhmm(text), day).strftime("%H:%M")


def for_session(profile: StrategyProfile, day) -> StrategyProfile:
    """The profile for one market session (market_calendar.Session): unchanged on a
    regular day; on an early close every time at or after the close moves earlier by
    the hours the session lost. Entry, exit and risk rules are otherwise untouched."""
    if getattr(day, "kind", None) != "early_close":
        return profile
    tf = profile.time_filter
    time_filter = dataclasses.replace(
        tf, earliest_entry_et=_shifted(tf.earliest_entry_et, day),
        latest_entry_et=_shifted(tf.latest_entry_et, day),
        force_close_et=_shifted(tf.force_close_et, day))
    exits = []
    for block in profile.exit:
        if getattr(block, "at_et", None):
            block = copy.copy(block)
            block.at_et = _shifted(block.at_et, day)
        exits.append(block)
    return dataclasses.replace(profile, time_filter=time_filter, exit=tuple(exits))


def read_rules(rules_path: Path | None = None) -> dict:
    rules_path = rules_path or paths.RULES_FILE
    try:
        rules = json.loads(rules_path.read_text())
    except (OSError, ValueError) as e:
        raise ConfigError(f"cannot read {rules_path.name}: {e}") from None
    strategies = rules.get("strategies")
    if not isinstance(strategies, dict) or not strategies:
        raise ConfigError(f'{rules_path.name} needs a "strategies" object with at least one strategy')
    return rules


def strategy_keys(rules_path: Path | None = None) -> list[str]:
    """Strategy keys in rules.json order (the menu order)."""
    return list(read_rules(rules_path)["strategies"])


def default_strategy(rules_path: Path | None = None) -> str | None:
    return read_rules(rules_path).get("default_strategy")


def _time(where: str, name: str, value, optional=False) -> str | None:
    if value is None and optional:
        return None
    if not (isinstance(value, str) and re.fullmatch(r"\d{2}:\d{2}", value)):
        raise ConfigError(f"{where}.{name} must be \"HH:MM\" (ET), got {value!r}")
    try:
        hhmm(value)
    except ValueError:
        raise ConfigError(f"{where}.{name} is not a valid time: {value!r}") from None
    return value


def _universe(where: str, value) -> dict:
    if not isinstance(value, dict):
        raise ConfigError(f"{where} must be an object")
    if "symbols" in value:
        symbols = value["symbols"]
        if not (isinstance(symbols, list) and symbols
                and all(isinstance(s, str) and s.strip() for s in symbols)):
            raise ConfigError(f"{where}.symbols must be a non-empty list of tickers")
        symbols = [s.strip().upper() for s in symbols]
        if "indexes" in value:  # the ETF tracking each index, never its members
            from paperbot.market import universe as watchlist_universe
            symbols = watchlist_universe.etf_symbols(where, symbols, value["indexes"])
        return {**value, "symbols": symbols}
    if "file" in value:
        if not (isinstance(value["file"], str) and (paths.CONFIG_DIR / value["file"]).exists()):
            raise ConfigError(f"{where}.file: config/{value.get('file')} not found")
        return dict(value)
    if value.get("source") == "watchlist":
        from paperbot.market import universe as watchlist_universe  # index, top_n, fallback
        return watchlist_universe.validate(where, value)
    raise ConfigError(f'{where} needs "symbols": [...], "file": "...", or "source": "watchlist"')


def _blocks(kind: str, where: str, value, single=False):
    if single:
        return blocks.build(kind, value, where)
    if not isinstance(value, list):
        raise ConfigError(f"{where} must be a list of {kind} blocks")
    return tuple(blocks.build(kind, spec, f"{where}[{i}]") for i, spec in enumerate(value))


def load(key: str, rules_path: Path | None = None, config: dict | None = None) -> StrategyProfile:
    """The validated profile of strategy `key`. ConfigError on any problem."""
    rules_path = rules_path or paths.RULES_FILE
    rules = read_rules(rules_path)
    section = strategy_section(rules, key, rules_path.name)
    where = f"{rules_path.name} strategies.{key}"

    unknown = sorted(set(section) - SECTION_KEYS)
    if unknown:
        raise ConfigError(f"{where} has unknown keys {unknown}; valid: {', '.join(sorted(SECTION_KEYS))}")
    missing = [k for k in REQUIRED_KEYS if k not in section]
    if missing:
        raise ConfigError(f"{where} is missing {missing}")

    timeframe = section["timeframe"]
    if timeframe not in SUPPORTED_TIMEFRAMES:
        raise ConfigError(f"{where}.timeframe must be one of {', '.join(SUPPORTED_TIMEFRAMES)}, "
                          f"got {timeframe!r}")
    holding = section["holding"]
    if holding not in HOLDINGS:
        raise ConfigError(f"{where}.holding must be one of {', '.join(HOLDINGS)}, got {holding!r}")

    tf_raw = section["time_filter"]
    if not isinstance(tf_raw, dict):
        raise ConfigError(f"{where}.time_filter must be an object")
    tfw = f"{where}.time_filter"
    time_filter = TimeFilter(_time(tfw, "earliest_entry_et", tf_raw.get("earliest_entry_et")),
                             _time(tfw, "latest_entry_et", tf_raw.get("latest_entry_et")),
                             _time(tfw, "force_close_et", tf_raw.get("force_close_et"), optional=True))
    if hhmm(time_filter.earliest_entry_et) > hhmm(time_filter.latest_entry_et):
        raise ConfigError(f"{tfw}: earliest_entry_et is after latest_entry_et")

    entry = _blocks("entry", f"{where}.entry", section["entry"])
    if not entry:
        raise ConfigError(f"{where}.entry needs at least one entry block")
    stop = _blocks("stop", f"{where}.stop", section["stop"], single=True)
    manage = _blocks("manage", f"{where}.manage", section.get("manage", []))
    exits = _blocks("exit", f"{where}.exit", section["exit"])
    if not exits:
        raise ConfigError(f"{where}.exit needs at least one exit block")
    has_force_close = any(b.type == "force_close" for b in exits)
    if holding == "intraday" and not (has_force_close and time_filter.force_close_et):
        raise ConfigError(f"{where}: an intraday strategy needs a force_close exit and "
                          "time_filter.force_close_et")
    if holding == "multi_day" and (has_force_close or time_filter.force_close_et):
        raise ConfigError(f"{where}: a multi_day strategy holds overnight: no force_close exit "
                          "and time_filter.force_close_et null")
    if timeframe == "1d" and holding != "multi_day":
        raise ConfigError(f"{where}: the 1d timeframe needs holding multi_day")
    if timeframe == "1d":
        for kind, found in (("entry", entry), ("stop", (stop,)), ("manage", manage), ("exit", exits)):
            for i, b in enumerate(found):
                if b.NEEDS == "intraday":
                    at = f"{where}.{kind}" + (f"[{i}]" if kind != "stop" else "")
                    raise ConfigError(f"{at}: {b.type} needs intraday bars; not available on 1d")
    if time_filter.force_close_et and hhmm(time_filter.force_close_et) <= hhmm(time_filter.latest_entry_et):
        raise ConfigError(f"{tfw}: force_close_et must be after latest_entry_et")

    order_ref = section.get("order_ref", f"paperbot:{key}")
    if not (order_ref == "paperbot" or (isinstance(order_ref, str) and order_ref.startswith("paperbot:"))):
        raise ConfigError(f'{where}.order_ref must be "paperbot" or start with "paperbot:", got {order_ref!r}')
    modes = section.get("modes", list(MODES))
    if not (isinstance(modes, list) and modes and set(modes) <= set(MODES)):
        raise ConfigError(f"{where}.modes must be a non-empty list of {', '.join(MODES)}")
    reason = section.get("entry_reason", "{label}: stop {stop:.2f}")
    if not isinstance(reason, str):
        raise ConfigError(f"{where}.entry_reason must be text")
    state_dir = section.get("state_dir", f"strategies/{key}")
    if not isinstance(state_dir, str) or ".." in Path(state_dir).parts or Path(state_dir).is_absolute():
        raise ConfigError(f"{where}.state_dir must be a folder inside logs/, got {state_dir!r}")
    label = section["label"]
    if not (isinstance(label, str) and label.strip()):
        raise ConfigError(f"{where}.label must be text")
    max_entries = section.get("max_entries_per_symbol_per_day", 1)
    if not (isinstance(max_entries, int) and not isinstance(max_entries, bool)
            and 1 <= max_entries <= 20):
        raise ConfigError(f"{where}.max_entries_per_symbol_per_day must be a whole number "
                          f"1-20, got {max_entries!r}")

    if config is None:
        config = json.loads(paths.CONFIG_FILE.read_text())
    limits = load_limits(config, rules_path, key)
    return StrategyProfile(
        key=key, label=label, summary=str(section.get("summary", "")), timeframe=timeframe,
        holding=holding, universe=_universe(f"{where}.universe", section["universe"]),
        time_filter=time_filter, entry=entry, stop=stop, manage=manage, exit=exits,
        limits=limits, order_ref=order_ref, modes=tuple(modes), entry_reason=reason,
        state_dir=(paths.LOGS_DIR / state_dir).resolve(), raw=section,
        max_entries_per_symbol_per_day=max_entries)


def load_all(rules_path: Path | None = None, config: dict | None = None) -> dict[str, StrategyProfile]:
    """Every strategy in rules.json, validated, in menu order. Also checks that no two
    strategies share an order_ref or a state folder (they would manage each other's orders)."""
    profiles = {key: load(key, rules_path, config) for key in strategy_keys(rules_path)}
    for attr in ("order_ref", "state_dir"):
        seen: dict = {}
        for key, p in profiles.items():
            value = getattr(p, attr)
            if value in seen:
                raise ConfigError(f"strategies {seen[value]} and {key} share the same {attr} {value}")
            seen[value] = key
    return profiles


def require(key: str, rules_path: Path | None = None, config: dict | None = None) -> StrategyProfile:
    """load() for a process that is starting: a ConfigError is logged as an ALERT and
    stops the process before it connects or trades."""
    from paperbot.core.common import log
    try:
        return load(key, rules_path, config)
    except ConfigError as e:
        log.error("ALERT refusing to start: %s", e)
        raise SystemExit(f"Refusing to start: {e}") from None
