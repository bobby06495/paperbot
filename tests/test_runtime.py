"""Offline tests for the cross-platform runtime: broker modes (paper/live), per-mode
folders, the built-in scheduler, the reconnecting session loop, the paper-bot
CLI and the per-OS service definitions. No Gateway, no OS scheduler, no network.

Run: python -m unittest discover -s tests -p test_runtime.py -v
"""
import os

os.environ["PAPERBOT_NOTIFY"] = "off"  # tests must never send real notifications

import datetime as dt
import json
import logging
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from types import SimpleNamespace as NS
from unittest import mock

from paperbot import cli, paths
from paperbot.core import broker, clock
from paperbot.core import engine
from paperbot.core.common import ET
from paperbot.core.risk import ConfigError
from paperbot.ops import launch
from paperbot.reporting import notify
from paperbot.system import services

PAPER = {"mode": "paper", "connections": {
    "paper": {"host": "127.0.0.1", "port": 4002, "client_id": 21},
    "live": {"host": "127.0.0.1", "port": 4001, "client_id": 21, "account_id": "U0000000"}}}
LIVE = {**PAPER, "mode": "live"}


def at(hour, minute, day=20):  # 2026-10-20 is a Tuesday
    return dt.datetime(2026, 10, day, hour, minute, tzinfo=ET)


# ---------------------------------------------------------------- broker

class BrokerSettingsTest(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        for key in ("PAPERBOT_IB_HOST", "PAPERBOT_IB_PORT", "PAPERBOT_LIVE_CONFIRM"):
            os.environ.pop(key, None)

    def tearDown(self):
        self.env.stop()

    def test_paper_and_live_from_config_only(self):
        self.assertEqual(broker.settings(PAPER), broker.Connection("paper", "127.0.0.1", 4002, 21))
        live = broker.settings(LIVE)
        self.assertEqual((live.mode, live.port, live.account_id), ("live", 4001, "U0000000"))

    def test_original_flat_layout_is_paper_only(self):
        flat = {"host": "127.0.0.1", "port": 4002, "client_id": 21}
        self.assertEqual(broker.settings(flat).mode, "paper")
        with self.assertRaises(ConfigError):
            broker.settings({**flat, "mode": "live"})

    def test_port_must_match_mode(self):
        with self.assertRaises(ConfigError):
            broker.settings({**PAPER, "connections": {**PAPER["connections"],
                             "paper": {"host": "h", "port": 4001, "client_id": 1}}})
        with self.assertRaises(ConfigError):
            broker.settings({**LIVE, "connections": {**LIVE["connections"],
                             "live": {**LIVE["connections"]["live"], "port": 4002}}})

    def test_live_account_id_required_and_valid(self):
        for account in (None, "", "DU000000", "SET_YOUR_LIVE_ACCOUNT_ID"):
            live = {**LIVE["connections"]["live"], "account_id": account}
            with self.subTest(account=account), self.assertRaises(ConfigError):
                broker.settings({**LIVE, "connections": {**LIVE["connections"], "live": live}})

    def test_bad_mode_and_env_overrides(self):
        with self.assertRaises(ConfigError):
            broker.settings({**PAPER, "mode": "Live"})
        os.environ.update(PAPERBOT_IB_HOST="ib-gateway", PAPERBOT_IB_PORT="4004")
        conn = broker.settings(PAPER)
        self.assertEqual((conn.host, conn.port), ("ib-gateway", 4004))
        os.environ["PAPERBOT_IB_PORT"] = "4003"  # a live relay port in paper mode
        with self.assertRaises(ConfigError):
            broker.settings(PAPER)


class AccountCheckTest(unittest.TestCase):
    def test_paper_needs_one_paper_account(self):
        conn = broker.settings(PAPER)
        self.assertEqual(broker.verify_account(conn, ["DU0000000"]), "DU0000000")
        for accounts in (["U0000000"], ["DU1", "DU2"], []):
            with self.subTest(accounts=accounts), self.assertRaises(SystemExit):
                broker.verify_account(conn, accounts)

    def test_live_needs_exactly_the_configured_account(self):
        conn = broker.settings(LIVE)
        self.assertEqual(broker.verify_account(conn, ["U0000000"]), "U0000000")
        for accounts in (["U00000000"], ["U0000000", "U00000000"], ["DU0000000"]):
            with self.subTest(accounts=accounts), self.assertRaises(SystemExit):
                broker.verify_account(conn, accounts)


class LiveConfirmTest(unittest.TestCase):
    def setUp(self):
        self.conn = broker.settings(LIVE)
        os.environ.pop("PAPERBOT_LIVE_CONFIRM", None)

    def test_paper_needs_no_confirmation(self):
        broker.confirm_live(broker.settings(PAPER), interactive=False)

    def test_typed_account_id(self):
        with self.assertLogs("paperbot", "WARNING"):
            broker.confirm_live(self.conn, True, ask=lambda p: "U0000000", say=lambda s: None)
        with self.assertRaises(SystemExit):
            broker.confirm_live(self.conn, True, ask=lambda p: "yes", say=lambda s: None)

    def test_unattended_needs_env(self):
        with self.assertRaises(SystemExit), self.assertLogs("paperbot", "ERROR"):
            broker.confirm_live(self.conn, interactive=False)
        with mock.patch.dict(os.environ, {"PAPERBOT_LIVE_CONFIRM": "U0000000"}), \
                self.assertLogs("paperbot", "WARNING"):
            broker.confirm_live(self.conn, interactive=False)
        with mock.patch.dict(os.environ, {"PAPERBOT_LIVE_CONFIRM": "U000000"}), \
                self.assertRaises(SystemExit), self.assertLogs("paperbot", "ERROR"):
            broker.confirm_live(self.conn, interactive=False)


class ModeFoldersTest(unittest.TestCase):
    """paths.py separates live state from paper state (checked in a fresh interpreter)."""

    def folders(self, config: dict) -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "config").mkdir()
            (Path(tmp) / "config" / "config.json").write_text(json.dumps(config))
            code = ("from paperbot import paths; print(paths.MODE); "
                    "print(paths.LOGS_DIR.relative_to(paths.PROJECT_ROOT).as_posix()); "
                    "print(paths.TRADES_CSV.relative_to(paths.PROJECT_ROOT).as_posix())")
            out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                                 env={**os.environ, "PAPERBOT_HOME": tmp}, check=True)
            return out.stdout.split()

    def test_paper_keeps_original_folders_live_gets_its_own(self):
        self.assertEqual(self.folders(PAPER), ["paper", "logs", "data/trades.csv"])
        self.assertEqual(self.folders(LIVE), ["live", "logs/live", "data/live/trades.csv"])

    def test_live_title_on_every_push(self):
        with mock.patch.object(paths, "MODE", "live"):
            self.assertEqual(notify.live_title("BUY NVDA"), "[LIVE] BUY NVDA")
        self.assertEqual(notify.live_title("BUY NVDA"), "BUY NVDA")


# ---------------------------------------------------------------- built-in scheduler

class FakeProc:
    def __init__(self, codes):
        self.codes, self.pid = list(codes), 4242

    def poll(self):
        return self.codes.pop(0) if self.codes else 0


class ClockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "scheduler_state.json"
        self.calls, self.started = [], []

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, proc_codes=(None, 0), watchlist=True):
        jobs = [clock.Job("rotate_logs", ("09:25",), dt.timedelta(hours=6),
                          call=lambda: self.calls.append("rotate")),
                clock.Job("prefilter", clock.PREFILTER_TIMES, dt.timedelta(minutes=25),
                          command=["python", "-m", "x"], enabled=lambda: watchlist)]

        def popen(cmd, cwd=None):
            self.started.append(cmd)
            return FakeProc(proc_codes)
        return clock.Clock(jobs, self.state, popen=popen)

    def test_slots_run_once_and_state_survives_restart(self):
        c = self.make()
        with self.assertLogs("paperbot", "INFO"):
            c.tick(at(9, 25))
            c.tick(at(9, 26))
        self.assertEqual(self.calls, ["rotate"])
        c2 = self.make()  # a restart reads the saved state
        c2.tick(at(9, 40))
        self.assertEqual(self.calls, ["rotate"])
        self.assertEqual(json.loads(self.state.read_text())["jobs"]["rotate_logs"]["status"], "ok")

    def test_late_start_catches_up_latest_slot_within_grace_only(self):
        c = self.make()
        with self.assertLogs("paperbot", "INFO"):
            c.tick(at(11, 40))  # 11:25 slot is 15 min old: runs; earlier slots skipped
        self.assertEqual(len(self.started), 1)
        self.assertTrue(c.state["jobs"]["prefilter"]["slot"].startswith("2026-10-20T11:25"))
        c.tick(at(11, 52))  # nothing new due (11:55 not yet)
        self.assertEqual(len(self.started), 1)

    def test_outside_grace_weekend_and_disabled(self):
        self.make().tick(at(13, 30))  # 12:55 + 25 min = 13:20: too late
        self.assertEqual(self.started, [])
        self.make().tick(at(10, 0, day=24))  # Saturday
        self.assertEqual(self.started, [])
        self.make(watchlist=False).tick(at(10, 0))  # strategy without the watchlist
        self.assertEqual(self.started, [])

    def test_background_job_still_running_skips_next_slot(self):
        c = self.make(proc_codes=(None, None, None))
        with self.assertLogs("paperbot", "INFO"):
            c.tick(at(9, 55))
        with self.assertLogs("paperbot", "WARNING") as logs:
            c.tick(at(10, 25))
        self.assertEqual(len(self.started), 1)
        self.assertIn("previous run still going", logs.output[-1])

    def test_finished_job_status_is_recorded(self):
        c = self.make(proc_codes=(None, 3))
        with self.assertLogs("paperbot", "INFO"):
            c.tick(at(9, 55))
            c.tick(at(9, 56))
        with self.assertLogs("paperbot", "ERROR"):
            c.tick(at(9, 57))
        job = json.loads(self.state.read_text())["jobs"]["prefilter"]
        self.assertEqual((job["status"], job["exit_code"]), ("failed", 3))

    def test_in_process_failure_never_raises(self):
        c = self.make()
        c.jobs[0].call = mock.Mock(side_effect=RuntimeError("disk full"))
        with self.assertLogs("paperbot", "ERROR") as logs:
            c.tick(at(9, 30))
        self.assertIn("rotate_logs failed", logs.output[-1])
        self.assertEqual(c.state["jobs"]["rotate_logs"]["status"], "failed")

    def test_default_jobs_follow_the_strategy_universe(self):
        jobs = clock.default_jobs(NS(universe={"source": "watchlist"}), rotate=lambda: None)
        self.assertEqual([j.name for j in jobs], ["rotate_logs", "prefilter", "daily_summary"])
        self.assertTrue(jobs[1].enabled())
        self.assertFalse(clock.default_jobs(NS(universe={"symbols": ["SPY"]}), lambda: None)[1].enabled())


# ---------------------------------------------------------------- session loop

class Event:
    def __iadd__(self, handler):
        return self


class SessionIB:
    """IB stand-in: connect() fails `down` times, then succeeds; accounts can change."""

    def __init__(self, down=0, accounts=(["DU1"],)):
        self.down, self.accounts = down, list(accounts)
        self.connected, self.connects, self.disconnects = False, 0, 0
        self.orderStatusEvent = self.execDetailsEvent = Event()

    def connect(self, *args, **kwargs):
        self.connects += 1
        if self.down:
            self.down -= 1
            raise ConnectionRefusedError("gateway down")
        self.connected = True

    def isConnected(self):
        return self.connected

    def disconnect(self):
        self.connected = False
        self.disconnects += 1

    def managedAccounts(self):
        return self.accounts[0] if len(self.accounts) == 1 else self.accounts.pop(0)

    def reqPnL(self, account):
        return NS(dailyPnL=0.0)

    def sleep(self, seconds):
        pass

    def openTrades(self):
        return []


class SessionLoopTest(unittest.TestCase):
    CFG = {"loop_seconds": 30}

    def run_session(self, ib, passes=None, max_passes=3):
        conn = broker.settings(PAPER)
        profile = NS(key="trend_join_long", limits="L", universe={"source": "watchlist"})
        passes = passes or mock.Mock(return_value=None)
        with mock.patch.object(engine, "one_pass", passes), \
                mock.patch.object(engine, "_wait"), \
                mock.patch.object(engine.clock, "Clock") as fake_clock, \
                mock.patch("paperbot.runtime.runstate.stop_requested", return_value=False):
            engine.run_session(conn, self.CFG, profile, mock.Mock(), True, ib=ib,
                               max_passes=max_passes)
        return passes, fake_clock

    def test_waits_for_gateway_then_trades(self):
        ib = SessionIB(down=2)
        with self.assertLogs("paperbot", "INFO") as logs:
            passes, fake_clock = self.run_session(ib)
        self.assertEqual(ib.connects, 3)
        self.assertEqual(passes.call_count, 3)
        alerts = [l for l in logs.output if "ALERT IB Gateway not reachable" in l]
        self.assertEqual(len(alerts), 1)  # one alert per outage, not per retry
        self.assertTrue(any("connection to IB Gateway restored" in l for l in logs.output))
        self.assertGreaterEqual(fake_clock.return_value.tick.call_count, 5)  # jobs run while down

    def test_connection_drop_mid_session_reconnects(self):
        ib = SessionIB()
        passes = mock.Mock(side_effect=[None, ConnectionError("Not connected"), None, None])
        with self.assertLogs("paperbot", "INFO") as logs:
            self.run_session(ib, passes, max_passes=3)
        self.assertEqual(ib.connects, 2)
        self.assertTrue(any("ALERT connection to IB Gateway lost" in l for l in logs.output))

    def test_account_change_after_reconnect_stops(self):
        ib = SessionIB(accounts=(["DU1"], ["DU2"]))
        passes = mock.Mock(side_effect=[None, ConnectionError("Not connected")])
        with self.assertRaises(SystemExit), self.assertLogs("paperbot", "INFO"):
            self.run_session(ib, passes, max_passes=5)

    def test_stop_file_cancels_working_orders_and_ends(self):
        ib = SessionIB()
        ib.connected = True
        conn = broker.settings(PAPER)
        with mock.patch.object(engine, "cancel_working") as cancel, \
                mock.patch.object(engine.clock, "Clock"), \
                mock.patch("paperbot.runtime.runstate.stop_requested", return_value=True), \
                self.assertLogs("paperbot", "WARNING"):
            engine.run_session(conn, self.CFG, NS(universe={}), mock.Mock(), True, ib=ib)
        cancel.assert_called_once_with(ib, "STOP file")


class PreflightTest(unittest.TestCase):
    """engine.preflight: the start-up summary, and refusal when anything disagrees."""

    def setUp(self):
        from paperbot.core import profile as profiles
        from paperbot.core.risk import RiskGuard
        from paperbot.strategies import rule_strategy as strategy
        self.tmp = tempfile.TemporaryDirectory()
        self.profiles, self.strategy = profiles, strategy
        self.profile = profiles.load("trend_join_long")
        self.guard = RiskGuard(self.profile.limits, Path(self.tmp.name) / "risk.json")
        self.conn = broker.settings(PAPER)
        self.patches = [mock.patch.object(strategy, "_profile", None),
                        mock.patch("paperbot.core.selector.unattended_key",
                                   return_value="trend_join_long")]
        for patch in self.patches:
            patch.start()
        with self.assertLogs("paperbot", "INFO"):
            strategy.configure(self.profile)

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        self.tmp.cleanup()

    def preflight(self, guard=None, rules_path=None):
        cfg = json.loads(paths.CONFIG_FILE.read_text())
        engine.preflight(self.conn, self.profile, guard or self.guard, cfg, rules_path)

    def refused(self, **kwargs) -> str:
        with self.assertLogs("paperbot", "INFO") as logs, self.assertRaises(SystemExit):
            self.preflight(**kwargs)
        alerts = [l for l in logs.output if "ALERT refusing to start" in l]
        self.assertTrue(alerts, logs.output)
        return "\n".join(alerts)

    def test_summary_names_connection_strategy_hash_and_every_limit(self):
        with self.assertLogs("paperbot", "INFO") as logs:
            self.preflight()
        text = "\n".join(logs.output)
        self.assertIn("PREFLIGHT PAPER via 127.0.0.1:4002 (clientId 21)", text)
        self.assertRegex(text, r"PREFLIGHT strategy trend_join_long \(.+\) from rules.json "
                               r"sha256 [0-9a-f]{12}")
        for name, value in vars(self.profile.limits).items():
            self.assertIn(f"{name}={value}", text)
        self.assertFalse([l for l in logs.output if "ERROR" in l])

    def test_guard_with_other_limits_is_refused(self):
        from paperbot.core.risk import RiskGuard
        other = RiskGuard(self.strategy.with_limits(self.profile, max_daily_loss=9999.0).limits,
                          Path(self.tmp.name) / "other.json")
        self.assertIn("risk guard limits differ", self.refused(guard=other))

    def test_rules_changed_on_disk_is_refused(self):
        rules = json.loads(paths.RULES_FILE.read_text())
        rules["strategies"]["trend_join_long"]["risk"]["max_daily_loss"] = 1500
        edited = Path(self.tmp.name) / "rules.json"
        edited.write_text(json.dumps(rules))
        self.assertIn("rules.json changed since", self.refused(rules_path=edited))

    def test_unreadable_rules_are_refused(self):
        missing = Path(self.tmp.name) / "rules.json"
        self.assertIn("cannot re-check rules.json", self.refused(rules_path=missing))

    def test_other_selected_strategy_is_refused(self):
        with mock.patch("paperbot.core.selector.unattended_key", return_value="macro_swing_etf"):
            self.assertIn("selected strategy 'macro_swing_etf' is not 'trend_join_long'",
                          self.refused())

    def test_strategy_module_running_another_profile_is_refused(self):
        with self.assertLogs("paperbot", "INFO"):
            self.strategy.configure(self.profiles.load("macro_swing_etf"))
        self.assertIn("strategy module runs 'macro_swing_etf'", self.refused())

    def test_runs_before_anything_connects(self):
        import inspect
        source = inspect.getsource(engine.main)
        self.assertLess(source.index("preflight("), source.index("claim_pid()"))
        self.assertLess(source.index("preflight("), source.index("run_session("))
        from paperbot.core import scheduled_pass
        source = inspect.getsource(scheduled_pass.run)
        self.assertLess(source.index("preflight("), source.index("connect_with_retry("))


class RotateLogsNowTest(unittest.TestCase):
    def test_bot_log_closed_for_rotation_and_reopened(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(engine, "LOGS", Path(tmp)):
                handler = engine._file_handler()
                engine.log.addHandler(handler)
                try:  # (not assertLogs: it swaps the logger's handlers out)
                    with mock.patch("paperbot.ops.rotate_logs.main",
                                    side_effect=lambda: print("Rotated 2 files")), \
                            mock.patch.object(engine.log, "info") as info:
                        engine.rotate_logs_now()
                    files = [h for h in engine.log.handlers if isinstance(h, logging.FileHandler)]
                    self.assertEqual(len(files), 1)
                    self.assertIsNot(files[0], handler)  # a fresh handler on the new bot.log
                    self.assertIsNone(handler.stream)  # the old one was closed for the move
                    info.assert_any_call("log rotation: %s", "Rotated 2 files")
                finally:
                    for h in [h for h in engine.log.handlers if isinstance(h, logging.FileHandler)]:
                        engine.log.removeHandler(h)
                        h.close()


# ---------------------------------------------------------------- CLI and services

class CliTest(unittest.TestCase):
    def start(self, argv, env=None):
        with mock.patch("paperbot.core.engine.main") as main, \
                mock.patch.dict(os.environ, env or {}), \
                mock.patch.object(cli.sys.stdin, "isatty", return_value=False):
            cli.main(["start", *argv])
        return main.call_args.args[0]

    def test_start_sends_orders_unless_dry_run(self):
        self.assertEqual(self.start([]), ["--submit"])
        self.assertEqual(self.start(["--dry-run", "--yes"]), ["--yes"])
        self.assertEqual(self.start(["--strategy", "x"]), ["--submit", "--strategy", "x"])

    def test_strategy_from_environment(self):
        os.environ.pop("PAPERBOT_STRATEGY", None)
        self.assertIn("swing", self.start([], {"PAPERBOT_STRATEGY": "swing"}))

    def test_status_never_raises(self):
        lines = cli.status_lines()
        self.assertTrue(lines[0].startswith("mode:"))
        self.assertTrue(any(l.startswith("process:") for l in lines))

    def test_every_command_parses(self):
        parser = cli.build_parser()
        for argv in (["stop"], ["status"], ["strategies"], ["check"], ["dashboard", "--open"],
                     ["kill", "--all"], ["service", "install", "--dry-run"], ["test"]):
            with self.subTest(argv=argv):
                self.assertTrue(callable(parser.parse_args(argv).func))

    def test_old_command_flags_translate(self):
        self.assertEqual(launch._translate(["-Yes", "-SkipTest", "-DryRun"]), ["--yes", "--dry-run"])


class ServicePlanTest(unittest.TestCase):
    PY = PurePosixPath("/opt/py/bin/python3")

    def test_linux_systemd_user_unit(self):
        p = services.plan("linux", Path("/home/u"), self.PY)
        (path, text), = p.files.items()
        self.assertEqual(path, Path("/home/u/.config/systemd/user/paper-bot.service"))
        self.assertIn("ExecStart=/opt/py/bin/python3 -m paperbot.cli start --yes", text)
        self.assertIn("Restart=on-failure", text)
        self.assertIn(["systemctl", "--user", "enable", "--now", "paper-bot"], p.install)
        self.assertTrue(any("enable-linger" in n for n in p.notes))

    def test_macos_launch_agent(self):
        p = services.plan("darwin", Path("/Users/u"), self.PY)
        (path, text), = p.files.items()
        self.assertEqual(path.name, "com.paperbot.agent.plist")
        self.assertIn("<string>paperbot.cli</string>", text)
        self.assertIn("<key>RunAtLoad</key><true/>", text)

    def test_windows_logon_task(self):
        p = services.plan("win32", Path("C:/Users/u"), Path("C:/Program Files/Py/python.exe"),
                          env={"PAPERBOT_LIVE_CONFIRM": "U0000000"})
        create = p.install[0]
        self.assertEqual(create[:6], ["schtasks", "/Create", "/TN", "PaperBot", "/SC", "ONLOGON"])
        tr = create[create.index("/TR") + 1]
        self.assertIn('"C:\\Program Files\\Py\\python.exe" -m paperbot.cli start --yes', tr)
        self.assertTrue(tr.startswith('cmd /c "set PAPERBOT_LIVE_CONFIRM=U0000000 && '))

    def test_live_install_needs_explicit_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            config.write_text(json.dumps(LIVE))
            with mock.patch.object(paths, "CONFIG_FILE", config), \
                    mock.patch("paperbot.core.selector.unattended_key", return_value="trend_join_long"), \
                    mock.patch("builtins.print") as printed:
                self.assertEqual(services.install(dry_run=True, system="linux", home=Path(tmp)), 1)
                self.assertIn("--live-confirm U0000000", str(printed.call_args_list))
                self.assertEqual(services.install(dry_run=True, live_confirm="U0000000",
                                                  system="linux", home=Path(tmp)), 0)
            self.assertFalse((Path(tmp) / ".config").exists())  # dry run wrote nothing


class DashboardBuiltinSchedulerTest(unittest.TestCase):
    def test_failed_job_today_is_a_warning(self):
        from paperbot.reporting import dashboard as dash
        now = at(11, 0)
        builtin = {"updated": now.isoformat(), "jobs": {
            "prefilter": {"status": "failed", "started": now.isoformat()}}}
        checks = dash.health_checks(now, {"available": False}, {}, [], [], {}, {}, True, None,
                                    "entry_window", False, None, builtin)
        sched = next(c for c in checks if c["key"] == "scheduler")
        self.assertEqual((sched["label"], sched["status"]), ("Built-in scheduler", "warn"))
        self.assertIn("prefilter", sched["detail"])


if __name__ == "__main__":
    unittest.main()
