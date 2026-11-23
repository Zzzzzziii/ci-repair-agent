import argparse
from contextlib import closing
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
from uuid import UUID, uuid4

from langgraph.checkpoint.sqlite import SqliteSaver

from ci_repair.pytest_adapter import failing_nodes, normalized_node
from ci_repair.workflow import build_workflow
from ci_repair.workspace import commit_id, repository_key, repository_path, within


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description="Reproduce, repair, and verify pytest failures")
    command.add_argument("--state-dir", type=Path, default=Path.home() / ".ci-repair")
    subcommands = command.add_subparsers(dest="command", required=True)
    fix = subcommands.add_parser("fix", help="Start a repair task")
    fix.add_argument("--repo", type=Path, required=True)
    fix.add_argument("--log", help="CI log path, or - for stdin")
    fix.add_argument("--commit", help="Commit tested by CI; defaults to local HEAD")
    fix.add_argument("--cwd", default=".", help="Target project directory relative to the repository")
    fix.add_argument("--python", default=sys.executable, help="Python interpreter with target dependencies")
    fix.add_argument("--test", action="append", default=[], help="Exact failed pytest node ID; repeatable")
    fix.add_argument("--regression", action="append", default=[], help="Regression path or node ID; repeatable")
    fix.add_argument("--timeout", type=float, default=120, help="Timeout per pytest invocation in seconds")
    fix.add_argument("--model", default=os.environ.get("CI_REPAIR_MODEL"), help="LangChain provider:model identifier")
    fix.add_argument("--strategy", choices=["batch", "sequential"], default="batch")
    fix.add_argument("--no-memory", action="store_true")
    fix.add_argument("--patch", type=Path, action="append", default=[], help="Verify a supplied diff without a model")
    fix.add_argument("--pause-after-candidate", action="store_true", help="Checkpoint after verifying one candidate")
    for name in ("status", "resume"):
        subcommands.add_parser(name).add_argument("task_id", type=UUID)
    return command


def new_state(args) -> dict:
    repository = repository_path(args.repo)
    directory = within(repository, args.cwd).relative_to(repository).as_posix()
    log = ""
    if args.log:
        log = sys.stdin.read() if args.log == "-" else Path(args.log).read_text(encoding="utf-8-sig", errors="replace")
    targets = list(dict.fromkeys(normalized_node(node) for node in (args.test or failing_nodes(log))))
    log_commit = re.search(r"^(?:GITHUB_SHA=|commit:[ \t]*)([0-9a-fA-F]{40})[ \t]*$", log, re.MULTILINE)
    ref = args.commit or (log_commit[1] if log_commit else "HEAD")
    for node in [*targets, *args.regression]:
        path = node.partition("::")[0]
        if not path or path.startswith("-") or Path(path).is_absolute() or Path(path).drive:
            raise ValueError(f"Expected a pytest selector relative to --cwd: {node}")
        within(repository / directory, path)
    if not targets or any("::" not in node for node in targets):
        raise ValueError("No exact failed pytest node IDs found; supply --test path/to/test.py::test_name")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        raise ValueError("--timeout must be a positive finite number")
    if len(args.patch) > 2:
        raise ValueError("Supply at most two candidate patches")
    if not args.patch and not args.model:
        raise ValueError("Set CI_REPAIR_MODEL or pass --model provider:model; --patch verifies offline")
    python = shutil.which(args.python)
    if python is None:
        raise ValueError(f"Python interpreter not found: {args.python}")
    return {
        "task": {
            "id": str(uuid4()),
            "repository": str(repository),
            "repository_key": repository_key(repository),
            "commit": commit_id(repository, ref),
            "commit_source": "argument" if args.commit else "CI log" if log_commit else "local HEAD",
            "cwd": directory,
            "python": str(Path(python).absolute()),
            "targets": targets,
            "regression": args.regression,
            "timeout": args.timeout,
            "model": args.model if not args.patch else None,
            "strategy": args.strategy,
            "use_memory": not args.no_memory,
        },
        "candidates": [
            {"hypothesis": f"Supplied patch {patch.name}", "evidence": [],
             "diff": patch.read_text(encoding="utf-8-sig")}
            for patch in args.patch
        ],
        "status": "running",
    }


def atomic_write(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}-{uuid4().hex}")
    try:
        temporary.write_text(content, encoding="utf-8", newline="\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def publish(directory: Path, state: dict, next_nodes: tuple, error: str | None = None) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    status = "interrupted" if error else "paused" if next_nodes else state["status"]
    report = {**state, "status": status, "next": list(next_nodes)}
    if error:
        report["reason"] = error
    if status == "accepted":
        accepted = next(item for item in state["validations"] if item["status"] == "accepted")
        atomic_write(directory / "fix.diff", accepted["diff"])
    atomic_write(directory / "report.json", json.dumps(report, ensure_ascii=False, indent=2))
    print(f"{status}: {directory / 'report.json'}")
    return status


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    storage = args.state_dir.resolve()
    try:
        initial = new_state(args) if args.command == "fix" else None
        task_id = initial["task"]["id"] if initial else str(args.task_id)
        directory = storage / "runs" / task_id
        database = storage / "checkpoints.sqlite"
        if initial is None and not database.is_file():
            raise ValueError(f"Unknown task: {task_id}")
        storage.mkdir(parents=True, exist_ok=True)
        config = {"configurable": {"thread_id": task_id}}
        with SqliteSaver.from_conn_string(str(database)) as saver:
            graph = build_workflow(saver, storage, pause_after_candidate=getattr(args, "pause_after_candidate", False))
            snapshot = graph.get_state(config)
            if initial is None and not snapshot.values:
                raise ValueError(f"Unknown task: {task_id}")
            if args.command == "status":
                print(json.dumps({
                    "task_id": task_id,
                    "status": "paused" if snapshot.next else snapshot.values["status"],
                    "next": list(snapshot.next),
                    "report": str(directory / "report.json"),
                }))
                return 0

            directory.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(directory / "lock.sqlite", timeout=0)) as lock:
                try:
                    lock.execute("BEGIN IMMEDIATE")
                except sqlite3.OperationalError as failure:
                    if failure.sqlite_errorcode == sqlite3.SQLITE_BUSY:
                        raise ValueError(f"Task is already running: {task_id}") from failure
                    raise
                snapshot = graph.get_state(config)
                print(f"Task ID: {task_id}", flush=True)
                error = None
                if initial is not None or snapshot.next:
                    try:
                        graph.invoke(initial, config, durability="sync")
                    except Exception as failure:
                        error = f"{type(failure).__name__}: {str(failure)[:1000]}"
                snapshot = graph.get_state(config)
                status = publish(directory, snapshot.values or initial, snapshot.next, error)
            return {"accepted": 0, "paused": 0, "rejected": 1}.get(status, 2)
    except (ValueError, OSError, sqlite3.Error, subprocess.TimeoutExpired) as error:
        print(f"ci-repair: {error}", file=sys.stderr)
        return 2
