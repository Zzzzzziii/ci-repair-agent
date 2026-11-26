from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from ci_repair.schemas import ModelCallRecord, RepairFeedback, RunManifest, ToolEvent, WorkflowEvent, WorkflowEventType


MANIFEST_FILENAME = "manifest.json"
EVENTS_FILENAME = "events.jsonl"
MODEL_ARTIFACTS = Path("artifacts") / "model"
TOOL_ARTIFACTS = Path("artifacts") / "tools"
FEEDBACK_ARTIFACTS = Path("artifacts") / "feedback"
WORKTREE_ARTIFACTS = Path("artifacts") / "worktrees"
RETRIEVAL_ARTIFACTS = Path("artifacts") / "retrieval"


def atomic_write(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}-{uuid4().hex}")
    try:
        temporary.write_bytes(content)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def write_json(path: Path, data: Mapping[str, object]) -> None:
    atomic_write(path, json.dumps(dict(data), ensure_ascii=False, indent=2).encode("utf-8"))


def read_report(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version", 1) not in (1, 2):
        raise ValueError("Unsupported repair report version")
    if not isinstance(data.get("task"), dict) or not data["task"].get("id"):
        raise ValueError("Missing task identity in repair report")
    return {"recovery": [], **data, "schema_version": 2}


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
        "project_config": task.get("project_config"),
        "python": task["python"],
        "source_paths": task.get("source_paths", []),
        "import_modules": task.get("import_modules", []),
        "pytest_args": task.get("pytest_args", []),
        "regression": task["regression"],
        "timeout": task["timeout"],
        "targets": task["targets"],
        "model": task["model"],
        "retrieval_mode": task.get("retrieval_mode"),
        "embedding_model": task.get("embedding_model"),
        "input_log": input_log,
        "input_sha256": sha256(log).hexdigest() if log is not None else None,
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


def record_retry_feedback(directory: Path, feedback: list[RepairFeedback]) -> None:
    destination = directory / FEEDBACK_ARTIFACTS / f"after-candidate-{feedback[-1]['candidate']}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    write_json(destination, {"schema_version": 1, "attempts": feedback})


def record_worktree(directory: Path, record: dict) -> None:
    destination = directory / WORKTREE_ARTIFACTS / f"{record['id']}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    write_json(destination, record)


def record_retrieval(directory: Path, record: dict) -> None:
    destination = directory / RETRIEVAL_ARTIFACTS / f"{uuid4().hex}.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    write_json(destination, record)


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


def record_candidate_diffs(directory: Path, candidates: list[dict]) -> list[dict]:
    output = directory / "artifacts" / "candidates"
    output.mkdir(parents=True, exist_ok=True)
    records = []
    for number, candidate in enumerate(candidates, start=1):
        path = output / f"candidate-{number}.diff"
        atomic_write(path, candidate["diff"].encode("utf-8"))
        records.append({**candidate, "candidate": number, "diff_artifact": path.relative_to(directory).as_posix()})
    return records


def render_markdown_report(report: dict) -> str:
    task = report.get("task", {})
    lines = ["# CI 修复报告", ""]

    def field(label, value):
        # Keep repository and model text inside indented code, including embedded newlines.
        text = " ".join(str(value).splitlines())
        lines.extend([f"    {label}  {text}", ""])

    if task.get("id"):
        field("任务", task["id"])
    field("结果", report["status"])
    if task:
        field("被测提交", task.get("commit", "未记录"))
        field("目标", ", ".join(task.get("targets", [])))
        field("回归范围", ", ".join(task.get("regression", [])) or "pytest 默认收集范围")
    if report.get("reason"):
        field("原因", report["reason"])
    for check in report.get("validations", []):
        number = check["candidate"]
        candidate = report["candidates"][number - 1]
        lines.extend([f"## 候选 {number}", ""])
        field("判定", check["status"])
        field("修改理由", candidate.get("hypothesis", ""))
        field("源码证据", ", ".join(candidate.get("evidence", [])) or "未提供")
        if check.get("reason"):
            field("判定原因", check["reason"])
        if run := check.get("regression"):
            field("已收集的回归用例数", len(run.get("tests", {})))
            field("剩余失败", ", ".join(check.get("remaining_failures", [])) or "无记录")
        if check.get("candidate_diff"):
            field("候选补丁", check["candidate_diff"])
    if budget := report.get("budget"):
        lines.extend(["## 预算用量", ""])
        for kind in ("model_calls", "tool_calls", "candidates", "tests"):
            field(kind, f"{len(budget['reservations'].get(kind, {}))} / {budget['limits'][kind]}")
    if report["status"] == "accepted":
        lines.extend(["[下载已验证补丁](fix.diff)", ""])
    lines.extend(["[查看完整证据](report.json)", ""])
    return "\n".join(lines)


def write_report(directory: Path, report: dict) -> None:
    atomic_write(directory / "report.md", render_markdown_report(report).encode("utf-8"))
    write_json(directory / "report.json", report)


def publish(directory: Path, state: dict, next_nodes: tuple, *, status: str, error: str | None = None) -> None:
    report = {**state, "schema_version": 2, "status": status, "next": list(next_nodes)}
    manifest_path = directory / MANIFEST_FILENAME
    if manifest_path.is_file():
        report["manifest"] = MANIFEST_FILENAME
        runtime = state.get("reference", {}).get("target", {}).get("runtime", {})
        if fingerprint := runtime.get("environment_fingerprint"):
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if "environment_fingerprint" not in manifest:
                manifest["environment_fingerprint"] = fingerprint
                write_json(manifest_path, manifest)
    if (directory / EVENTS_FILENAME).is_file():
        report["events"] = EVENTS_FILENAME
    if (directory / "budget.json").is_file():
        report["budget"] = json.loads((directory / "budget.json").read_text(encoding="utf-8"))
        calls = report["budget"]["reservations"].get("model_calls", {})
        report["model_usage"] = [reservation["usage"] or {} for reservation in calls.values()]
        for temporary in ("prompt", "decision", "observations", "applied_diff", "verdict", "related_run", "target_run"):
            report.pop(temporary, None)
    for key, category in (
        ("model_calls", MODEL_ARTIFACTS), ("tool_events", TOOL_ARTIFACTS),
        ("retry_feedback", FEEDBACK_ARTIFACTS), ("worktrees", WORKTREE_ARTIFACTS),
        ("retrieval", RETRIEVAL_ARTIFACTS),
        ("recovery", Path("artifacts/recovery")),
    ):
        paths = sorted(
            path.relative_to(directory).as_posix()
            for path in (directory / category).glob("*.json")
            if path.is_file()
        )
        if paths:
            report[key] = paths
    worktree_issues = []
    for path in report.get("worktrees", []):
        record = json.loads((directory / path).read_text(encoding="utf-8"))
        if record["status"] != "removed":
            worktree_issues.append({**record, "artifact": path})
    if worktree_issues:
        report["worktree_issues"] = worktree_issues
    if candidates := state.get("candidates"):
        candidate_records = record_candidate_diffs(directory, candidates)
        report["candidates"] = candidate_records
        if validations := state.get("validations"):
            report["validations"] = [
                {**validation, "candidate_diff": candidate_records[validation["candidate"] - 1]["diff_artifact"]}
                for validation in validations
            ]
    if error:
        report["reason"] = error
    if status == "accepted":
        accepted = next(item for item in state["validations"] if item["status"] == "accepted")
        atomic_write(directory / "fix.diff", accepted["diff"].encode("utf-8"))
    write_report(directory, report)
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
