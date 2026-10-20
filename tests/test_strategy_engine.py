"""Offline tests for the strategy engine pieces: blocks, profiles, selector, sizing.

Covers: block validation messages, the rules.json profile loader (every error
names its place), adding a strategy with rules.json only, the start-up menu and
switch guard, directional tick rounding, the indicators and orderRef roles.
Run: python -m unittest discover -s tests -p test_strategy_engine.py -v
"""
import os

os.environ["PAPERBOT_NOTIFY"] = "off"  # tests must never send real notifications

import copy
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

from paperbot import blocks, paths
from paperbot.core import profile as profiles
from paperbot.core import selector, sizing
from paperbot.core.common import ET
from paperbot.core.risk import ConfigError, ref_role
from paperbot.indicators import relative_volume, sma, swing_low
from paperbot.runtime.runstate import write_json_atomic

LIVE_RULES = json.loads(paths.RULES_FILE.read_text())
CONFIG = {"limits": {"allowed_symbols": ["*"], "limit_orders_only": True}}
TJL = LIVE_RULES["strategies"]["trend_join_long"]


def etf_breakout():
    """A second strategy assembled only from existing blocks (no new Python)."""
    section = copy.deepcopy(TJL)
    section.update(label="ETF Breakout", summary="Sector ETFs above yesterday's high",
                   universe={"symbols": ["xlk", "XLF"]}, entry_reason="{label}: stop {stop:.2f}")
    for key in ("order_ref", "state_dir"):
        section.pop(key)
    section["entry"] = [{"type": "above_prior_day_high"}, {"type": "min_rvol", "min": 1.5}]
    section["manage"] = [{"type": "breakeven", "at_R": 1.0}]
    return section


class RulesFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "rules.json"

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, strategies, **top):
        self.path.write_text(json.dumps({"strategies": strategies, **top}))
        return self.path

    def load(self, section, key="s"):
        return profiles.load(key, self.write({key: section}), CONFIG)

    def fails(self, section, text):
        with self.assertRaises(ConfigError) as caught:
            self.load(section)
        self.assertIn(text, str(caught.exception))


class ProfileTest(RulesFileTest):
    def test_live_trend_join_long_profile(self):
        p = profiles.load("trend_join_long", paths.RULES_FILE, CONFIG)
        self.assertEqual(p.refs, ("paperbot:entry", "paperbot:stop", "paperbot:exit"))
        self.assertEqual(p.state_file, (paths.LOGS_DIR / "strategy_state.json").resolve())
        self.assertEqual([b.label for b in p.entry], ["", "D2", "", "D3", "D1", "I1", "I2", "I3"])
        self.assertEqual(p.time_filter.force_close_time(), dt.time(15, 51))
        self.assertEqual(p.bar, dt.timedelta(minutes=5))

    def test_new_strategy_from_rules_json_only(self):
        rules = self.write({"trend_join_long": TJL, "etf_breakout": etf_breakout()})
        loaded = profiles.load_all(rules, CONFIG)
        self.assertEqual(list(loaded), ["trend_join_long", "etf_breakout"])  # menu order
        p = loaded["etf_breakout"]
        self.assertEqual(p.universe["symbols"], ["XLK", "XLF"])
        self.assertEqual(p.refs[1], "paperbot:etf_breakout:stop")  # its own orders
        self.assertEqual(p.state_file.parent.name, "etf_breakout")  # its own state
        self.assertIsNone(p.block("manage", "partial_profit"))

    def test_typo_in_block_type_names_place_and_valid_types(self):
        section = copy.deepcopy(TJL)
        section["entry"][1]["type"] = "prior_close_above_smaa"
        self.fails(section, 'strategies.s.entry[1]: unknown entry type "prior_close_above_smaa"; valid:')

    def test_bad_block_setting(self):
        section = copy.deepcopy(TJL)
        section["entry"][7]["min"] = "2"
        self.fails(section, "strategies.s.entry[7]: min must be a number > 0")
        section["entry"][7] = {"type": "min_rvol"}
        self.fails(section, "min_rvol is missing the setting 'min'")
        section["entry"][7] = {"type": "min_rvol", "min": 2.0, "lookbak_days": 14}
        self.fails(section, "unknown settings ['lookbak_days']")

    def test_section_level_checks(self):
        for change, text in (
                ({"timeframe": "2m"}, "timeframe must be one of"),
                ({"holding": "forever"}, "holding must be one of"),
                ({"color": "red"}, "unknown keys ['color']"),
                ({"exit": []}, "exit needs at least one exit block"),
                ({"exit": [{"type": "time_stop", "minutes": 30}]},
                 "an intraday strategy needs a force_close exit"),
                ({"entry": []}, "needs at least one entry block"),
                ({"order_ref": "mybot"}, 'order_ref must be "paperbot"'),
                ({"modes": ["sometimes"]}, "modes must be a non-empty list"),
                ({"max_entries_per_symbol_per_day": 0}, "must be a whole number 1-20"),
                ({"state_dir": "../x"}, "state_dir must be a folder inside logs/"),
                ({"universe": {"symbols": []}}, "universe.symbols must be a non-empty list"),
                ({"time_filter": {**TJL["time_filter"], "latest_entry_et": "3:30pm"}}, "must be \"HH:MM\""),
                ({"time_filter": {**TJL["time_filter"], "force_close_et": "15:00"}},
                 "force_close_et must be after latest_entry_et")):
            with self.subTest(change=change):
                self.fails({**copy.deepcopy(TJL), **change}, text)
        section = copy.deepcopy(TJL)
        del section["risk"]
        self.fails(section, "is missing ['risk']")

    def test_partial_fraction_below_one(self):
        section = copy.deepcopy(TJL)
        section["manage"][0]["fraction"] = 1.0
        self.fails(section, "fraction must be below 1")

    def test_two_strategies_cannot_share_orders_or_state(self):
        twin = copy.deepcopy(TJL)
        with self.assertRaises(ConfigError) as caught:
            profiles.load_all(self.write({"a": TJL, "b": twin}), CONFIG)
        self.assertIn("share the same order_ref", str(caught.exception))

    def test_entry_window_and_cutoff(self):
        tf = profiles.TimeFilter("10:05", "15:30", "15:51")
        day = dt.datetime(2026, 10, 19, tzinfo=ET)
        self.assertTrue(tf.in_entry_window(day.replace(hour=15, minute=30, second=59)))
        self.assertFalse(tf.in_entry_window(day.replace(hour=15, minute=31)))
        self.assertEqual(tf.entry_cutoff(day.replace(hour=11)), day.replace(hour=15, minute=31))


class BlockRegistryTest(unittest.TestCase):
    def test_every_block_type_is_registered(self):
        self.assertIn("min_rvol", blocks.names("entry"))
        self.assertIn("low_of_day_minus_pct", blocks.names("stop"))
        self.assertEqual(blocks.names("exit"),
                         ["close_above_sma", "force_close", "target_vwap", "time_stop"])

    def test_defaults_and_label(self):
        block = blocks.build("entry", {"type": "prior_close_above_sma", "label": "D2"}, "x")
        self.assertEqual((block.period, block.prefix), (200, "D2 "))
        self.assertEqual(blocks.build("manage", {"type": "trail_swing_low"}, "x").describe(),
                         "trail_swing_low(left=2, right=2)")

    def test_booleans_are_not_numbers(self):
        with self.assertRaises(ConfigError):
            blocks.build("entry", {"type": "min_gap_pct", "min_pct": True}, "x")


class SelectorTest(RulesFileTest):
    def setUp(self):
        super().setUp()
        self.active = Path(self.tmp.name) / "active_strategy.json"
        self.patch = mock.patch.object(selector, "ACTIVE_FILE", self.active)
        self.patch.start()
        tjl = {**copy.deepcopy(TJL), "state_dir": "tjl-test"}
        self.rules = self.write({"trend_join_long": tjl, "etf_breakout": etf_breakout()},
                                default_strategy="trend_join_long")
        self.logs = mock.patch.object(paths, "LOGS_DIR", Path(self.tmp.name))
        self.logs.start()
        self.config = mock.patch.object(paths, "CONFIG_FILE", Path(self.tmp.name) / "config.json")
        self.config.start()
        paths.CONFIG_FILE.write_text(json.dumps(CONFIG))

    def tearDown(self):
        for p in (self.config, self.logs, self.patch):
            p.stop()
        super().tearDown()

    def choose(self, answers, **kwargs):
        said, answers = [], iter(answers)
        profile = selector.choose(rules_path=self.rules, ask=lambda prompt: next(answers),
                                  say=said.append, **kwargs)
        return profile, "\n".join(said)

    def test_menu_lists_strategies_from_rules_json_and_saves_choice(self):
        profile, text = self.choose(["7", "2", "y"])
        self.assertEqual(profile.key, "etf_breakout")
        self.assertIn("1) Trend Join Long", text)
        self.assertIn("2) ETF Breakout", text)
        self.assertIn("Please enter a number from 1 to 2", text)
        self.assertEqual(selector.saved(), "etf_breakout")

    def test_enter_keeps_current_and_q_quits(self):
        profile, _ = self.choose(["", "y"])
        self.assertEqual(profile.key, "trend_join_long")  # the default is current
        with self.assertRaises(SystemExit):
            self.choose(["q"])
        with self.assertRaises(SystemExit):  # not confirmed
            self.choose(["1", "n"])

    def test_cli_key_and_unattended(self):
        profile, _ = self.choose([], cli_key="etf_breakout", interactive=False)
        self.assertEqual(profile.key, "etf_breakout")
        profile, _ = self.choose([], interactive=False)  # the saved choice
        self.assertEqual(profile.key, "etf_breakout")
        with self.assertRaises(SystemExit):
            self.choose([], cli_key="nope", interactive=False)

    def test_switch_refused_while_positions_open(self):
        self.choose(["1", "y"])
        tjl = profiles.load("trend_join_long", self.rules, CONFIG)
        write_json_atomic(tjl.state_file, {"positions": {"NVDA": {}}, "entered": {}})
        with self.assertRaises(SystemExit) as caught, self.assertLogs("paperbot", "ERROR"):
            self.choose(["2", "y"])
        self.assertIn("still manages NVDA", str(caught.exception))
        profile, _ = self.choose(["1", "y"])  # staying on the same strategy is fine
        self.assertEqual(profile.key, "trend_join_long")

    def test_invalid_strategy_is_listed_but_cannot_be_chosen(self):
        broken = etf_breakout()
        broken["entry"][0]["type"] = "nope"
        self.rules = self.write({"trend_join_long": {**copy.deepcopy(TJL), "state_dir": "t"},
                                 "broken": broken})
        profile, text = self.choose(["2", "1", "y"])
        self.assertIn("2) broken  [INVALID:", text)
        self.assertIn("broken is invalid and cannot be started", text)
        self.assertEqual(profile.key, "trend_join_long")

    def test_no_choice_anywhere_refuses_unattended(self):
        self.rules = self.write({"etf_breakout": etf_breakout()})
        with self.assertRaises(SystemExit), self.assertLogs("paperbot", "ERROR"):
            self.choose([], interactive=False)


class SizingRoundingTest(unittest.TestCase):
    def test_directional_ticks(self):
        self.assertEqual(sizing.to_tick(100.22004, "BUY"), 100.23)
        self.assertEqual(sizing.to_tick(100.299, "SELL"), 100.29)
        self.assertEqual(sizing.to_tick(94.545, "STOP"), 94.54)
        self.assertEqual(sizing.to_tick(100.0 * 1.002, "BUY"), 100.2)  # on the tick: unchanged
        with self.assertRaises(ValueError):
            sizing.to_tick(1.0, "HOLD")

    def test_partial_split(self):
        self.assertEqual(sizing.split_shares(9, 0.3333), 3)
        self.assertEqual(sizing.split_shares(1, 0.3333), 0)
        self.assertEqual(sizing.split_shares(2, 0.9), 1)  # always leaves one


class IndicatorTest(unittest.TestCase):
    def test_sma_and_swing_low(self):
        self.assertEqual(sma([1, 2, 3, 4], 2), 3.5)
        self.assertIsNone(sma([1, 2], 3))
        bars = [NS(low=x) for x in (10, 9, 8, 9, 10, 11, 10.5, 10.2, 10.6, 11)]
        self.assertEqual(swing_low(bars, 2, 2), 10.2)

    def test_relative_volume(self):
        t = dt.time
        history = [[(t(9, 30), 100), (t(9, 35), 100)], [(t(9, 30), 300), (t(9, 35), 300)]]
        self.assertEqual(relative_volume(history, [200], t(9, 30), 2), 1.0)
        self.assertIsNone(relative_volume(history[:1], [200], t(9, 30), 2))


class RefRoleTest(unittest.TestCase):
    def test_roles(self):
        self.assertEqual(ref_role("paperbot:stop"), "stop")
        self.assertEqual(ref_role("paperbot:etf_breakout:entry"), "entry")
        self.assertIsNone(ref_role("manual"))
        self.assertIsNone(ref_role(""))
        self.assertIsNone(ref_role(None))
        self.assertIsNone(ref_role("paperbot:other"))


class EvaluationSafetyTest(unittest.TestCase):
    """A broken block fails one symbol cleanly; scan state never outlives its strategy."""

    def setUp(self):
        import dataclasses
        from paperbot.strategies import rule_strategy as strategy
        self.tmp = tempfile.TemporaryDirectory()
        self.strategy, self.replace = strategy, dataclasses.replace
        self.profile = profiles.load("trend_join_long")
        self.now = dt.datetime(2026, 10, 20, 10, 30, tzinfo=ET)
        self.patches = [
            mock.patch.object(strategy, "SAFETY_LOG", Path(self.tmp.name) / "safety.jsonl"),
            mock.patch("paperbot.core.market_data.contract", return_value=NS(conId=1)),
            mock.patch.object(strategy, "_profile", None)]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        self.tmp.cleanup()

    def broken(self, type_name="broken_check", error=ZeroDivisionError("division by zero")):
        def fail(ctx):
            raise error
        return NS(type=type_name, check=fail, compute=fail)

    def test_crashing_entry_block_fails_the_symbol_not_the_pass(self):
        profile = self.replace(self.profile, entry=(self.broken(),))
        with self.assertLogs("paperbot", "WARNING") as logs:
            result = self.strategy.evaluate(None, "NVDA", profile, self.now)
        self.assertFalse(result["pass"])
        self.assertEqual(result["reasons"][-1],
                         "FAIL broken_check: error ZeroDivisionError('division by zero')")
        self.assertIn("NVDA: broken_check block error", logs.output[0])
        logged = json.loads((Path(self.tmp.name) / "safety.jsonl").read_text())
        self.assertEqual(logged["reasons"], result["reasons"])  # still in the safety log

    def test_crashing_stop_block_fails_the_symbol(self):
        ok = NS(type="always", check=lambda ctx: "A always")
        profile = self.replace(self.profile, entry=(ok,), stop=self.broken("bad_stop", KeyError("low")))
        with self.assertLogs("paperbot", "WARNING"):
            result = self.strategy.evaluate(None, "NVDA", profile, self.now)
        self.assertEqual(result["reasons"], ["A always", "FAIL bad_stop: error KeyError('low')"])
        self.assertIsNone(result["stop"])

    def test_every_result_key_exists_before_any_block_runs(self):
        from paperbot.blocks.context import RESULT_DEFAULTS, EvalContext
        seen = {}
        probe = NS(type="probe", check=lambda ctx: seen.update(ctx.result))
        profile = self.replace(self.profile, entry=(probe,), stop=NS(type="s", compute=lambda c: 1.0))
        self.strategy.evaluate(None, "NVDA", profile, self.now)
        self.assertLessEqual(set(RESULT_DEFAULTS), set(seen))
        partial = {"symbol": "NVDA"}  # a context built from a partial result is completed
        EvalContext(None, None, "NVDA", self.now, profile, partial)
        self.assertEqual({k: partial[k] for k in RESULT_DEFAULTS}, RESULT_DEFAULTS)

    def test_scan_restarts_when_another_strategy_runs(self):
        strategy, universe = self.strategy, ["AAA", "BBB"]
        bar = strategy._bar_start(self.now, self.profile.scan_every)
        evaluated = []
        state = {"positions": {}, "entered": {}, "entry_counts": {}}
        with mock.patch.object(strategy, "_universe", lambda p, today: universe), \
                mock.patch.object(strategy, "net_liquidation_usd", return_value=1e6), \
                mock.patch.object(strategy, "buying_power_usd", return_value=4e6), \
                mock.patch.object(strategy, "_now", lambda: self.now), \
                mock.patch("paperbot.runtime.runstate.stop_requested", return_value=False), \
                mock.patch.object(strategy, "evaluate",
                                  lambda ib, s, p, now: evaluated.append(s) or {"pass": False}):
            # the scan of this bar already finished, but for another strategy
            with mock.patch.multiple(strategy, _scan_key="macro_swing_etf", _scan_bar=bar,
                                     _scan_pos=len(universe), _scan_list=universe):
                strategy._find_entries(None, "DU1", self.profile, state, {}, {}, self.now)
                self.assertEqual(strategy._scan_key, "trend_join_long")
            self.assertEqual(evaluated, universe)
            evaluated.clear()
            with mock.patch.multiple(strategy, _scan_key="trend_join_long", _scan_bar=bar,
                                     _scan_pos=len(universe), _scan_list=universe):
                strategy._find_entries(None, "DU1", self.profile, state, {}, {}, self.now)
            self.assertEqual(evaluated, [])  # same strategy, same bar: nothing rescanned

    def test_configure_resets_the_scan(self):
        strategy = self.strategy
        with mock.patch.multiple(strategy, _scan_key="x", _scan_bar=self.now, _scan_pos=7,
                                 _scan_list=["A"]), self.assertLogs("paperbot", "INFO"):
            strategy.configure(self.profile)
            self.assertEqual((strategy._scan_key, strategy._scan_bar, strategy._scan_pos,
                              strategy._scan_list), (None, None, 0, []))

    def test_decide_refuses_a_profile_other_than_the_selected_one(self):
        strategy = self.strategy
        with mock.patch.object(strategy, "ACTIVE", "macro_swing_etf"), \
                mock.patch.object(strategy, "_profile", self.profile), \
                self.assertLogs("paperbot", "ERROR") as logs, self.assertRaises(RuntimeError):
            strategy.decide(None, "DU1")
        self.assertIn("ALERT strategy mismatch", logs.output[0])

    def test_crashing_exit_check_skips_only_that_position(self):
        from paperbot.core import orders
        held = {"AAA": NS(position=10), "BBB": NS(position=10)}
        state = {"positions": {s: {"stop": 1.0} for s in held}, "entered": {}}
        def check(ctx):
            if ctx.symbol == "AAA":
                raise TypeError("bad bar")
            return "target hit"
        profile = NS(signal_exits=(NS(check=check),), holding="intraday")
        with mock.patch.object(orders, "_close_position",
                               lambda ib, p, symbol, *a: [symbol]), \
                mock.patch.object(orders, "push"), \
                self.assertLogs("paperbot", "WARNING") as logs:
            requests = orders.signal_exits(None, profile, state, held, {}, self.now)
        self.assertEqual(requests, ["BBB"])
        self.assertIn("AAA: exit check error TypeError('bad bar'); its stop stays at IBKR",
                      logs.output[0])
        self.assertNotIn("exit_reason", state["positions"]["AAA"])


if __name__ == "__main__":
    unittest.main()
