import json

from ci_repair import workspace
from ci_repair.artifacts import publish


def test_worktree_cleanup_failure_is_reported_without_losing_result(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    task = tmp_path / "task"
    git = workspace.git
    git(repo, "init", "--quiet")
    git(repo, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "--quiet", "--allow-empty", "-m", "Baseline")
    commit = workspace.commit_id(repo, "HEAD")

    def fail_cleanup(repository, *args, **kwargs):
        if args[:2] == ("worktree", "remove"):
            raise ValueError("Fixture holds the directory open")
        return git(repository, *args, **kwargs)

    monkeypatch.setattr(workspace, "git", fail_cleanup)
    with workspace.worktree(repo, commit, scratch, task_directory=task, purpose="candidate") as tree:
        assert workspace.commit_id(tree, "HEAD") == commit
        assert tree.is_relative_to(scratch)

    try:
        record_path, = (task / "artifacts" / "worktrees").glob("*.json")
        record = json.loads(record_path.read_text(encoding="utf-8"))
        assert record["status"] == "cleanup_failed"
        assert record["commit"] == commit
        assert record["path"] == str(tree)
        assert record["error"] == "Fixture holds the directory open"
        publish(task, {}, (), status="blocked")
        result = json.loads((task / "report.json").read_text(encoding="utf-8"))
        assert result["worktree_issues"][0]["artifact"] == result["worktrees"][0]
        assert result["worktree_issues"][0]["status"] == "cleanup_failed"
    finally:
        git(repo, "worktree", "remove", "--force", str(tree))
