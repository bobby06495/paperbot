"""Trend Join Long must behave exactly as before the strategy-engine refactor.

fixtures/tjl_golden.json was recorded from the pre-refactor code (2026-10-20) with
the configuration pinned in fixtures/. This test replays the same scenarios
(tjl_golden_scenarios.py) through the current code and requires identical
filter reasons, sizing, order requests and position state.

Run: python -m unittest discover -s tests -p test_tjl_golden.py -v
"""
import os

os.environ["PAPERBOT_NOTIFY"] = "off"  # tests must never send real notifications

import json
import logging
import unittest

import tjl_golden_scenarios as golden

# The same values as the legacy fixtures the golden record was made with, in the
# refactored rules.json layout (strategies.trend_join_long built from blocks).
RULES = golden.FIXTURES / "tjl_rules.json"
CONFIG = golden.FIXTURES / "tjl_config.json"


class TrendJoinLongGoldenTest(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        logging.getLogger("paperbot").addHandler(logging.NullHandler())
        cls.expected = json.loads(golden.GOLDEN.read_text())
        cls.actual = json.loads(json.dumps(golden.run_all(RULES, CONFIG)))

    def compare(self, section):
        self.assertEqual(set(self.actual[section]), set(self.expected[section]))
        for name, expected in self.expected[section].items():
            with self.subTest(scenario=name):
                self.assertEqual(self.actual[section][name], expected)

    def test_filter_reasons_identical(self):
        self.compare("evaluate")

    def test_order_requests_and_state_identical(self):
        self.compare("decide")

    def test_sizing_identical(self):
        self.compare("sizing")

    def test_check_symbol_identical(self):
        self.compare("check_symbol")


if __name__ == "__main__":
    unittest.main()
