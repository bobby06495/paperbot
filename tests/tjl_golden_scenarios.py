"""Trend Join Long behaviour scenarios, shared by the golden-record generator and
test_tjl_golden.py.

Every scenario drives the public strategy API (evaluate / decide / sizing /
check_symbol) against the fake IB objects from test_strategy.py and returns a
JSON-able result. The recorded results in fixtures/tjl_golden.json were produced
by the code BEFORE the strategy-engine refactor (2026-10-20); the refactored
engine must reproduce them exactly, which proves Trend Join Long behaves the same.

The API used is resolved by api() below, so the scenarios themselves never change.
"""
import dataclasses
import datetime as dt
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

import test_strategy as ts
from paperbot.core.risk import REF_ENTRY

FIXTURES = Path(__file__).with_name("fixtures")
GOLDEN = FIXTURES / "tjl_golden.json"
ET = ts.ET
UTC = ts.UTC
MON_11 = dt.datetime(2026, 10, 19, 11, 0, tzinfo=ET)


def api():
    """(strategy module, evaluate(ib, symbol, now), size(signal, net_liq, bp, ask, good_till)).
    The refactored engine (2026-10-20): strategies/rule_strategy.py running the
    trend_join_long profile from rules.json."""
    from paperbot.strategies import rule_strategy as strategy

    def evaluate(ib, symbol, now):
        return strategy.evaluate(ib, symbol, strategy.load_profile(), now)

    def size(signal, net_liq, bp, ask, good_till=None):
        return strategy._size_entry("TEST", signal, strategy.load_profile(), net_liq, bp, ask,
                                    good_till)
    return strategy, evaluate, size


def plain(value):
    """JSON-able form of requests, dataclasses and datetimes."""
    if dataclasses.is_dataclass(value):
        return {"kind": type(value).__name__,
                **{k: plain(v) for k, v in dataclasses.asdict(value).items()}}
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    return value


# ---------------------------------------------------------------- evaluate() scenarios

def _market(**kw):
    return ts.FakeMarket(**kw)


def _mutate(fn):
    def build():
        m = _market()
        fn(m)
        return m
    return build


def _no_contract(m):
    m.qualifyContracts = lambda contract: setattr(contract, "conId", 0)


def _set_daily_close(m, value):
    for b in m.daily:
        b.close, b.high = value, value + 1


def _falling_daily(m):
    for i, b in enumerate(m.daily):
        b.close = b.high = 200 - i * 0.5


def _short_history(m):
    m.daily = m.daily[-150:]


def _cheap_intraday(m):
    for b in m.today:
        b.close = b.high = b.low = b.open = 2.0


def _high_prior_day(m):
    m.daily[-1].high = 500.0


def _no_premarket(m):
    m.today = [b for b in m.today if b.date.astimezone(ET).hour >= 9 and
               not (b.date.astimezone(ET).hour == 9 and b.date.astimezone(ET).minute < 30)]


def _high_premarket(m):
    for b in m.today:
        if b.date.astimezone(ET).hour < 9:
            b.high = 999.0


def _earlier_high(m):
    rth = [b for b in m.today if b.date.astimezone(ET).hour >= 10]
    rth[0].high = 999.0


def _low_rvol(m):
    for b in m.history:
        b.volume = 1_000_000


def _thin_rvol_history(m):
    m.history = m.history[:78 * 5]


EVALUATE = {
    "pass": (_market, MON_11),
    "contract_missing": (_mutate(_no_contract), MON_11),
    "no_daily_history": (_mutate(lambda m: setattr(m, "daily", [])), MON_11),
    "prior_close_below_min": (_mutate(lambda m: _set_daily_close(m, 2.0)), MON_11),
    "d2_below_sma": (_mutate(_falling_daily), MON_11),
    "d2_short_history": (_mutate(_short_history), MON_11),
    "too_few_bars": (_market, MON_11.replace(hour=9, minute=39)),
    "price_below_min": (_mutate(_cheap_intraday), MON_11),
    "d3_gap_small": (lambda: _market(gap_open=92.0), MON_11),
    "d1_below_prior_high": (_mutate(_high_prior_day), MON_11),
    "i1_no_premarket": (_mutate(_no_premarket), MON_11),
    "i1_below_premarket_high": (_mutate(_high_premarket), MON_11),
    "i2_not_new_high": (_mutate(_earlier_high), MON_11),
    "i3_rvol_low": (_mutate(_low_rvol), MON_11),
    "i3_rvol_short_history": (_mutate(_thin_rvol_history), MON_11),
    "pass_early_session": (_market, MON_11.replace(hour=10, minute=10)),
}


# ---------------------------------------------------------------- decide() scenarios

def _own(state_path, **extra):
    pos = {"initial_stop": 95.0, "stop": 95.0, "breakeven": False, "partial_done": False}
    ts.write_json_atomic(state_path, {"positions": {"AAPL": {**pos, **extra}}, "entered": {}})


def _at(hour, minute, second=0):
    return dt.datetime(2026, 10, 19, hour, minute, second, tzinfo=ET)


DECIDE = {  # name: (own-state extras or None, ib builder, now, universe)
    "restore_missing_stop": ({}, lambda now: ts.FakeIB(9, [], 101.0, now), _at(11, 0), []),
    "sync_stop_size": ({}, lambda now: ts.FakeIB(6, [ts.trade(ts.REF_STOP, 5, 9, 95.0)], 101.0, now),
                       _at(11, 0), []),
    "partial_profit": ({}, lambda now: ts.FakeIB(9, [ts.trade(ts.REF_STOP, 5, 9, 95.0)], 104.0, now),
                       _at(11, 0), []),
    "breakeven_and_trail": ({}, lambda now: _rising_ib(now), _at(12, 0), []),
    "force_close_legacy_stop": ({}, lambda now: ts.FakeIB(9, [ts.trade(ts.REF_STOP, 5, 9, 95.0)], 101.0, now),
                                _at(15, 52), []),
    "force_close_oca_stop": ({}, lambda now: ts.FakeIB(9, [ts.trade(ts.REF_STOP, 5, 9, 95.0, oca="g1")],
                                                       101.0, now, quote=(100.5, 100.6)),
                             _at(15, 51, 2), []),
    "force_close_stale_exit": ({}, lambda now: ts.FakeIB(9, [ts.trade(ts.REF_EXIT, 7, 9, age_s=120)], 101.0, now),
                               _at(15, 55), []),
    "force_close_fresh_exit": ({}, lambda now: ts.FakeIB(9, [ts.trade(ts.REF_EXIT, 7, 9, age_s=10)], 101.0, now),
                               _at(15, 52), []),
    "after_close_protect": ({}, lambda now: ts.FakeIB(9, [], 101.0, now), _at(17, 0), []),
    "after_close_live_exit": ({}, lambda now: ts.FakeIB(9, [ts.trade(ts.REF_EXIT, 7, 9, age_s=300)], 101.0, now),
                              _at(16, 0, 5), []),
    "entry_sweep": (None, lambda now: ts.FakeIB(0, [ts.trade(REF_ENTRY, 3, 1)], 101.0, now), _at(15, 31), []),
    "entry_signal": (None, lambda now: ts.FakeMarket(now=now), MON_11, ["TEST"]),
    "entry_signal_partial_window": (None, lambda now: ts.FakeMarket(now=now), _at(15, 30, 40), ["TEST"]),
    "no_signal_outside_window": (None, lambda now: ts.FakeMarket(now=now), _at(15, 31, 5), ["TEST"]),
}


def _rising_ib(now):
    """Position up > 1R with a swing low: breakeven, then the trail."""
    ib = ts.FakeIB(9, [ts.trade(ts.REF_STOP, 5, 9, 95.0)], 106.0, now)
    start = now.replace(hour=9, minute=30, second=0, microsecond=0)
    closes = [101, 103, 102, 101.5, 102.5, 104, 105, 106]
    lows = [100, 102, 101, 100.5, 101.8, 103, 104, 105]
    ib._bars = [ts.bar(start + i * dt.timedelta(minutes=5), c, low=l)
                for i, (c, l) in enumerate(zip(closes, lows))]
    ib._bars[-1].close = 106.0
    return ib


# ---------------------------------------------------------------- sizing scenarios

SIZING = {  # name: (signal, net_liq, buying_power, ask, good_till)
    "typical": ({"price": 100.0, "stop": 98.0, "gap": 4.0, "rvol": 3.0}, 781_120.0, 4e6, 100.02, None),
    "high_price": ({"price": 1358.5, "stop": 1331.33, "gap": 4.6, "rvol": 2.4}, 781_120.0, 4e6, 1358.6, None),
    "buying_power_binds": ({"price": 100.0, "stop": 98.0, "gap": 4.0, "rvol": 3.0}, 781_120.0, 20_000.0, 100.0, None),
    "commission_after_bp_cap": ({"price": 100.0, "stop": 98.0, "gap": 4.0, "rvol": 3.0}, 781_120.0, 5_010.0, 100.0, None),
    "commission_blocks": ({"price": 50.0, "stop": 49.9, "gap": 4.0, "rvol": 3.0}, 781_120.0, 4e6, 50.0, None),
    "stop_above_limit": ({"price": 100.0, "stop": 101.0, "gap": 4.0, "rvol": 3.0}, 781_120.0, 4e6, 100.0, None),
    "gtd": ({"price": 100.0, "stop": 98.0, "gap": 4.0, "rvol": 3.0}, 781_120.0, 4e6, 100.0,
            dt.datetime(2026, 10, 19, 15, 31, tzinfo=ET)),
}


# ---------------------------------------------------------------- runner

def run_all(rules_file: Path, config_file: Path) -> dict:
    """Every scenario's result, with the given (pinned) rules and config files."""
    from paperbot.core import common, orders
    from paperbot.core import market_data as md
    strategy, evaluate, size = api()
    out = {"evaluate": {}, "decide": {}, "sizing": {}, "check_symbol": {}}
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        patches = [mock.patch.object(strategy, "STATE", tmp / "state.json"),
                   mock.patch.object(strategy, "SAFETY_LOG", tmp / "safety.jsonl"),
                   mock.patch.object(strategy, "RULES_FILE", rules_file),
                   mock.patch.object(strategy, "CONFIG_FILE", config_file),
                   mock.patch.object(strategy, "ACTIVE", "trend_join_long"),
                   mock.patch.object(strategy, "_profile", None)]
        for p in patches:
            p.start()
        try:
            def fresh():
                orders._orphans.clear()
                md.clear_caches()
                common._warned.clear()
                strategy._scan_bar, strategy._scan_pos, strategy._scan_list = None, 0, []
                (tmp / "state.json").unlink(missing_ok=True)

            for name, (build, now) in EVALUATE.items():
                fresh()
                result = evaluate(build(), "TEST", now)
                out["evaluate"][name] = plain({k: v for k, v in result.items() if k != "time"})

            for name, (own, build, now, universe) in DECIDE.items():
                fresh()
                if own is not None:
                    _own(tmp / "state.json", **own)
                with mock.patch.object(strategy, "_universe", lambda profile, today, u=universe: list(u)), \
                        mock.patch.object(strategy, "_now", lambda n=now: n):
                    requests = strategy.decide(build(now), "DU1")
                state = json.loads((tmp / "state.json").read_text())
                out["decide"][name] = {"requests": plain(requests), "positions": state["positions"]}

            for name, (signal, nlv, bp, ask, gtd) in SIZING.items():
                fresh()
                out["sizing"][name] = plain(size(signal, nlv, bp, ask, gtd))

            for name, (shares, symbol, now) in {
                    "pass": (0, "test", MON_11), "held": (5, "aapl", MON_11),
                    "outside_window": (0, "test", MON_11.replace(hour=9, minute=50)),
                    "window_last_minute": (0, "test", MON_11.replace(hour=15, minute=30, second=59))}.items():
                fresh()
                with mock.patch.object(strategy, "_now", lambda n=now: n):
                    result = strategy.check_symbol(ts.FakeMarket(shares=shares), "DU1", symbol)
                out["check_symbol"][name] = plain({k: v for k, v in result.items() if k != "time"})
        finally:
            for p in reversed(patches):
                p.stop()
    return out


if __name__ == "__main__":  # regenerate the golden record (only with the PRE-refactor code)
    import logging
    import sys
    logging.getLogger("paperbot").addHandler(logging.NullHandler())
    rules, config = Path(sys.argv[1]), Path(sys.argv[2])
    GOLDEN.write_text(json.dumps(run_all(rules, config), indent=1, sort_keys=True) + "\n")
    print(f"wrote {GOLDEN}")
