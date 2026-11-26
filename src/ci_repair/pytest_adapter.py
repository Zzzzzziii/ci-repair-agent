import json
from pathlib import Path
import re
from time import monotonic

from ci_repair.artifacts import pytest_run_directory
from ci_repair.project import prepare_candidate_environment
from ci_repair.runner import OUTPUT_LIMIT, run_process


ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
FAILED_NODE = re.compile(
    r"^[ \t]*(?:\d{4}-\d\d-\d\dT[^\s]+[ \t]+)?FAILED[ \t]+"
    r"(.+?::.+?)(?:[ \t]+-[ \t]+.*)?[ \t]*$",
    re.MULTILINE,
)
PYTEST_EXIT_ERRORS = {2: "interrupted", 3: "pytest_internal_error", 4: "pytest_usage_error"}


def normalized_node(node: str) -> str:
    path, separator, name = node.partition("::")
    return path.replace("\\", "/").removeprefix("./") + separator + name


def failing_nodes(log: str) -> list[str]:
    clean_log = ANSI_ESCAPE.sub("", log)
    return list(dict.fromkeys(
        normalized_node(match) for match in FAILED_NODE.findall(clean_log)
    ))


def test_status(phases: dict) -> str:
    for stage in ("setup", "teardown"):
        if phases.get(stage, {}).get("status") == "failed":
            return "error"
    if any(phase["status"] == "xfailed" for phase in phases.values()):
        return "xfailed"
    if any(phase["status"] == "skipped" for phase in phases.values()):
        return "skipped"
    return phases.get("call", {}).get("status", "not_run")


def read_evidence(result: dict, path: Path, tree: Path) -> None:
    evidence = json.loads(path.read_text(encoding="utf-8"))
    result["collected"] = evidence["collected"]
    result["session_finished"] = evidence["session_finished"]
    result["collection_errors"] = evidence["collection_errors"]
    result["missing_dependencies"] = evidence["missing_dependencies"]
    result["runtime"] = evidence["runtime"]
    tree = tree.resolve()
    imports = {}
    for name, origins in evidence["imports"].items():
        status = "not_observed"
        if origins:
            status = "in_worktree" if all(Path(origin).is_relative_to(tree) for origin in origins) else "outside_worktree"
        imports[name] = {"status": status, "origins": origins}
    result["imports"] = imports
    phases_by_node = evidence["phases"]
    if result["session_finished"]:
        phases_by_node = {**{node: {} for node in result["collected"] or []}, **phases_by_node}
    result["tests"] = {
        node: {"status": test_status(phases), "phases": phases} for node, phases in phases_by_node.items()
    }
    if result["status"] == "timeout":
        return

    statuses = {test["status"] for test in result["tests"].values()}
    exit_code = result["exit_code"]
    if result["missing_dependencies"]:
        result["failure_kind"] = "missing_dependency"
    elif result["collection_errors"]:
        result["failure_kind"] = "collection_error"
    elif exit_code in PYTEST_EXIT_ERRORS:
        result["failure_kind"] = PYTEST_EXIT_ERRORS[exit_code]
    elif "error" in statuses:
        result["failure_kind"] = "fixture_error"
    elif not result["session_finished"]:
        result["failure_kind"] = "session_incomplete"
    elif exit_code == 0:
        if not statuses or "not_run" in statuses:
            result.update(status="incomplete", failure_kind="incomplete")
        else:
            result.update(status="passed", failure_kind=None)
    elif exit_code == 1:
        result.update(status="failed", failure_kind="test_failure")
    elif exit_code == 5:
        result.update(status="no_tests", failure_kind="no_tests")
    else:
        result["failure_kind"] = "process_error"


class PytestAdapter:
    def __init__(
        self, interpreter: Path, scratch: Path, timeout: float, *, task_directory: Path | None = None,
        source_paths: list[str] | None = None, import_modules: list[str] | None = None,
        pytest_args: list[str] | None = None, budget=None, process_runner=run_process,
    ):
        self.interpreter = interpreter
        self.scratch = scratch
        self.timeout = timeout
        self.budget = budget
        self.run_process = process_runner
        self.task_directory = task_directory
        self.source_paths = source_paths or []
        self.import_modules = import_modules or []
        self.pytest_args = pytest_args or []

    def run(self, directory: Path, selectors: list[str], *, tree: Path | None = None) -> dict:
        start = monotonic()
        result = {
            "status": "error",
            "failure_kind": "runner_error",
            "exit_code": None,
            "tests": {},
            "collected": None,
            "session_finished": False,
            "collection_errors": [],
            "missing_dependencies": [],
            "output": "",
            "cwd": str(directory),
        }
        worker = Path(__file__).with_name("pytest_reporter.py").resolve()
        tree = tree or directory
        try:
            env = prepare_candidate_environment(tree, self.source_paths)
            with pytest_run_directory(self.scratch, self.task_directory) as run_directory:
                report = run_directory / "pytest.json"
                stdout = run_directory / "stdout.log"
                stderr = run_directory / "stderr.log"
                command = [
                    str(self.interpreter), "-B", "-X", "utf8", str(worker), str(report),
                    json.dumps({"imports": self.import_modules, "pytest_args": self.pytest_args}), *selectors,
                ]
                result["command"] = command
                timeout = self.timeout
                if self.budget:
                    self.budget.reserve_call("tests", run_directory.name)
                    timeout = min(timeout, self.budget.check_budget())
                try:
                    process = self.run_process(
                        command, cwd=directory, env=env, stdout_path=stdout, stderr_path=stderr, timeout=timeout,
                    )
                finally:
                    if self.task_directory is not None:
                        result["artifacts"] = {
                            name: path.relative_to(self.task_directory).as_posix()
                            for name, path in (("stdout", stdout), ("stderr", stderr), ("pytest_report", report))
                            if path.is_file()
                        }
                result["exit_code"] = process["exit_code"]
                result["output"] = process["output"]
                if process["timed_out"]:
                    result.update(status="timeout", failure_kind="timeout")
                    notice = f"pytest exceeded {timeout:g}s\n"
                    result["output"] = (notice + process["output"][-(OUTPUT_LIMIT - len(notice)):]).rstrip()
                    if process["cleanup_error"]:
                        result["cleanup_error"] = process["cleanup_error"]
                if report.is_file():
                    read_evidence(result, report, tree)
                elif not process["timed_out"]:
                    result["failure_kind"] = "report_unavailable"
        except (OSError, ValueError) as error:
            result["output"] = str(error)
        result["seconds"] = round(monotonic() - start, 3)
        return result
