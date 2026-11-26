import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from uuid import UUID, uuid4

from langgraph.checkpoint.sqlite import SqliteSaver

from ci_repair.artifacts import append_workflow_event, publish, record_run_inputs
from ci_repair.project import check_project, resolve_project_profile, validate_selector
from ci_repair.pytest_adapter import failing_nodes, normalized_node
from ci_repair.workflow import build_workflow
from ci_repair.workspace import commit_id, repository_key


def add_project_arguments(command: argparse.ArgumentParser) -> None:
    command.add_argument("--repo", type=Path, required=True)
    command.add_argument("--config", type=Path, help="Project TOML; defaults to .ci-repair.toml in the repository")
    command.add_argument("--cwd", help="Target project directory relative to the repository")
    command.add_argument("--python", help="Python interpreter with target dependencies")
    command.add_argument("--source-path", action="append", help="Source directory relative to the repository; repeatable")
    command.add_argument("--pytest-arg", action="append", help="Pytest argument; repeatable, e.g. --pytest-arg=-x")
    command.add_argument("--regression", action="append", help="Regression path or node ID relative to --cwd; repeatable")
    command.add_argument("--timeout", type=float, help="Timeout per pytest invocation in seconds")


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description="Reproduce, repair, and verify pytest failures")
    command.add_argument("--state-dir", type=Path, default=Path.home() / ".ci-repair")
    subcommands = command.add_subparsers(dest="command", required=True)
    fix = subcommands.add_parser("fix", help="Start a repair task")
    add_project_arguments(fix)
    fix.add_argument("--import", dest="imports", action="append", help="Verify a project module's runtime origin; repeatable")
    fix.add_argument("--log", help="CI log path, or - for stdin")
    fix.add_argument("--commit", help="Commit tested by CI; defaults to local HEAD")
    fix.add_argument("--test", action="append", default=[], help="Exact failed pytest node ID; repeatable")
    fix.add_argument("--model", default=os.environ.get("CI_REPAIR_MODEL"), help="LangChain provider:model identifier")
    fix.add_argument("--strategy", choices=["batch", "sequential"], help="Defaults to batch, or sequential for --seed-patch")
    fix.add_argument("--no-memory", action="store_true")
    patches = fix.add_mutually_exclusive_group()
    patches.add_argument("--patch", type=Path, action="append", default=[], help="Verify a supplied diff without a model")
    patches.add_argument("--seed-patch", type=Path, help="Verify this first diff before a sequential model retry")
    fix.add_argument("--pause-after-candidate", action="store_true", help="Checkpoint after verifying one candidate")
    doctor = subcommands.add_parser("doctor", help="Check a project's Python environment and test plan")
    add_project_arguments(doctor)
    doctor.add_argument("--import", dest="imports", action="append", help="Module to locate; repeatable")
    for name in ("status", "resume"):
        subcommands.add_parser(name).add_argument("task_id", type=UUID)
    return command


def read_ci_log(path: str | None) -> bytes | None:
    if not path:
        return None
    if path == "-":
        return sys.stdin.read().encode("utf-8", errors="surrogateescape")
    return Path(path).read_bytes()


def new_state(args, log: str) -> dict:
    profile = project_profile(args)
    repository = Path(profile["repository"])
    targets = list(dict.fromkeys(normalized_node(node) for node in (args.test or failing_nodes(log))))
    log_commit = re.search(r"^(?:GITHUB_SHA=|commit:[ \t]*)([0-9a-fA-F]{40})[ \t]*$", log, re.MULTILINE)
    ref = args.commit or (log_commit[1] if log_commit else "HEAD")
    for node in targets:
        validate_selector(repository / profile["cwd"], node)
    if not targets or any("::" not in node for node in targets):
        raise ValueError("No exact failed pytest node IDs found; supply --test path/to/test.py::test_name")
    if len(args.patch) > 2:
        raise ValueError("Supply at most two candidate patches")
    if not args.patch and not args.model:
        raise ValueError("Set CI_REPAIR_MODEL or pass --model provider:model; --patch verifies offline")
    strategy = args.strategy or ("sequential" if args.seed_patch else "batch")
    if args.seed_patch and strategy != "sequential":
        raise ValueError("--seed-patch requires sequential repair")
    initial_patches = [args.seed_patch] if args.seed_patch else args.patch
    return {
        "task": {
            **profile,
            "id": str(uuid4()),
            "repository_key": repository_key(repository),
            "commit": commit_id(repository, ref),
            "commit_source": "argument" if args.commit else "CI log" if log_commit else "local HEAD",
            "targets": targets,
            "model": args.model if not args.patch else None,
            "strategy": strategy,
            "use_memory": not args.no_memory,
        },
        "candidates": [
            {"hypothesis": f"Supplied patch {patch.name}", "evidence": [],
             "diff": patch.read_text(encoding="utf-8-sig")}
            for patch in initial_patches
        ],
        "status": "running",
    }


def project_profile(args):
    return resolve_project_profile(
        args.repo, args.config, cwd=args.cwd, python=args.python, source_paths=args.source_path,
        import_modules=args.imports, pytest_args=args.pytest_arg, regression=args.regression, timeout=args.timeout,
    )


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    storage = args.state_dir.resolve()
    try:
        if args.command == "doctor":
            diagnosis = check_project(project_profile(args))
            print(json.dumps(diagnosis, ensure_ascii=False, indent=2))
            return 0 if diagnosis["status"] == "ready" else 2
        initial = None
        input_log = None
        if args.command == "fix":
            input_log = read_ci_log(args.log)
            log = (input_log or b"").decode("utf-8-sig", errors="replace")
            initial = new_state(args, log.replace("\r\n", "\n").replace("\r", "\n"))
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
                if initial is not None:
                    record_run_inputs(directory, initial["task"], input_log)
                error = None
                should_run = initial is not None or bool(snapshot.next)
                if should_run:
                    append_workflow_event(
                        directory, task_id, "task_started" if initial is not None else "task_resumed",
                    )
                    try:
                        graph.invoke(initial, config, durability="sync")
                    except Exception as failure:
                        error = f"{type(failure).__name__}: {str(failure)[:1000]}"
                snapshot = graph.get_state(config)
                state = snapshot.values or initial
                if error:
                    status, ending = "interrupted", "task_interrupted"
                elif snapshot.next:
                    status, ending = "paused", "task_paused"
                else:
                    status, ending = state["status"], "task_finished"
                if should_run:
                    append_workflow_event(directory, task_id, ending, status=status, next=list(snapshot.next))
                publish(directory, state, snapshot.next, status=status, error=error)
            return {"accepted": 0, "paused": 0, "rejected": 1}.get(status, 2)
    except (ValueError, OSError, sqlite3.Error, subprocess.TimeoutExpired) as error:
        print(f"ci-repair: {error}", file=sys.stderr)
        return 2
