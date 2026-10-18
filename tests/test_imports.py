"""Setup tests: every module imports without side effects, and check_setup passes.

Run: python -m unittest discover -s tests
"""
import os

os.environ["PAPERBOT_NOTIFY"] = "off"  # tests must never send real notifications

import importlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from paperbot import paths
from paperbot.ops import check_setup
from paperbot.runtime import runstate


class ImportTest(unittest.TestCase):
    def test_modules_import_without_side_effects(self):
        stop_before = runstate.STOP_FILE.exists()
        with mock.patch("ib_async.IB.connect") as connect:
            for name in check_setup.MODULES + ("paperbot.ops.check_setup",):
                importlib.import_module(name)
        connect.assert_not_called()
        self.assertEqual(runstate.STOP_FILE.exists(), stop_before)

    def test_cycle_import_is_lightweight(self):
        """cycle's off-hours exit must not pay for importing ib_async/pandas/the bot."""
        code = ("import sys, paperbot.runtime.cycle; print(any(m in sys.modules for m in "
                "('ib_async', 'pandas', 'paperbot.trading.bot')))")
        with tempfile.TemporaryDirectory() as elsewhere:  # works from any working directory
            out = subprocess.run([sys.executable, "-c", code], cwd=elsewhere,
                                 capture_output=True, text=True, timeout=30)
        self.assertEqual(out.stdout.strip(), "False", out.stderr)

    def test_paths_resolve_to_this_project(self):
        self.assertTrue(paths.PROJECT_ROOT.is_absolute())
        self.assertTrue(paths.CONFIG_FILE.exists(), paths.CONFIG_FILE)
        self.assertTrue((paths.PROJECT_ROOT / "pyproject.toml").exists())
        for path in (paths.TRADES_CSV, paths.WATCHLIST_FILE, paths.LOGS_DIR, paths.STOP_FILE):
            self.assertTrue(path.is_relative_to(paths.PROJECT_ROOT), path)

    def test_setup_check_has_no_problems(self):
        problems, _ = check_setup.run_checks()
        self.assertEqual(problems, [])


if __name__ == "__main__":
    unittest.main()
