"""Executed by the target project's Python interpreter."""

import json
from pathlib import Path
import sys

class Reporter:
    def __init__(self, destination: Path, modules: list[str]):
        self.destination = destination
        self.imports = {name: set() for name in modules}
        self.collected = None
        self.phases = {}
        self.collection_errors = []
        self.missing_dependencies = []
        self.session_finished = False
        self.pytest_version = None

    def pytest_collection_finish(self, session):
        self.collected = [item.nodeid for item in session.items]
        self.write_evidence()

    def pytest_sessionfinish(self):
        self.session_finished = True

    def pytest_runtest_logreport(self, report):
        self.observe_imports()
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

    def pytest_exception_interact(self, call, report):
        error = call.excinfo.value
        seen = set()
        while error is not None and id(error) not in seen:
            if isinstance(error, ModuleNotFoundError) and error.name:
                self.record_missing_dependency(error.name, report.nodeid, call.when)
                break
            seen.add(id(error))
            error = error.__cause__ or (None if error.__suppress_context__ else error.__context__)

    def record_missing_dependency(self, name: str, node_id: str | None, phase: str):
        self.missing_dependencies.append({"module": name, "node_id": node_id, "phase": phase})

    def observe_imports(self):
        for name, observed in self.imports.items():
            module = sys.modules.get(name)
            origin = getattr(module, "__file__", None)
            paths = (origin,) if origin else getattr(module, "__path__", ())
            observed.update(str(Path(path).resolve()) for path in paths)

    def write_evidence(self):
        self.observe_imports()
        evidence = {
            "collected": self.collected,
            "phases": self.phases,
            "collection_errors": self.collection_errors,
            "missing_dependencies": self.missing_dependencies,
            "session_finished": self.session_finished,
            "imports": {name: sorted(paths) for name, paths in self.imports.items()},
            "runtime": {
                "python": sys.executable,
                "python_version": sys.version.split()[0],
                "pytest_version": self.pytest_version,
            },
        }
        temporary = self.destination.with_suffix(".tmp")
        temporary.write_text(json.dumps(evidence), encoding="utf-8")
        temporary.replace(self.destination)


def main():
    destination = Path(sys.argv[1])
    reporter = Reporter(destination, json.loads(sys.argv[2]))
    try:
        import pytest
    except ModuleNotFoundError as error:
        if error.name:
            reporter.record_missing_dependency(error.name, None, "startup")
        reporter.write_evidence()
        print(str(error), file=sys.stderr)
        return 1
    reporter.pytest_version = pytest.__version__
    sys.path.insert(0, str(Path.cwd()))
    exit_code = pytest.main(
        ["-q", "-p", "no:cacheprovider", "--tb=short", f"--rootdir={Path.cwd()}", "--", *sys.argv[3:]],
        plugins=[reporter],
    )
    reporter.write_evidence()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
