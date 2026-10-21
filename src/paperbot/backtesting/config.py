"""Backtest settings: backtesting/config/backtest.json merged over DEFAULTS.

Every execution and cost assumption the simulator makes lives here, so each
run can record exactly what it assumed (run_metadata.json). Strategy rules are
NOT here: they come from config/rules.json (read-only) or an experiment file.
"""
import copy
import json
import math
from pathlib import Path

from paperbot import paths

DEFAULTS: dict = {
    "currency": "USD",
    "starting_capital": 1_000_000.0,
    "provider": "ibkr",            # ibkr | yfinance | synthetic (tests/demos only)
    "fallback_provider": "yfinance",  # used when the default provider cannot connect
    "lookback_calendar_days": 120,  # default test period, ending yesterday (ET)
    "daily_warmup_calendar_days": 420,  # daily history before the period (SMA200)
    "symbols": [],                  # empty = rules.json universe_filters.fallback_file
    "max_watchlist": 20,            # the prefilter keeps the top 20 gappers each morning
    "benchmark_symbol": "SPY",
    "execution": {
        "half_spread_bps": 2.0,     # quote proxy: ask = close x (1 + h), bid = close x (1 - h)
        "slippage_bps": 2.0,        # extra adverse move on every market-like fill
        "participation_rate": 0.10,  # an entry fills at most this share of a bar's volume
        "buying_power_multiplier": 1.0,  # 1.0 = cash only (conservative); IBKR margin is higher
        "ambiguous_bar_policy": "stop_first",  # only supported value: the conservative one
        "decision_delay_seconds": 3,  # a scheduled run starts ~3 s after its 5-min slot
        "reset_drawdown_breaker_next_day": False,  # live needs a manual reset; False = faithful
    },
    "validation": {
        "in_sample": 0.6,
        "validation": 0.2,
        "out_of_sample": 0.2,
        "walk_forward_folds": 3,
        "min_trades_for_assessment": 30,
        "bootstrap_samples": 2000,
        "bootstrap_seed": 7,
    },
    "ibkr": {
        "client_id_offset": 3,       # config.json client_id + 3: never the bot's own id
        "request_pause_seconds": 0.5,
        "timeout_seconds": 60,
    },
    "retention": {
        "keep_last_runs_per_strategy": 50,  # applied only by an explicit `prune`
    },
    "schedule": {
        "enabled": False,
        "note": "Manual only. Run `paperbot-backtest all` yourself; no scheduled task is registered.",
    },
}

EXECUTION_POLICIES = ("stop_first",)


class BacktestConfigError(ValueError):
    pass


def _merge(base: dict, override: dict, where: str = "") -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if key.startswith("_"):
            continue  # comments
        if key not in base:
            raise BacktestConfigError(f"unknown backtest setting {where}{key!r}")
        if isinstance(base[key], dict):
            if not isinstance(value, dict):
                raise BacktestConfigError(f"backtest setting {where}{key} must be an object")
            out[key] = _merge(base[key], value, f"{where}{key}.")
        else:
            out[key] = value
    return out


NULLABLE = {"benchmark_symbol", "fallback_provider"}  # null / "" = none


def _check_types(base: dict, cfg: dict, where: str = ""):
    """Every setting must have its default's type: a bool is never accepted as a number
    (Python treats True as 1), whole-number settings must be integers and numbers must
    be finite."""
    for key, default in base.items():
        name, value = f"{where}{key}", cfg.get(key)
        if isinstance(default, dict):
            if not isinstance(value, dict):
                raise BacktestConfigError(f"backtest setting {name} must be an object")
            _check_types(default, value, f"{name}.")
        elif key in NULLABLE and value in (None, ""):
            continue
        elif isinstance(default, bool):
            if not isinstance(value, bool):
                raise BacktestConfigError(f"backtest setting {name} must be true or false, not {value!r}")
        elif isinstance(default, (int, float)):
            wanted = (int,) if isinstance(default, int) else (int, float)
            if isinstance(value, bool) or not isinstance(value, wanted):
                kind = "a whole number" if wanted == (int,) else "a number"
                raise BacktestConfigError(f"backtest setting {name} must be {kind}, not {value!r}")
            if not math.isfinite(value):
                raise BacktestConfigError(f"backtest setting {name} must be finite, not {value!r}")
        elif isinstance(default, str):
            if not isinstance(value, str):
                raise BacktestConfigError(f"backtest setting {name} must be text, not {value!r}")
        elif isinstance(default, list):
            if not isinstance(value, list) or not all(isinstance(x, str) and x.strip() for x in value):
                raise BacktestConfigError(f"backtest setting {name} must be a list of symbols")


def _at_least(cfg: dict, path: str, low, inclusive: bool = True):
    *parents, key = path.split(".")
    value = cfg
    for p in parents:
        value = value[p]
    value = value[key]
    if value < low or (not inclusive and value == low):
        raise BacktestConfigError(f"backtest setting {path} must be {'>=' if inclusive else '>'} {low}")


def validate(cfg: dict) -> dict:
    """Fail closed: a malformed setting is a BacktestConfigError naming the field, never a
    traceback, and never silently changes an assumption."""
    try:
        return _validate(cfg)
    except BacktestConfigError:
        raise
    except (TypeError, KeyError, AttributeError) as e:
        raise BacktestConfigError(f"invalid backtest settings: {e!r}") from None


def _validate(cfg: dict) -> dict:
    _check_types(DEFAULTS, cfg)
    ex, val = cfg["execution"], cfg["validation"]
    if cfg["currency"] != "USD":
        raise BacktestConfigError("only USD is supported (the strategy sizes in USD)")
    if not (isinstance(cfg["starting_capital"], (int, float)) and cfg["starting_capital"] > 0):
        raise BacktestConfigError("starting_capital must be > 0")
    if cfg["provider"] not in ("ibkr", "yfinance", "synthetic"):
        raise BacktestConfigError(f"unknown provider {cfg['provider']!r}")
    for key in ("half_spread_bps", "slippage_bps"):
        if not 0 <= ex[key] <= 500:
            raise BacktestConfigError(f"execution.{key} must be between 0 and 500")
    if not 0 < ex["participation_rate"] <= 1:
        raise BacktestConfigError("execution.participation_rate must be in (0, 1]")
    if not ex["buying_power_multiplier"] > 0:
        raise BacktestConfigError("execution.buying_power_multiplier must be > 0")
    if ex["ambiguous_bar_policy"] not in EXECUTION_POLICIES:
        raise BacktestConfigError(f"execution.ambiguous_bar_policy must be one of {EXECUTION_POLICIES}")
    total = val["in_sample"] + val["validation"] + val["out_of_sample"]
    if abs(total - 1) > 1e-6 or min(val["in_sample"], val["validation"], val["out_of_sample"]) < 0:
        raise BacktestConfigError("validation in_sample + validation + out_of_sample must sum to 1")
    if cfg["fallback_provider"] not in (None, "", "ibkr", "yfinance"):  # never synthetic prices
        raise BacktestConfigError(f"fallback_provider must be yfinance, ibkr or null, not "
                                  f"{cfg['fallback_provider']!r}")
    for path in ("lookback_calendar_days", "max_watchlist", "validation.walk_forward_folds",
                 "validation.min_trades_for_assessment", "validation.bootstrap_samples",
                 "retention.keep_last_runs_per_strategy",
                 "ibkr.client_id_offset"):  # offset 0 would be the bot's own client id
        _at_least(cfg, path, 1)
    for path in ("daily_warmup_calendar_days", "execution.decision_delay_seconds",
                 "ibkr.request_pause_seconds"):
        _at_least(cfg, path, 0)
    _at_least(cfg, "ibkr.timeout_seconds", 0, inclusive=False)
    return cfg


def load(path: Path | None = None, overrides: dict | None = None) -> dict:
    """DEFAULTS, then the backtest.json file (if present), then `overrides`."""
    path = path or paths.BACKTEST_CONFIG_FILE
    cfg = copy.deepcopy(DEFAULTS)
    if path.exists():
        try:
            cfg = _merge(cfg, json.loads(path.read_text(encoding="utf-8")))
        except ValueError as e:
            raise BacktestConfigError(f"cannot read {path}: {e}") from None
    if overrides:
        cfg = _merge(cfg, overrides)
    return validate(cfg)
