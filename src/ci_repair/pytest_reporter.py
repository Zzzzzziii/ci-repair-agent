"""Executed by the target project's Python interpreter."""

import json
from pathlib import Path
import sys

import pytest


class Reporter:
    def __init__(self):
        self.phases = {}
        self.collection_errors = []

    def pytest_runtest_logreport(self, report):
        outcome = report.outcome
        if hasattr(report, "wasxfail"):
            outcome = "xpassed" if report.passed else "xfailed"
        self.phases.setdefault(report.nodeid, {})[report.when] = {
            "status": outcome,
            "seconds": report.duration,
            "detail": report.longreprtext if not report.passed else "",
        }

    def pytest_collectreport(self, report):
        if report.failed:
            self.collection_errors.append(report.longreprtext)


def main():
    destination = Path(sys.argv[1])
    reporter = Reporter()
    sys.path.insert(0, str(Path.cwd()))
    exit_code = pytest.main(
        ["-q", "-p", "no:cacheprovider", "--tb=short", f"--rootdir={Path.cwd()}", "--", *sys.argv[2:]],
        plugins=[reporter],
    )
    destination.write_text(
        json.dumps({"phases": reporter.phases, "collection_errors": reporter.collection_errors}),
        encoding="utf-8",
    )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
