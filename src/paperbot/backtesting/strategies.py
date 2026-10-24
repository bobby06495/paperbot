"""The strategies the backtester can run: every "strategies.<key>" section of rules.json.

Paperbot runs every strategy through one engine (strategies/rule_strategy.py with
the blocks in blocks/), so every profile in rules.json is backtestable with no
extra code: the simulator drives that same engine (backtesting/engine.py).
"""
import json
from dataclasses import dataclass
from pathlib import Path

from paperbot import paths
from paperbot.core import profile as profiles

LIVE_MODULE = "paperbot.strategies.rule_strategy + paperbot.blocks + paperbot.core.orders"


@dataclass(frozen=True)
class StrategySpec:
    strategy_id: str
    name: str
    description: str
    timeframe: str
    holding: str
    rules_file: Path
    live_module: str = LIVE_MODULE

    @property
    def tape(self) -> str:
        """Intraday bar size the simulation runs on (a 1d strategy is priced from 5m bars)."""
        return self.timeframe if self.timeframe != "1d" else "5m"

    def rules(self) -> dict:
        """The whole production rules.json, read-only."""
        return json.loads(self.rules_file.read_text(encoding="utf-8"))

    def section(self) -> dict:
        return self.rules()["strategies"][self.strategy_id]

    def load_profile(self, rules_file: Path | None = None, config: dict | None = None):
        return profiles.load(self.strategy_id, rules_file or self.rules_file, config)


def discover(rules_file: Path | None = None) -> dict[str, StrategySpec]:
    rules_file = rules_file or paths.RULES_FILE
    rules = json.loads(rules_file.read_text(encoding="utf-8"))
    out = {}
    for key, section in (rules.get("strategies") or {}).items():
        out[key] = StrategySpec(key, section.get("label", key), section.get("summary", ""),
                                section.get("timeframe", ""), section.get("holding", ""), rules_file)
    return out


def get(strategy_id: str, rules_file: Path | None = None) -> StrategySpec:
    found = discover(rules_file)
    if strategy_id not in found:
        raise KeyError(f"unknown strategy {strategy_id!r}; rules.json has: {', '.join(found)}")
    return found[strategy_id]
