"""Run the offline unit tests and record the results for the dashboard.

    paperbot-test        run tests/, write logs/test_results.json, rebuild the dashboard
    paperbot-test -v     also print one line per test
    (same as python -m paperbot.ops.run_tests)

Every test is recorded with its status (passed / failed / error / skipped /
expected failure / unexpected success), its duration, a description (the
test's docstring, else its name in words; with its module's and class's
one-line docstrings as context) and the warnings it raised:
  python  warnings.warn(...) calls, e.g. DeprecationWarning from a library
  log     WARNING-or-worse log lines, e.g. ALERT. Many are expected: the test
          deliberately drives the code down that path (a missing stop, ...)
Warnings raised while importing the test modules are recorded too. Log lines
checked with assertLogs are the test's own business and are not recorded.

Never connects to IBKR or sends notifications (the tests set PAPERBOT_NOTIFY=off).
Exit code 0 when no test failed or errored (skips are fine), else 1.
"""
import argparse
import datetime as dt
import json
import logging
import os
import platform
import sys
import time
import traceback
import unittest
import warnings
from pathlib import Path
from zoneinfo import ZoneInfo

from paperbot import paths

TESTS_DIR = paths.PROJECT_ROOT / "tests"
RESULTS_FILE = paths.LOGS_DIR / "test_results.json"
ET = ZoneInfo("America/New_York")
FAILED_STATUSES = ("failed", "error", "unexpected success")


def _first_line(doc: str | None) -> str:
    return (doc or "").strip().splitlines()[0].strip() if (doc or "").strip() else ""


def _words(method_name: str) -> str:
    words = method_name.removeprefix("test_").replace("_", " ").strip()
    return words[:1].upper() + words[1:]


def _python_warning(w: warnings.WarningMessage) -> dict:
    return {"kind": "python", "category": w.category.__name__, "message": str(w.message),
            "location": f"{Path(w.filename).name}:{w.lineno}"}


class _LogCollector(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


class RecordingResult(unittest.TextTestResult):
    """TextTestResult that also keeps a structured record of every test."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records: list[dict] = []
        self._current: dict | None = None

    def _record_for(self, test) -> dict:
        if self._current is not None and self._current["id"] == test.id():
            return self._current
        record = self._new_record(test)  # setUpClass / module errors have no startTest
        self.records.append(record)
        return record

    @staticmethod
    def _new_record(test) -> dict:
        module_name = type(test).__module__
        module = sys.modules.get(module_name)
        method = getattr(test, "_testMethodName", None)
        is_case = isinstance(test, unittest.TestCase) and method is not None
        return {
            "id": test.id(),
            "module": module_name,
            "module_doc": _first_line(getattr(module, "__doc__", "")),
            "class": type(test).__name__ if is_case else "",
            "class_doc": _first_line(type(test).__doc__) if is_case else "",
            "name": method or str(test),
            "description": (test.shortDescription() if is_case else None)
                           or (_words(method) if is_case else str(test)),
            "status": "passed", "detail": "", "duration_s": 0.0, "warnings": [],
        }

    def _set(self, test, status: str, detail: str = ""):
        record = self._record_for(test)
        if record["status"] in FAILED_STATUSES and status == "passed":
            return  # a failed subTest keeps the test failed
        record["status"], record["detail"] = status, detail

    @staticmethod
    def _short_trace(err) -> str:
        """'file.py:line: ErrorType: message' for the innermost frame."""
        frames = traceback.extract_tb(err[2])
        where = f"{Path(frames[-1].filename).name}:{frames[-1].lineno}: " if frames else ""
        return where + "".join(traceback.format_exception_only(err[0], err[1])).strip()

    def startTest(self, test):
        super().startTest(test)
        self._current = self._new_record(test)
        self.records.append(self._current)
        self._started = time.perf_counter()
        self._warn_ctx = warnings.catch_warnings(record=True)
        self._caught = self._warn_ctx.__enter__()
        warnings.simplefilter("always")
        self._logs = _LogCollector()
        logging.getLogger().addHandler(self._logs)

    def stopTest(self, test):
        logging.getLogger().removeHandler(self._logs)
        self._warn_ctx.__exit__(None, None, None)
        record = self._current
        record["duration_s"] = round(time.perf_counter() - self._started, 3)
        seen = set()
        for item in ([_python_warning(w) for w in self._caught]
                     + [{"kind": "log", "category": r.levelname, "message": r.getMessage(),
                         "location": r.name} for r in self._logs.records]):
            key = tuple(item.values())
            if key not in seen:
                seen.add(key)
                record["warnings"].append(item)
        self._current = None
        super().stopTest(test)

    def addSuccess(self, test):
        super().addSuccess(test)
        self._set(test, "passed")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._set(test, "failed", self._short_trace(err))

    def addError(self, test, err):
        super().addError(test, err)
        self._set(test, "error", self._short_trace(err))

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._set(test, "skipped", reason)

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self._set(test, "expected failure", self._short_trace(err))

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self._set(test, "unexpected success", "marked expectedFailure but passed")

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            failed = issubclass(err[0], test.failureException)
            self._set(test, "failed" if failed else "error",
                      f"{subtest._subDescription()}: {self._short_trace(err)}")


def run_suite(tests_dir: Path = TESTS_DIR, verbosity: int = 1, stream=None) -> dict:
    """Discover and run the tests; return the JSON-ready results."""
    started = time.perf_counter()
    with warnings.catch_warnings(record=True) as import_caught:
        warnings.simplefilter("always")
        suite = unittest.defaultTestLoader.discover(str(tests_dir),
                                                    top_level_dir=str(tests_dir))
    runner = unittest.TextTestRunner(stream=stream or sys.stderr, verbosity=verbosity,
                                     resultclass=RecordingResult)
    result = runner.run(suite)
    records = result.records

    counts = {status: sum(1 for r in records if r["status"] == status)
              for status in ("passed", "failed", "error", "skipped",
                             "expected failure", "unexpected success")}
    import_warnings = [_python_warning(w) for w in import_caught]
    test_warnings = [w for r in records for w in r["warnings"]]
    return {
        "time": dt.datetime.now(ET).isoformat(timespec="seconds"),
        "duration_s": round(time.perf_counter() - started, 2),
        "python": platform.python_version(),
        "status": "failed" if any(counts[s] for s in FAILED_STATUSES) else "passed",
        "summary": {
            "total": len(records), **{k.replace(" ", "_"): v for k, v in counts.items()},
            "python_warnings": sum(1 for w in test_warnings if w["kind"] == "python")
                               + len(import_warnings),
            "log_warnings": sum(1 for w in test_warnings if w["kind"] == "log"),
            "tests_with_warnings": sum(1 for r in records if r["warnings"]),
        },
        "import_warnings": import_warnings,
        "tests": records,
    }


def save_results(results: dict, out: Path = RESULTS_FILE) -> Path:
    os.makedirs(out.parent, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(json.dumps(results, indent=2), encoding="utf-8")
    os.replace(tmp, out)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the unit tests and record the results.")
    parser.add_argument("-v", "--verbose", action="store_true", help="one line per test")
    parser.add_argument("--no-html", action="store_true", help="do not rebuild the dashboard")
    args = parser.parse_args(argv)

    results = run_suite(verbosity=2 if args.verbose else 1)
    out = save_results(results)
    s = results["summary"]
    print(f"TESTS {results['status'].upper()}: {s['passed']} passed, {s['failed']} failed, "
          f"{s['error']} errors, {s['skipped']} skipped of {s['total']} in "
          f"{results['duration_s']}s; {s['python_warnings']} Python warnings, "
          f"{s['log_warnings']} log warnings. Results: {out}")
    if not args.no_html:
        try:
            from paperbot.reporting.compute_perf import refresh_dashboard  # after the tests
            print(f"Dashboard updated: {refresh_dashboard()}")
        except Exception as e:  # noqa: BLE001 - the results file is already written
            print(f"dashboard not written: {e!r}", file=sys.stderr)
    return 1 if results["status"] == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
