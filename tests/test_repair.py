import json
from pathlib import Path
import shutil
import subprocess
import sys

from ci_repair.pytest_adapter import PytestAdapter, failing_nodes
from ci_repair.workspace import git


PROJECT = Path(__file__).resolve().parents[1]


def make_repository(path: Path, source: Path) -> None:
    shutil.copytree(source, path, ignore=shutil.ignore_patterns("__pycache__"))
    git(path, "init", "--quiet")
    git(path, "add", ".")
    git(path, "-c", "user.email=fixture@example.invalid", "-c", "user.name=Fixture",
        "commit", "--quiet", "-m", "Failing baseline")


def create_patch(repository: Path, path: str, new_code: str, patch: Path) -> None:
    target = repository / path
    old_code = target.read_bytes()
    try:
        target.write_text(new_code, encoding="utf-8", newline="\n")
        patch.write_text(git(repository, "diff", "--", path), encoding="utf-8", newline="\n")
    finally:
        target.write_bytes(old_code)


def cli(storage: Path, *args: str):
    return subprocess.run(
        [sys.executable, "-c", "from ci_repair.cli import main; raise SystemExit(main())",
         "--state-dir", str(storage), *args],
        cwd=PROJECT, text=True, encoding="utf-8", capture_output=True, timeout=90,
    )


def test_reports_real_execution_states(tmp_path, monkeypatch):
    sample = tmp_path / "test_status.py"
    sample.write_text(
        "import pytest\n"
        "def test_pass():\n"
        "    print('LOG_START')\n"
        "    print('x' * 14000)\n"
        "def test_fail(): assert False\n"
        "@pytest.mark.skip\ndef test_skip(): pass\n"
        "@pytest.mark.xfail\ndef test_expected_failure(): assert False\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PYTEST_ADDOPTS", "-s")
    task_directory = tmp_path / "task"
    adapter = PytestAdapter(Path(sys.executable), tmp_path, 15, task_directory=task_directory)
    result = adapter.run(tmp_path, [sample.name])
    assert result["status"] == "failed", result
    assert {key.split("::")[1]: value["status"] for key, value in result["tests"].items()} == {
        "test_pass": "passed", "test_fail": "failed", "test_skip": "skipped",
        "test_expected_failure": "xfailed",
    }
    assert "LOG_START" in (task_directory / result["artifacts"]["stdout"]).read_text(encoding="utf-8")
    assert "LOG_START" not in result["output"]
    assert len(result["output"]) == 12000
    assert adapter.run(tmp_path, [sample.name + "::test_missing"])["status"] == "error"
    sample.write_text("import missing_test_dependency\n", encoding="utf-8")
    collection = adapter.run(tmp_path, [sample.name])
    assert collection["status"] == "error"
    assert "missing_test_dependency" in collection["collection_errors"][0]
    timed_out = PytestAdapter(
        Path(sys.executable), tmp_path, 0.001, task_directory=task_directory
    ).run(tmp_path, [sample.name])
    assert timed_out["status"] == "timeout"
    assert (task_directory / timed_out["artifacts"]["stdout"]).is_file()
    assert (task_directory / timed_out["artifacts"]["stderr"]).is_file()


def test_extracts_parameterized_nodes():
    assert failing_nodes(
        "\x1b[31mFAILED\x1b[0m tests\\test_shipping.py::test_fee[remote zone] - AssertionError\n"
        "FAILED tests/test_shipping.py::test_fee[standard]\n"
    ) == ["tests/test_shipping.py::test_fee[remote zone]", "tests/test_shipping.py::test_fee[standard]"]


def test_resume_after_rejecting_regression(tmp_path):
    repo = tmp_path / "shipping"
    make_repository(repo, PROJECT / "examples/shipping")
    storage = tmp_path / "state"
    original = (repo / "shipping.py").read_text(encoding="utf-8")
    wrong, right = tmp_path / "wrong.diff", tmp_path / "right.diff"
    create_patch(repo, "shipping.py", original.replace(
        "    if remote_area:", '    if subtotal == FREE_SHIPPING_THRESHOLD:\n        return Decimal("0")\n    if remote_area:'
    ), wrong)
    create_patch(repo, "shipping.py", original.replace("subtotal >", "subtotal >="), right)
    (repo / "shipping.py").write_text(original + "\n# local edit\n", encoding="utf-8")
    log = tmp_path / "ci.log"
    commit = git(repo, "rev-parse", "HEAD").strip()
    target_node = "test_shipping.py::test_standard_shipping_at_threshold_is_free"
    log_bytes = f"\ufeffGITHUB_SHA={commit}\r\nFAILED {target_node} - AssertionError\r\n".encode("utf-8")
    log.write_bytes(log_bytes)

    result = cli(storage, "fix", "--repo", str(repo), "--log", str(log),
                 "--patch", str(wrong), "--patch", str(right), "--pause-after-candidate")
    assert result.returncode == 0, result.stdout + result.stderr
    task = result.stdout.splitlines()[0].split()[-1]
    artifacts = storage / "runs" / task
    report = json.loads((artifacts / "report.json").read_text(encoding="utf-8"))
    assert report["status"] == "paused", report
    manifest_path = artifacts / report["manifest"]
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    assert manifest["schema_version"] == 1
    assert manifest["task_id"] == task
    assert manifest["commit"] == commit
    assert manifest["commit_source"] == "CI log"
    assert manifest["targets"] == [target_node]
    saved_log = artifacts / manifest["input_log"]
    assert saved_log.read_bytes() == log_bytes
    failed_node = "test_shipping.py::test_remote_shipping_at_threshold_is_charged"
    assert report["validations"][0]["regressions"] == [failed_node]
    rejected_evidence = report["validations"][0]["regression"]["artifacts"]
    rejected_report = artifacts / rejected_evidence["pytest_report"]
    assert json.loads(rejected_report.read_text(encoding="utf-8"))["phases"][failed_node]["call"]["status"] == "failed"
    assert failed_node in (artifacts / rejected_evidence["stdout"]).read_text(encoding="utf-8")
    assert not (artifacts / "fix.diff").exists()

    log.unlink()
    resumed = cli(storage, "resume", task)
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    report = json.loads((artifacts / "report.json").read_text(encoding="utf-8"))
    assert report["status"] == "accepted"
    assert report["manifest"] == manifest_path.name
    assert manifest_path.read_bytes() == manifest_bytes
    assert saved_log.read_bytes() == log_bytes
    assert [candidate["status"] for candidate in report["validations"]] == ["rejected", "accepted"]
    assert report["validations"][0]["regression"]["artifacts"] == rejected_evidence
    accepted_evidence = report["validations"][1]["regression"]["artifacts"]
    assert accepted_evidence["pytest_report"] != rejected_evidence["pytest_report"]
    accepted_report = artifacts / accepted_evidence["pytest_report"]
    assert json.loads(accepted_report.read_text(encoding="utf-8"))["phases"][failed_node]["call"]["status"] == "passed"
    assert "subtotal >=" in (artifacts / "fix.diff").read_text(encoding="utf-8")
    assert "# local edit" in (repo / "shipping.py").read_text(encoding="utf-8")
    assert git(repo, "worktree", "list").count("\n") == 1

    from ci_repair.memory import RepairMemory
    from ci_repair.workspace import repository_key

    memory = RepairMemory(storage / "repairs.sqlite")
    assert memory.search(repository_key(repo), "shipping threshold")[0]["verified_tests"] == 4
    assert memory.search("another repository", "shipping threshold") == []


def test_rejects_test_edit_and_accepts_cross_file_fix(tmp_path):
    repo = tmp_path / "checkout"
    make_repository(repo, PROJECT / "examples/checkout")
    wrong, right = tmp_path / "test-edit.diff", tmp_path / "checkout.diff"
    test_code = (repo / "test_checkout.py").read_text(encoding="utf-8")
    create_patch(repo, "test_checkout.py", test_code.replace("== 170", "== 182"), wrong)
    checkout = (repo / "checkout.py").read_text(encoding="utf-8")
    create_patch(repo, "checkout.py", checkout.replace("shipping_fee(discounted,", "shipping_fee(subtotal,"), right)
    storage = tmp_path / "state"
    result = cli(storage, "fix", "--repo", str(repo), "--test",
                 "test_checkout.py::test_discount_keeps_free_shipping", "--patch", str(wrong), "--patch", str(right))
    assert result.returncode == 0, result.stdout + result.stderr
    report_path = next((storage / "runs").glob("*/report.json"))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    manifest = json.loads((report_path.parent / report["manifest"]).read_text(encoding="utf-8"))
    assert manifest["input_log"] is None
    assert not (report_path.parent / "artifacts" / "input").exists()
    assert "protected" in report["validations"][0]["reason"]
    assert report["validations"][1]["status"] == "accepted"
