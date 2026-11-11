"""Offline tests for setup_schedule.py and cleanup_schedule.py.

schtasks and powercfg are never called: every command goes to a fake runner.
Run: python -m unittest discover -s tests -p test_schedule.py -v
"""
import os

os.environ["PAPERBOT_NOTIFY"] = "off"  # tests must never send real notifications

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock
from zoneinfo import ZoneInfo

from paperbot.ops import cleanup_schedule as cleanup
from paperbot.ops import setup_schedule as setup

SGT, LA = ZoneInfo("Asia/Singapore"), ZoneInfo("America/Los_Angeles")
OCT_19 = dt.date(2026, 10, 19)  # Monday, New York on daylight time (UTC-4)
NOV_02 = dt.date(2026, 11, 2)   # Monday after New York's switch to standard time (UTC-5)

POWERCFG = """Power Scheme GUID: 381b4222-f694-41f0-9685-ff5bb260df2e  (Balanced)
  Subgroup GUID: 238c9fa8-0aad-41ed-83f4-97be242c8f20  (Sleep)
    Power Setting GUID: 29f6c1db-86da-48c5-9fdb-f2b67b1f44da  (Sleep after)
      Minimum Possible Setting: 0x00000000
      Maximum Possible Setting: 0xffffffff
      Possible Settings increment: 0x00000001
      Possible Settings units: Seconds
    Current AC Power Setting Index: 0x00000708
    Current DC Power Setting Index: 0x00000384
"""


def in_zone(zone):
    return lambda moment: moment.astimezone(zone)


class FakeRunner:
    """Records commands; returns success unless the command matches a failing task name."""

    def __init__(self, fail_names=(), query_csv="", powercfg=POWERCFG):
        self.calls, self.fail_names = [], set(fail_names)
        self.query_csv, self.powercfg = query_csv, powercfg

    def __call__(self, cmd):
        self.calls.append(cmd)
        if cmd[:2] == ["powercfg", "/query"]:
            return NS(returncode=0, stdout=self.powercfg, stderr="")
        if cmd[:3] == ["schtasks", "/Query", "/FO"]:
            return NS(returncode=0, stdout=self.query_csv, stderr="")
        if "/TN" in cmd and cmd[cmd.index("/TN") + 1] in self.fail_names:
            return NS(returncode=1, stdout="", stderr="ERROR: Access is denied.")
        return NS(returncode=0, stdout="", stderr="")

    def creates(self):
        return [c for c in self.calls if c[:2] == ["schtasks", "/Create"]]


XML_DAY_NAMES = {"MON": "Monday", "TUE": "Tuesday", "WED": "Wednesday", "THU": "Thursday",
                 "FRI": "Friday", "SAT": "Saturday", "SUN": "Sunday"}


def task_xml(cmd):
    """The trigger part of `schtasks /Query /XML` for a task made by this /Create command."""
    start = f"<StartBoundary>2026-11-01T{cmd[cmd.index('/ST') + 1]}:00</StartBoundary>"
    if "/D" not in cmd:
        return f"<Triggers><CalendarTrigger>{start}<ScheduleByDay /></CalendarTrigger></Triggers>"
    days = "".join(f"<{XML_DAY_NAMES[d]} />\n" for d in cmd[cmd.index("/D") + 1].split(","))
    return (f"<Triggers><CalendarTrigger>{start}<ScheduleByWeek>"
            f"<DaysOfWeek>\n{days}</DaysOfWeek></ScheduleByWeek></CalendarTrigger></Triggers>")


class XmlRunner(FakeRunner):
    """FakeRunner whose Task Scheduler holds the given /Create commands (by task name)."""

    def __init__(self, registered):
        super().__init__()
        self.registered = registered

    def __call__(self, cmd):
        if cmd[:2] == ["schtasks", "/Query"] and cmd[-1] == "/XML":
            self.calls.append(cmd)
            name = cmd[cmd.index("/TN") + 1]
            if name not in self.registered:
                return NS(returncode=1, stdout="", stderr="ERROR: not found")
            return NS(returncode=0, stdout=task_xml(self.registered[name]), stderr="")
        return super().__call__(cmd)


class LocalTimeTest(unittest.TestCase):
    def test_singapore_shifts_weekdays_after_midnight(self):
        self.assertEqual(setup.local_slot("09:25", OCT_19, in_zone(SGT)),
                         ("21:25", ["MON", "TUE", "WED", "THU", "FRI"]))
        self.assertEqual(setup.local_slot("16:05", OCT_19, in_zone(SGT)),
                         ("04:05", ["TUE", "WED", "THU", "FRI", "SAT"]))

    def test_new_york_standard_time_moves_local_times(self):
        self.assertEqual(setup.local_slot("16:05", NOV_02, in_zone(SGT))[0], "05:05")
        self.assertEqual(setup.local_slot("09:55", NOV_02, in_zone(SGT))[0], "22:55")

    def test_registering_around_the_november_switch_uses_standard_time(self):
        # New York leaves daylight time on Sunday 2026-11-01: from the Saturday
        # before or the Sunday itself, the coming weekdays are on standard time.
        for today in (dt.date(2026, 10, 31), dt.date(2026, 11, 1)):
            self.assertEqual(setup.local_slot("09:30", today, in_zone(SGT))[0], "22:30", today)

    def test_registering_around_the_march_switch_uses_daylight_time(self):
        for today in (dt.date(2027, 3, 13), dt.date(2027, 3, 14)):
            self.assertEqual(setup.local_slot("09:30", today, in_zone(SGT))[0], "21:30", today)

    def test_registering_midweek_keeps_monday_to_friday(self):
        self.assertEqual(setup.local_slot("09:30", dt.date(2026, 11, 11), in_zone(SGT)),
                         ("22:30", ["MON", "TUE", "WED", "THU", "FRI"]))

    def test_pacific_time(self):
        self.assertEqual(setup.local_slot("09:25", OCT_19, in_zone(LA)),
                         ("06:25", ["MON", "TUE", "WED", "THU", "FRI"]))


class CommandTest(unittest.TestCase):
    def setUp(self):
        self.commands = {name: cmd for name, cmd, _ in setup.build_commands(OCT_19, in_zone(SGT))}

    def test_eleven_tasks(self):
        self.assertEqual(len(self.commands), 11)
        self.assertEqual(list(self.commands), setup.TASK_NAMES)
        self.assertEqual(sum(n.startswith("HT_Prefilter_") for n in self.commands), 7)

    def test_cycle_every_5_minutes_in_the_session_on_weekdays_only(self):
        """09:30-16:10 ET Mon-Fri, not every 5 minutes around the clock."""
        cmd = self.commands["HT_Cycle"]
        self.assertEqual(cmd[cmd.index("/SC") + 1], "WEEKLY")
        self.assertEqual(cmd[cmd.index("/D") + 1], "MON,TUE,WED,THU,FRI")  # 21:30 SGT, same day
        self.assertEqual(cmd[cmd.index("/RI") + 1], "5")
        self.assertEqual(cmd[cmd.index("/DU") + 1], "06:40")  # until 16:10 ET (04:10 SGT)
        self.assertNotIn("MINUTE", cmd)
        self.assertTrue(cmd[cmd.index("/TR") + 1].endswith("-m paperbot.runtime.cycle --submit"))
        self.assertNotIn("/SD", cmd)
        self.assertEqual(cmd[cmd.index("/ST") + 1], "21:30")  # 09:30 EDT in Singapore

    def test_dashboard_runs_on_singapore_weekdays(self):
        cmd = self.commands["HT_Dashboard"]
        self.assertEqual(cmd[cmd.index("/D") + 1], "TUE,WED,THU,FRI,SAT")
        self.assertEqual(cmd[cmd.index("/ST") + 1], "04:05")

    def test_no_elevation_and_overwrite(self):
        for cmd in self.commands.values():
            self.assertIn("/F", cmd)
            for forbidden in ("/RL", "/RU", "HIGHEST", "SYSTEM"):
                self.assertNotIn(forbidden, cmd)

    def test_paths_quoted_only_with_spaces(self):
        self.assertNotIn('"', setup.task_run(["paperbot.ops.rotate_logs"]))
        with mock.patch.object(setup, "VENV_PY", Path(r"C:\My Envs\python.exe")):
            self.assertTrue(setup.task_run(["paperbot.runtime.cycle"]).startswith(r'"C:\My Envs\python.exe" '))


class SetupMainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        logs = Path(self.tmp.name)
        self.patches = [mock.patch.object(setup, "LOGS", logs),
                        mock.patch.object(setup, "STATE_FILE", logs / "schedule_state.json"),
                        mock.patch.object(setup, "POWER_BACKUP", logs / "power_backup.json"),
                        mock.patch("builtins.print")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def test_full_setup(self):
        runner = FakeRunner()
        self.assertEqual(setup.main([], runner, in_zone(SGT)), 0)
        self.assertEqual(len(runner.creates()), 11)
        self.assertIn(["schtasks", "/Delete", "/TN", "paperbot cycle", "/F"], runner.calls)
        self.assertIn(["powercfg", "/change", "standby-timeout-ac", "0"], runner.calls)
        self.assertEqual(json.loads(setup.POWER_BACKUP.read_text()),
                         {"ac_seconds": 1800, "dc_seconds": 900})
        self.assertTrue(setup.STATE_FILE.exists())

    def test_failure_does_not_stop_other_tasks(self):
        runner = FakeRunner(fail_names={"HT_Prefilter_03"})
        self.assertEqual(setup.main([], runner, in_zone(SGT)), 1)
        self.assertEqual(len(runner.creates()), 11)  # all attempted

    def test_rerun_keeps_the_original_power_backup(self):
        setup.main([], FakeRunner(), in_zone(SGT))
        never_sleep = POWERCFG.replace("0x00000708", "0x00000000")  # after keep-awake
        setup.main([], FakeRunner(powercfg=never_sleep), in_zone(SGT))
        self.assertEqual(json.loads(setup.POWER_BACKUP.read_text())["ac_seconds"], 1800)

    def test_dst_check_only_reregisters_on_change(self):
        setup.main([], FakeRunner(), in_zone(SGT))
        unchanged = FakeRunner()
        self.assertEqual(setup.main(["--if-dst-changed"], unchanged, in_zone(SGT)), 0)
        self.assertEqual(unchanged.creates(), [])
        setup.STATE_FILE.write_text(json.dumps({"et": -240, "local": 480}))  # summer values
        with mock.patch.object(setup, "utc_offsets", return_value={"et": -300, "local": 480}):
            changed = FakeRunner()
            setup.main(["--if-dst-changed"], changed, in_zone(SGT))
        self.assertEqual(len(changed.creates()), 11)
        self.assertNotIn(["powercfg", "/change", "standby-timeout-ac", "0"], changed.calls)

    def test_daily_check_repairs_wrong_start_times_even_with_matching_offsets(self):
        # What happened on 2026-11-01: offsets saved, but HT_Cycle left an hour early.
        today = dt.date(2026, 11, 11)
        good = {n: c for n, c, _ in setup.build_commands(today, in_zone(SGT))}
        bad = dict(good)
        bad["HT_Cycle"] = [("21:30" if x == "22:30" else x) for x in good["HT_Cycle"]]
        setup.STATE_FILE.write_text(json.dumps({"et": -300, "local": 480}))
        with mock.patch.object(setup, "utc_offsets", return_value={"et": -300, "local": 480}), \
             mock.patch.object(setup.dt, "datetime", wraps=dt.datetime) as clock:
            clock.now.return_value = dt.datetime(2026, 11, 11, 13, tzinfo=dt.timezone.utc)
            broken = XmlRunner(bad)
            setup.main(["--if-dst-changed"], broken, in_zone(SGT))
            self.assertEqual(len(broken.creates()), 11)
            fine = XmlRunner(good)
            self.assertEqual(setup.main(["--if-dst-changed"], fine, in_zone(SGT)), 0)
            self.assertEqual(fine.creates(), [])

    def test_daily_check_registers_missing_task(self):
        today = dt.date(2026, 11, 11)
        cmds = {n: c for n, c, _ in setup.build_commands(today, in_zone(SGT))}
        del cmds["HT_Dashboard"]
        self.assertEqual(setup.schedule_mismatches(today, XmlRunner(cmds), in_zone(SGT)),
                         ["HT_Dashboard"])

    def test_read_standby(self):
        self.assertEqual(setup.read_standby(FakeRunner()), {"ac_seconds": 1800, "dc_seconds": 900})
        self.assertIsNone(setup.read_standby(FakeRunner(powercfg="garbled")))


class CleanupTest(unittest.TestCase):
    QUERY = ('"\\HT_Cycle","N/A","Ready"\r\n"\\HT_Cycle","N/A","Ready"\r\n'
             '"\\HT_Dashboard","10/20/2026 4:05:00 AM","Ready"\r\n'
             '"\\paperbot cycle","N/A","Disabled"\r\n'
             '"\\Microsoft\\Windows\\HT_fake","N/A","Ready"\r\n')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        logs = Path(self.tmp.name)
        self.patches = [mock.patch.object(cleanup, "POWER_BACKUP", logs / "power_backup.json"),
                        mock.patch.object(cleanup, "STATE_FILE", logs / "schedule_state.json"),
                        mock.patch("builtins.print")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def test_only_top_level_ht_tasks(self):
        self.assertEqual(cleanup.ht_tasks(FakeRunner(query_csv=self.QUERY)),
                         ["HT_Cycle", "HT_Dashboard"])

    def test_deletes_and_restores_saved_power(self):
        cleanup.POWER_BACKUP.write_text(json.dumps({"ac_seconds": 3600, "dc_seconds": 900}))
        runner = FakeRunner(query_csv=self.QUERY)
        self.assertEqual(cleanup.main(runner), 0)
        self.assertIn(["schtasks", "/Delete", "/TN", "HT_Cycle", "/F"], runner.calls)
        self.assertIn(["schtasks", "/Delete", "/TN", "HT_Dashboard", "/F"], runner.calls)
        self.assertIn(["powercfg", "/change", "standby-timeout-ac", "60"], runner.calls)
        self.assertFalse(cleanup.POWER_BACKUP.exists())

    def test_default_power_without_backup(self):
        runner = FakeRunner(query_csv=self.QUERY)
        cleanup.main(runner)
        self.assertIn(["powercfg", "/change", "standby-timeout-ac", "30"], runner.calls)

    def test_nothing_to_clean(self):
        runner = FakeRunner(query_csv='"\\paperbot cycle","N/A","Disabled"\r\n')
        with mock.patch("builtins.print") as printed:
            self.assertEqual(cleanup.main(runner), 0)
        printed.assert_called_with("Nothing to clean.")
        self.assertFalse(any(c[0] == "powercfg" for c in runner.calls))


if __name__ == "__main__":
    unittest.main()
