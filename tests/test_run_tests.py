"""Offline tests for run_tests.py (paperbot-test): statuses, descriptions and warnings.

Run: python -m unittest discover -s tests -p test_run_tests.py -v
"""
import os

os.environ["PAPERBOT_NOTIFY"] = "off"  # tests must never send real notifications

import io
import json
import logging
import tempfile
import unittest
import warnings
from pathlib import Path

from paperbot.ops import run_tests

SAMPLE = '''"""Sample module for the recorder."""
import logging
import unittest
import warnings


class Sample(unittest.TestCase):
    """Sample class doc."""

    def test_passes_quietly(self):
        """Adds one and one."""
        self.assertEqual(1 + 1, 2)

    def test_fails(self):
        self.assertEqual(1, 2)

    def test_errors(self):
        raise RuntimeError("boom")

    @unittest.skip("not today")
    def test_skipped(self):
        pass

    def test_warns_twice(self):
        for _ in range(2):
            warnings.warn("old api", DeprecationWarning)
        logging.getLogger("paperbot").warning("ALERT no stop")
        logging.getLogger("paperbot").info("just info")

    def test_assert_logs_not_recorded(self):
        with self.assertLogs("paperbot", "WARNING"):
            logging.getLogger("paperbot").warning("checked by the test")

    def test_subtest_failure(self):
        for i in range(2):
            with self.subTest(i=i):
                self.assertEqual(i, 0)
'''


class RecorderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        folder = Path(cls.tmp.name)
        (folder / "test_sample_recorder.py").write_text(SAMPLE)
        handlers_before = list(logging.getLogger().handlers)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            cls.results = run_tests.run_suite(folder, verbosity=0, stream=io.StringIO())
        cls.handlers_left = [h for h in logging.getLogger().handlers if h not in handlers_before]
        cls.by_name = {t["name"]: t for t in cls.results["tests"]}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_statuses_and_summary(self):
        status = {name: t["status"] for name, t in self.by_name.items()}
        self.assertEqual(status["test_passes_quietly"], "passed")
        self.assertEqual(status["test_fails"], "failed")
        self.assertEqual(status["test_errors"], "error")
        self.assertEqual(status["test_skipped"], "skipped")
        self.assertEqual(status["test_subtest_failure"], "failed")
        self.assertEqual(self.results["status"], "failed")
        s = self.results["summary"]
        self.assertEqual((s["total"], s["passed"], s["failed"], s["error"], s["skipped"]),
                         (7, 3, 2, 1, 1))
        self.assertRegex(self.by_name["test_errors"]["detail"],
                         r"^test_sample_recorder\.py:\d+: RuntimeError: boom$")
        self.assertEqual(self.by_name["test_skipped"]["detail"], "not today")

    def test_descriptions(self):
        quiet = self.by_name["test_passes_quietly"]
        self.assertEqual(quiet["description"], "Adds one and one.")  # docstring wins
        self.assertEqual(self.by_name["test_fails"]["description"], "Fails")  # name in words
        self.assertEqual((quiet["module_doc"], quiet["class_doc"]),
                         ("Sample module for the recorder.", "Sample class doc."))

    def test_warnings_deduplicated_and_assert_logs_ignored(self):
        warned = self.by_name["test_warns_twice"]["warnings"]
        self.assertEqual([(w["kind"], w["category"], w["message"]) for w in warned],
                         [("python", "DeprecationWarning", "old api"),
                          ("log", "WARNING", "ALERT no stop")])
        self.assertEqual(self.by_name["test_assert_logs_not_recorded"]["warnings"], [])
        self.assertEqual(self.by_name["test_passes_quietly"]["warnings"], [])
        s = self.results["summary"]
        self.assertEqual((s["python_warnings"], s["log_warnings"], s["tests_with_warnings"]),
                         (1, 1, 1))

    def test_no_handler_left_behind(self):
        self.assertEqual(self.handlers_left, [])

    def test_save_is_atomic_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = run_tests.save_results(self.results, Path(tmp) / "logs" / "test_results.json")
            self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["summary"]["total"], 7)
            self.assertEqual([p.name for p in out.parent.iterdir()], ["test_results.json"])


if __name__ == "__main__":
    unittest.main()
