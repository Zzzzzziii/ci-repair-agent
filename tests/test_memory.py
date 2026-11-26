import json
import sqlite3

import pytest

from ci_repair.artifacts import read_report
from ci_repair.memory import RepairMemory
from ci_repair.project import repository_id
from ci_repair.workspace import git


def test_identity_survives_move_and_normalizes_remotes(tmp_path):
    repo = tmp_path / "local"
    repo.mkdir()
    git(repo, "init", "--quiet")
    local = repository_id(repo)
    repo.rename(tmp_path / "moved")
    repo = tmp_path / "moved"
    assert repository_id(repo) == local
    git(repo, "remote", "add", "origin", "https://fixture:secret@example.org/team/project.git?token=fixture")
    remote = repository_id(repo)
    git(repo, "remote", "set-url", "origin", "git@example.org:team/project")
    assert remote == repository_id(repo) and "secret" not in remote
    assert repository_id(repo, "team-api") == "project:team-api"


def test_legacy_migration_source_validation_and_management(tmp_path):
    memory = RepairMemory(tmp_path / "repairs.sqlite")
    assert memory.search("empty", "pricing") == []
    old = tmp_path / "old"
    old.mkdir()
    store = RepairMemory(old / "repairs.sqlite")
    with sqlite3.connect(store.database) as db:
        db.execute("CREATE TABLE repairs(task_id TEXT PRIMARY KEY,repository TEXT,record TEXT)")
        db.execute("INSERT INTO repairs VALUES(?,?,?)", ("task", "shop", json.dumps(
            {"commit": "abc", "targets": ["test_pricing"], "signature": "tax", "verified_tests": 1})))
    assert store.search("shop", "pricing") == []
    folder = old / "runs/task"
    folder.mkdir(parents=True)
    report = {"task": {"id": "task", "commit": "abc"}, "validations": [
        {"candidate": 1, "status": "accepted", "candidate_diff": "patch.diff",
         "target": {"artifacts": {"pytest_report": "pytest.json"}},
         "regression": {"artifacts": {"pytest_report": "pytest.json"}}}]}
    (folder / "report.json").write_text(json.dumps(report))
    (folder / "pytest.json").write_text("{}")
    (folder / "patch.diff").write_text("verified")
    record, = store.search("shop", "pricing")
    assert record["source_report"] == "runs/task/report.json" and record["last_checked"]
    assert store.search("another", "pricing") == []
    store.invalidate("shop", "task")
    assert store.search("shop", "pricing") == []
    store.invalidate("shop", "task", delete=True)
    assert store.list_experiences("shop") == []
    (folder / "report.json").write_text(json.dumps({**report, "schema_version": 99}))
    with pytest.raises(ValueError, match="version"):
        read_report(folder / "report.json")
