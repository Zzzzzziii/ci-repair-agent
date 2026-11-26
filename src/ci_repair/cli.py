import argparse
from hashlib import sha256
from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from langgraph.checkpoint.sqlite import SqliteSaver

from ci_repair.artifacts import append_workflow_event, publish, read_report, record_run_inputs, write_json, write_report
from ci_repair.project import check_project, resolve_project_profile, validate_selector, repository_id, load_project_config
from ci_repair.memory import RepairMemory
from ci_repair.recovery import verify_checkpoint, record_resume
from ci_repair.budget import Budget, DEFAULTS
from ci_repair.sequential import identify
from ci_repair.pytest_adapter import PytestAdapter, failing_nodes, normalized_node
from ci_repair.workflow import build_workflow
from ci_repair.workspace import commit_id, git, repository_path


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
    command.add_argument("--result-file", type=Path, help="Write the task ID and report paths as JSON for CI")
    subcommands = command.add_subparsers(dest="command", required=True)
    fix = subcommands.add_parser("fix", help="Start a repair task")
    add_project_arguments(fix)
    fix.add_argument("--import", dest="imports", action="append", help="Verify a project module's runtime origin; repeatable")
    fix.add_argument("--log", help="CI log path, or - for stdin")
    fix.add_argument("--commit", help="Commit tested by CI; defaults to local HEAD")
    fix.add_argument("--test", action="append", default=[], help="Exact failed pytest node ID; repeatable")
    fix.add_argument("--model", default=os.environ.get("CI_REPAIR_MODEL"), help="LangChain provider:model identifier")
    fix.add_argument("--retrieval", choices=["none", "literal", "bm25", "hybrid"], default="bm25",
                     help="Source retrieval strategy for model repair")
    fix.add_argument("--embedding-model", default=os.environ.get("CI_REPAIR_EMBEDDING_MODEL"),
                     help="LangChain provider:model identifier for explicit hybrid retrieval")
    fix.add_argument("--strategy", choices=["batch", "sequential"], help="Defaults to batch, or sequential for --seed-patch")
    fix.add_argument("--no-memory", action="store_true")
    fix.add_argument("--no-retry-feedback", action="store_true", help="Omit previous test feedback from sequential model prompts")
    for kind in DEFAULTS:
        fix.add_argument("--max-" + kind.replace("_", "-"), dest="max_" + kind, type=int,
                         help=f"Sequential repair budget for {kind} (default {DEFAULTS[kind]})")
    patches = fix.add_mutually_exclusive_group()
    patches.add_argument("--patch", type=Path, action="append", default=[], help="Verify a supplied diff without a model")
    patches.add_argument("--seed-patch", type=Path, help="Verify this first diff before a sequential model retry")
    fix.add_argument("--pause-after-candidate", action="store_true", help="Checkpoint after verifying one candidate")
    fix.add_argument("--reuse-task", action="store_true", help="Resume or reuse the same CI repair in this state directory")
    doctor = subcommands.add_parser("doctor", help="Check a project's Python environment and test plan")
    add_project_arguments(doctor)
    doctor.add_argument("--import", dest="imports", action="append", help="Module to locate; repeatable")
    for name in ("status", "resume"):
        sub = subcommands.add_parser(name)
        sub.add_argument("task_id", type=UUID)
        if name == "resume":
            sub.add_argument("--repo", type=Path, help="New location of the same repository")
            sub.add_argument("--config", type=Path, help="Project config at its current location")
    memory = subcommands.add_parser("memory", help="Inspect or manage stored repair experiences")
    memory.add_argument("action", choices=["list", "show", "disable", "delete"])
    memory.add_argument("experience_id", nargs="?")
    memory.add_argument("--repo", type=Path, required=True)
    memory.add_argument("--config", type=Path)
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
    markers = set(re.findall(r"^(?:GITHUB_SHA=|commit:[ \t]*)([0-9a-fA-F]{40})[ \t]*$", log, re.MULTILINE))
    if len({s.lower() for s in markers}) > 1:
        raise ValueError("Conflicting tested commits in the CI log")
    log_commit = next(iter(markers), None)
    ref = args.commit or log_commit or "HEAD"
    for node in targets:
        validate_selector(repository / profile["cwd"], node)
    if not targets or any("::" not in node for node in targets):
        raise ValueError("No exact failed pytest node IDs found; supply --test path/to/test.py::test_name")
    if len(args.patch) > 2:
        raise ValueError("Supply at most two candidate patches")
    if not args.patch and not args.model:
        raise ValueError("Set CI_REPAIR_MODEL or pass --model provider:model; --patch verifies offline")
    embedding_model = args.embedding_model or profile.get("embedding_model")
    if not args.patch and args.retrieval == "hybrid" and not embedding_model:
        raise ValueError("Hybrid retrieval requires --embedding-model provider:model")
    strategy = args.strategy or ("sequential" if args.seed_patch else "batch")
    if (args.no_retry_feedback or args.retrieval == "none") and (strategy != "sequential" or args.patch):
        raise ValueError("Tool-only retrieval and feedback ablations require model-driven sequential repair")
    if args.seed_patch and strategy != "sequential":
        raise ValueError("--seed-patch requires sequential repair")
    initial_patches = [args.seed_patch] if args.seed_patch else args.patch
    limits = {kind: getattr(args, "max_" + kind) for kind in DEFAULTS}
    if any(value is not None for value in limits.values()) and (strategy != "sequential" or args.patch):
        raise ValueError("Repair budgets require model-driven sequential repair")
    if any(value is not None and value < 0 for value in limits.values()):
        raise ValueError("Repair budgets must be nonnegative")
    commit = commit_id(repository, ref)
    if log_commit and log_commit.lower() != commit:
        raise ValueError("The requested commit differs from the tested SHA in the CI log")
    candidates = [
        {"hypothesis": f"Supplied patch {patch.name}", "evidence": [],
         "diff": patch.read_text(encoding="utf-8-sig")}
        for patch in initial_patches
    ]
    if args.seed_patch:
        candidates = [identify(candidates[0], commit, None)]
    return {
        "task": {
            **profile,
            "id": str(uuid4()),
            "data_version": 2,
            "repository_key": repository_id(repository, profile.get("explicit_repo_id")),
            "commit": commit,
            "commit_source": "argument" if args.commit else "CI log" if log_commit else "local HEAD",
            "targets": targets,
            "model": args.model if not args.patch else None,
            "retrieval_mode": args.retrieval if not args.patch else None,
            "embedding_model": embedding_model if not args.patch and args.retrieval == "hybrid" else None,
            "strategy": strategy,
            "limits": {**DEFAULTS, **{k: v for k, v in limits.items() if v is not None}}
                      if strategy == "sequential" and not args.patch else None,
            "use_memory": not args.no_memory,
            "use_retry_feedback": not args.no_retry_feedback,
        },
        "candidates": candidates,
        "status": "running",
    }


def project_profile(args):
    return resolve_project_profile(
        args.repo, args.config, cwd=args.cwd, python=args.python, source_paths=args.source_path,
        import_modules=args.imports, pytest_args=args.pytest_arg, regression=args.regression, timeout=args.timeout,
    )


def ci_task_id(initial):
    task = initial["task"]
    identity = {k: v for k, v in task.items() if k not in
                {"id", "commit_source", "repository", "project_config", "targets"}}
    identity["targets"] = sorted(task["targets"])
    identity["initial_patches"] = [c["diff"] for c in initial["candidates"]]
    if path := task.get("project_config"):
        identity["config_sha256"] = sha256(Path(path).read_bytes()).hexdigest()
    return str(uuid5(NAMESPACE_URL, json.dumps(identity, sort_keys=True, ensure_ascii=False)))


def main(argv=None, *, adapter_factory=PytestAdapter) -> int:
    args = parser().parse_args(argv)
    storage = args.state_dir.resolve()
    try:
        if args.command == "memory":
            repo = repository_path(args.repo)
            _, settings = load_project_config(repo, args.config)
            key = repository_id(repo, settings.get("repo_id"))
            memory = RepairMemory(storage / "repairs.sqlite")
            legacy = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").strip())
            memory.adopt_legacy(key, os.path.normcase(str(legacy)), repo)
            if args.action != "list" and not args.experience_id:
                raise ValueError("Supply the experience ID from memory list")
            if args.action in ("disable", "delete"):
                memory.invalidate(key, args.experience_id, delete=args.action == "delete")
            else:
                rows = memory.list_experiences(key)
                for item in rows:
                    item["source_available"] = memory.source_exists(item)
                if args.action == "show":
                    rows = next((item for item in rows if item["id"] == args.experience_id), None)
                    if rows is None:
                        raise ValueError("Experience not found in this repository")
                print(json.dumps(rows, ensure_ascii=False, indent=2))
            return 0
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
            if args.reuse_task:
                initial["task"]["id"] = ci_task_id(initial)
            if initial["task"]["use_memory"] and (storage / "repairs.sqlite").is_file():
                task = initial["task"]
                repo = Path(task["repository"])
                legacy = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").strip())
                RepairMemory(storage / "repairs.sqlite").adopt_legacy(task["repository_key"], os.path.normcase(str(legacy)), repo)
        task_id = initial["task"]["id"] if initial else str(args.task_id)
        directory = storage / "runs" / task_id
        database = storage / "checkpoints.sqlite"
        if initial is None and not database.is_file():
            raise ValueError(f"Unknown task: {task_id}")
        storage.mkdir(parents=True, exist_ok=True)
        config = {"configurable": {"thread_id": task_id}, "recursion_limit": 100000}
        with SqliteSaver.from_conn_string(str(database)) as saver:
            reusable = args.command == "fix" and args.reuse_task
            relocation = repository_path(args.repo) if (reusable or args.command == "resume") and args.repo else None
            graph = build_workflow(saver, storage, pause_after_candidate=getattr(args, "pause_after_candidate", False),
                                   relocated_repo=relocation, config_override=args.config if args.command == "resume" else None,
                                   adapter_factory=adapter_factory)
            snapshot = graph.get_state(config)
            if initial is None and not snapshot.values:
                raise ValueError(f"Unknown task: {task_id}")
            if args.command == "status":
                state = snapshot.values
                status = state["status"]
                if snapshot.next:
                    status = "interrupted"
                    with closing(sqlite3.connect(directory / "lock.sqlite", timeout=0)) as probe:
                        try:
                            probe.execute("BEGIN IMMEDIATE")
                        except sqlite3.OperationalError as failure:
                            if failure.sqlite_errorcode != sqlite3.SQLITE_BUSY:
                                raise
                            status = "running"
                        else:
                            events = directory / "events.jsonl"
                            if events.is_file() and json.loads(events.read_text(encoding="utf-8").splitlines()[-1])["event"] == "task_paused":
                                status = "paused"
                print(json.dumps({
                    "task_id": task_id,
                    "status": status, "commit": state["task"]["commit"],
                    "completed_candidates": len(state.get("validations", [])),
                    "checkpoint": snapshot.config["configurable"]["checkpoint_id"],
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
                if reusable and snapshot.values:
                    initial = None
                elif reusable and (directory / "report.json").exists():
                    raise ValueError("Restore checkpoints.sqlite together with the existing run directory")
                print(f"Task ID: {task_id}", flush=True)
                if args.result_file:
                    write_json(args.result_file, {"task_id": task_id, "directory": str(directory),
                               "report": str(directory / "report.json"), "markdown": str(directory / "report.md")})
                if initial is not None:
                    record_run_inputs(directory, initial["task"], input_log)
                error = None
                should_run = initial is not None or bool(snapshot.next)
                if should_run:
                    details = {}
                    if initial is None:
                        state = snapshot.values
                        repo = relocation or Path(state["task"]["repository"])
                        checkpoint = snapshot.config["configurable"]["checkpoint_id"]
                        try:
                            verify_checkpoint(directory, state, relocation, args.config)
                            if state["task"].get("limits"):
                                Budget(directory, state["task"]["limits"]).reconcile(state)
                        except (ValueError, OSError, subprocess.TimeoutExpired) as failure:
                            artifact = record_resume(directory, state, snapshot.next, checkpoint, repo, str(failure))
                            previous = directory / "report.json"
                            report = read_report(previous) if previous.is_file() else {"schema_version": 2, **state}
                            write_report(directory, {**report, "status": "interrupted", "reason_code": "resume_check_failed",
                                                  "reason": str(failure), "next": list(snapshot.next),
                                                  "recovery": [*report.get("recovery", []), artifact]})
                            append_workflow_event(directory, task_id, "task_interrupted", recovery=artifact, next=list(snapshot.next))
                            print(f"Resume check failed: {failure}", file=sys.stderr)
                            return 2
                        details["recovery"] = record_resume(directory, state, snapshot.next, checkpoint, repo)
                        print(f"Resuming {', '.join(snapshot.next)} after {len(state.get('validations', []))} completed candidates")
                    append_workflow_event(
                        directory, task_id, "task_started" if initial is not None else "task_resumed", **details,
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
                if state["task"].get("limits"):
                    Budget(directory, state["task"]["limits"])
                publish(directory, state, snapshot.next, status=status, error=error)
            return {"accepted": 0, "paused": 0, "rejected": 1}.get(status, 2)
    except (ValueError, OSError, sqlite3.Error, subprocess.TimeoutExpired) as error:
        print(f"ci-repair: {error}", file=sys.stderr)
        return 2
