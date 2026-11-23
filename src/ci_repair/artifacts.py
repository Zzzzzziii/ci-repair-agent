from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from ci_repair.schemas import ModelCallRecord, RunManifest, ToolEvent, WorkflowEvent, WorkflowEventType


MANIFEST_FILENAME = "manifest.json"
EVENTS_FILENAME = "events.jsonl"
MODEL_ARTIFACTS = Path("artifacts") / "model"
TOOL_ARTIFACTS = Path("artifacts") / "tools"


def atomic_write(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}-{uuid4().hex}")
    try:
        temporary.write_bytes(content)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def write_json(path: Path, data: Mapping[str, object]) -> None:
    atomic_write(path, json.dumps(dict(data), ensure_ascii=False, indent=2).encode("utf-8"))


def record_run_inputs(directory: Path, task: dict, log: bytes | None) -> None:
    input_log = None
    if log is not None:
        log_path = directory / "artifacts" / "input" / "ci.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(log_path, log)
        input_log = log_path.relative_to(directory).as_posix()

    manifest: RunManifest = {
        "schema_version": 1,
        "task_id": task["id"],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "repository": task["repository"],
        "commit": task["commit"],
        "commit_source": task["commit_source"],
        "cwd": task["cwd"],
        "targets": task["targets"],
        "model": task["model"],
        "input_log": input_log,
    }
    write_json(directory / MANIFEST_FILENAME, manifest)


def record_model_call(directory: Path, record: ModelCallRecord) -> None:
    destination = directory / MODEL_ARTIFACTS / f"{record['call_id']}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    write_json(destination, record)


def record_tool_event(directory: Path, event: ToolEvent) -> None:
    destination = directory / TOOL_ARTIFACTS / f"{event['event_id']}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    write_json(destination, event)


def append_workflow_event(directory: Path, task_id: str, kind: WorkflowEventType, **details: object) -> None:
    event: WorkflowEvent = {
        "schema_version": 1,
        "task_id": task_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": kind,
        "details": details,
    }
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / EVENTS_FILENAME
    try:
        previous = path.read_bytes()
    except FileNotFoundError:
        previous = b""
    # The CLI serializes writers per task. Replacing the short timeline keeps every JSON line complete.
    line = (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
    atomic_write(path, previous + line)


def publish(directory: Path, state: dict, next_nodes: tuple, *, status: str, error: str | None = None) -> None:
    report = {**state, "status": status, "next": list(next_nodes)}
    if (directory / MANIFEST_FILENAME).is_file():
        report["manifest"] = MANIFEST_FILENAME
    if (directory / EVENTS_FILENAME).is_file():
        report["events"] = EVENTS_FILENAME
    for key, category in (("model_calls", MODEL_ARTIFACTS), ("tool_events", TOOL_ARTIFACTS)):
        paths = sorted(
            path.relative_to(directory).as_posix()
            for path in (directory / category).glob("*.json")
            if path.is_file()
        )
        if paths:
            report[key] = paths
    if error:
        report["reason"] = error
    if status == "accepted":
        accepted = next(item for item in state["validations"] if item["status"] == "accepted")
        atomic_write(directory / "fix.diff", accepted["diff"].encode("utf-8"))
    write_json(directory / "report.json", report)
    print(f"{status}: {directory / 'report.json'}")


@contextmanager
def pytest_run_directory(scratch: Path, task_directory: Path | None) -> Iterator[Path]:
    if task_directory is None:
        with TemporaryDirectory(dir=scratch) as temporary:
            yield Path(temporary)
        return

    destination = task_directory / "artifacts" / "pytest" / uuid4().hex
    destination.mkdir(parents=True)
    yield destination
