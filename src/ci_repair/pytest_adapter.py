import json
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory
from time import monotonic


ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
FAILED_NODE = re.compile(
    r"^[ \t]*(?:\d{4}-\d\d-\d\dT[^\s]+[ \t]+)?FAILED[ \t]+"
    r"(.+?::.+?)(?:[ \t]+-[ \t]+.*)?[ \t]*$",
    re.MULTILINE,
)


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


class PytestAdapter:
    def __init__(self, interpreter: Path, scratch: Path, timeout: float):
        self.interpreter = interpreter
        self.scratch = scratch
        self.timeout = timeout

    def run(self, directory: Path, selectors: list[str]) -> dict:
        start = monotonic()
        result = {
            "status": "error",
            "exit_code": None,
            "tests": {},
            "collection_errors": [],
            "output": "",
        }
        worker = Path(__file__).with_name("pytest_reporter.py").resolve()
        try:
            with TemporaryDirectory(dir=self.scratch) as temporary:
                report = Path(temporary) / "pytest.json"
                process = subprocess.run(
                    [str(self.interpreter), "-B", "-X", "utf8", str(worker), str(report), *selectors],
                    cwd=directory,
                    capture_output=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=self.timeout,
                )
                result["exit_code"] = process.returncode
                result["output"] = (process.stdout + process.stderr)[-12000:]
                if report.is_file():
                    evidence = json.loads(report.read_text(encoding="utf-8"))
                    result["collection_errors"] = evidence["collection_errors"]
                    result["tests"] = {
                        node: {"status": test_status(phases), "phases": phases}
                        for node, phases in evidence["phases"].items()
                    }
                    has_error = evidence["collection_errors"] or any(
                        test["status"] == "error" for test in result["tests"].values()
                    )
                    if not has_error:
                        result["status"] = {0: "passed", 1: "failed", 5: "no_tests"}.get(
                            process.returncode, "error"
                        )
        except subprocess.TimeoutExpired:
            result.update(status="timeout", output=f"pytest exceeded {self.timeout:g}s")
        except (OSError, ValueError) as error:
            result["output"] = str(error)
        result["seconds"] = round(monotonic() - start, 3)
        return result
