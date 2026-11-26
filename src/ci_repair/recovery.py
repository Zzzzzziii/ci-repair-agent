from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
from datetime import datetime, timezone
from uuid import uuid4

from ci_repair.artifacts import write_json
from ci_repair.project import load_project_config, prepare_candidate_environment, repository_id
from ci_repair.sequential import identify
from ci_repair.workspace import commit_id, git, repository_path, within, worktree


def artifact_paths(value):
    if isinstance(value, list):
        for item in value:
            yield from artifact_paths(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            if key == "artifacts" and isinstance(item, dict):
                yield from (name for name in item.values() if isinstance(name, str))
            elif key == "applied_diff_artifact":
                yield item
            elif key in ("event_id", "model_call_id") and isinstance(item, str):
                category = "tools" if key == "event_id" else "model"
                yield f"artifacts/{category}/{item}.json"
            elif key not in ("task", "history", "observations", "decision", "args"):
                yield from artifact_paths(item)


def digest(path):
    from hashlib import file_digest
    with path.open("rb") as stream:
        return file_digest(stream, "sha256").hexdigest()


def checkpoint_evidence(directory, updates, existing):
    hashes = dict(existing)
    for name in set(artifact_paths(updates)) - hashes.keys():
        hashes[name] = digest(within(directory, name))
    decision = updates.get("decision")
    if decision:
        name = f"artifacts/model/{decision['model_call_id']}.json"
        hashes[name] = digest(within(directory, name))
    return hashes


def check_environment(task, reference, repository, directory):
    target = reference.get("target", {})
    runtime = target.get("runtime", {})
    expected = runtime.get("environment_fingerprint")
    if not expected:
        return
    reporter = Path(__file__).with_name("pytest_reporter.py")
    script = ("import json,runpy,sys; sys.path[0]=sys.argv[1]; import pytest; "
              "f=runpy.run_path(sys.argv[2])['runtime_environment']; "
              "print(f(pytest.__version__,json.loads(sys.argv[3]))['environment_fingerprint'])")
    scratch = directory.parent.parent / "scratch"
    with worktree(repository, task["commit"], scratch, task_directory=directory, purpose="resume_environment") as tree:
        probe = subprocess.run(
            [task["python"], "-B", "-X", "utf8", "-c", script, str(reporter.parent), str(reporter),
             json.dumps(task.get("pytest_args", []))],
            cwd=within(tree, task["cwd"]), env=prepare_candidate_environment(tree, task.get("source_paths", [])),
            capture_output=True, text=True, timeout=30,
        )
        actual = probe.stdout.strip()
        if config := runtime.get("pytest_config"):
            old_root = Path(target["cwd"])
            for _ in Path(task["cwd"]).parts:
                old_root = old_root.parent
            path = Path(config["path"])
            if path.is_relative_to(old_root):
                path = tree / path.relative_to(old_root)
            actual = sha256(f"{actual}:{digest(path)}".encode("ascii")).hexdigest()
    if probe.returncode or actual != expected:
        raise ValueError("environment_changed: restore the baseline Python, pytest settings and configuration")


def verify_checkpoint(directory, state, new_repo=None, config=None):
    task = state["task"]
    version = task.get("data_version", 1)
    if version not in (1, 2):
        raise ValueError(f"Unsupported task data version {version}")
    repo = repository_path(new_repo or Path(task["repository"]))
    if version == 2:
        selected = resume_config(task, repo, config)
        _, settings = load_project_config(repo, selected)
        if repository_id(repo, settings.get("repo_id")) != task["repository_key"]:
            raise ValueError("repository_mismatch: repository identity differs from the checkpoint")
    elif repo != Path(task["repository"]):
        common = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").strip())
        if os.path.normcase(str(common)) != task["repository_key"]:
            raise ValueError("Legacy checkpoint requires the original repository location")
    if commit_id(repo, task["commit"]) != task["commit"]:
        raise ValueError("Checkpoint commit is unavailable")
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if (not isinstance(manifest, dict) or manifest.get("schema_version") != 1
            or manifest.get("commit") != task["commit"] or manifest.get("task_id") != task["id"]):
        raise ValueError("Manifest version or task identity mismatch")
    if name := manifest.get("input_log"):
        path = within(directory, name)
        if not path.is_file() or (manifest.get("input_sha256") and digest(path) != manifest["input_sha256"]):
            raise ValueError("Original CI log is missing or changed")
    for candidate in state.get("candidates", []):
        if "id" in candidate:
            actual = identify(candidate, task["commit"], candidate.get("parent_id"))
            if any(actual[key] != candidate.get(key) for key in ("commit", "id", "diff_hash")):
                raise ValueError("Candidate hash differs from checkpoint metadata")
    hashes = state.get("evidence_hashes", {})
    for name in set(artifact_paths({k: state[k] for k in ("reference", "validations", "candidates") if k in state})) | hashes.keys():
        path = within(directory, name)
        if not path.is_file() or (name in hashes and digest(path) != hashes[name]):
            raise ValueError(f"Evidence missing or changed: {name}")
    if task.get("limits") and state.get("reference") and not (directory / "budget.json").is_file():
        raise ValueError("Missing persisted budget")
    check_environment(task, state.get("reference", {}), repo, directory)
    return repo


def resume_config(task, repo, override):
    if override:
        return Path(override).resolve()
    if original := task.get("project_config"):
        path = Path(original)
        if path.is_relative_to(task["repository"]):
            return repo / path.relative_to(task["repository"])
        return path
    return None


def record_resume(directory, state, next_nodes, checkpoint, repo, issue=None):
    folder = directory / "artifacts/recovery"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{uuid4().hex}.json"
    write_json(path, {"schema_version": 1, "timestamp": datetime.now(timezone.utc).isoformat(),
                      "task_id": state["task"]["id"], "checkpoint": checkpoint,
                      "next": list(next_nodes), "repository": str(repo), "issue": issue,
                      "completed_candidates": len(state.get("validations", []))})
    return path.relative_to(directory).as_posix()
