from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from ci_repair.schemas import ModelCallRecord, RunManifest


MANIFEST_FILENAME = "manifest.json"
MODEL_ARTIFACTS = Path("artifacts") / "model"


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


def publish(directory: Path, state: dict, next_nodes: tuple, error: str | None = None) -> str:
    status = "interrupted" if error else "paused" if next_nodes else state["status"]
    report = {**state, "status": status, "next": list(next_nodes)}
    if (directory / MANIFEST_FILENAME).is_file():
        report["manifest"] = MANIFEST_FILENAME
    model_calls = sorted(
        path.relative_to(directory).as_posix()
        for path in (directory / MODEL_ARTIFACTS).glob("*.json")
        if path.is_file()
    )
    if model_calls:
        report["model_calls"] = model_calls
    if error:
        report["reason"] = error
    if status == "accepted":
        accepted = next(item for item in state["validations"] if item["status"] == "accepted")
        atomic_write(directory / "fix.diff", accepted["diff"].encode("utf-8"))
    write_json(directory / "report.json", report)
    print(f"{status}: {directory / 'report.json'}")
    return status


@contextmanager
def pytest_run_directory(scratch: Path, task_directory: Path | None) -> Iterator[Path]:
    if task_directory is None:
        with TemporaryDirectory(dir=scratch) as temporary:
            yield Path(temporary)
        return

    destination = task_directory / "artifacts" / "pytest" / uuid4().hex
    destination.mkdir(parents=True)
    yield destination
