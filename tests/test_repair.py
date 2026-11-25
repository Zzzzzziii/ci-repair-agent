import json
from pathlib import Path
import shutil
import subprocess
import sys

from langchain_core.messages import AIMessage, ToolMessage
import pytest

from ci_repair import model, workflow
from ci_repair.cli import main
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


def read_timeline(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


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
    candidate_paths = [item["diff_artifact"] for item in report["candidates"]]
    original_diffs = [path.read_text(encoding="utf-8-sig").encode("utf-8") for path in (wrong, right)]
    assert [item["candidate"] for item in report["candidates"]] == [1, 2]
    assert [item["candidate_diff"] for item in report["validations"]] == candidate_paths[:1]
    assert [(artifacts / path).read_bytes() for path in candidate_paths] == original_diffs
    timeline_path = artifacts / report["events"]
    paused_events = read_timeline(timeline_path)
    assert [event["event"] for event in paused_events] == [
        "task_started", "node_started", "node_finished", "node_started", "node_finished", "task_paused",
    ]
    assert paused_events[-1]["details"] == {"status": "paused", "next": ["validate"]}
    first_validation = paused_events[-2]["details"]
    assert first_validation["node"] == "validate"
    assert first_validation["outcome"] == "rejected"
    assert first_validation["candidate"] == 1
    assert first_validation["step_id"] == paused_events[-3]["details"]["step_id"]
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
    wrong.unlink()
    right.unlink()
    resumed = cli(storage, "resume", task)
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    report = json.loads((artifacts / "report.json").read_text(encoding="utf-8"))
    assert report["status"] == "accepted"
    assert report["events"] == timeline_path.name
    events = read_timeline(timeline_path)
    assert events[:len(paused_events)] == paused_events
    assert [event["event"] for event in events[len(paused_events):]] == [
        "task_resumed", "node_started", "node_finished", "node_started", "node_finished", "task_finished",
    ]
    second_validation = events[len(paused_events) + 2]["details"]
    assert second_validation["outcome"] == "accepted"
    assert second_validation["candidate"] == 2
    assert second_validation["step_id"] != first_validation["step_id"]
    assert events[-1]["details"] == {"status": "accepted", "next": []}
    assert all(event["task_id"] == task for event in events)
    assert report["manifest"] == manifest_path.name
    assert manifest_path.read_bytes() == manifest_bytes
    assert saved_log.read_bytes() == log_bytes
    assert [candidate["status"] for candidate in report["validations"]] == ["rejected", "accepted"]
    assert [item["candidate_diff"] for item in report["validations"]] == candidate_paths
    assert [(artifacts / path).read_bytes() for path in candidate_paths] == original_diffs
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


def test_saves_rejected_model_candidate(tmp_path, monkeypatch):
    repo = tmp_path / "shipping"
    make_repository(repo, PROJECT / "examples/shipping")
    storage = tmp_path / "state"
    proposed_diff = "Malformed candidate from a scripted proposal\n"

    def propose(*args, **kwargs):
        return {
            "candidates": [{"diff": proposed_diff, "hypothesis": "Test proposal", "evidence": []}],
            "tool_trace": [],
            "model_usage": [],
        }

    monkeypatch.setattr(workflow, "propose", propose)
    assert main([
        "--state-dir", str(storage), "fix", "--repo", str(repo),
        "--test", "test_shipping.py::test_standard_shipping_at_threshold_is_free",
        "--model", "deepseek:deepseek-flash",
    ]) == 1

    report_path = next((storage / "runs").glob("*/report.json"))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    candidate, = report["candidates"]
    validation, = report["validations"]
    assert report["status"] == validation["status"] == "rejected"
    assert candidate["diff_artifact"] == validation["candidate_diff"]
    assert (report_path.parent / candidate["diff_artifact"]).read_bytes() == proposed_diff.encode("utf-8")
    assert not (report_path.parent / "fix.diff").exists()


def test_config_uses_candidate_src(tmp_path, monkeypatch):
    fixture = tmp_path / "fixture"
    source = Path("service/src/demo_shop/__init__.py")
    (fixture / source).parent.mkdir(parents=True)
    (fixture / source).write_text("price = 5\n", encoding="utf-8")
    tests = fixture / "service" / "tests"
    tests.mkdir()
    (tests / "test_price.py").write_text(
        "from demo_shop import price\n\ndef test_price():\n    assert price == 9\n", encoding="utf-8",
    )
    (fixture / ".ci-repair.toml").write_text(
        'cwd = "service"\nsource_paths = ["service/src"]\nregression = ["tests"]\n', encoding="utf-8",
    )
    old_package = tmp_path / "installed" / "demo_shop"
    old_package.mkdir(parents=True)
    (old_package / "__init__.py").write_text("price = 9\n", encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(old_package.parent))
    repo = tmp_path / "repo"
    make_repository(repo, fixture)
    storage = tmp_path / "state"

    doctor = cli(storage, "doctor", "--repo", str(repo), "--import", "demo_shop")
    assert doctor.returncode == 0, doctor.stderr
    diagnosis = json.loads(doctor.stdout)
    assert diagnosis["status"] == "ready"
    assert Path(diagnosis["imports"]["demo_shop"]) == repo / source
    assert diagnosis["pytest_version"]
    assert not storage.exists()

    missing = cli(storage, "doctor", "--repo", str(repo), "--import", "uninstalled_ci_fixture_package")
    assert missing.returncode == 2
    assert json.loads(missing.stdout)["missing_imports"] == ["uninstalled_ci_fixture_package"]

    patch = tmp_path / "repair.diff"
    create_patch(repo, source.as_posix(), "price = 9\n", patch)
    result = cli(storage, "fix", "--repo", str(repo), "--test", "tests/test_price.py::test_price",
                 "--patch", str(patch))
    assert result.returncode == 0, result.stdout + result.stderr
    report_path, = (storage / "runs").glob("*/report.json")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["reference"]["target"]["status"] == "failed"
    assert report["validations"][0]["regression"]["status"] == "passed"
    manifest = json.loads((report_path.parent / report["manifest"]).read_text(encoding="utf-8"))
    assert manifest["cwd"] == diagnosis["cwd"] == "service"
    assert manifest["source_paths"] == diagnosis["source_paths"] == ["service/src"]
    assert manifest["regression"] == diagnosis["regression"] == ["tests"]


@pytest.mark.parametrize(("purpose", "status", "error_type"), [
    ("initialize", "setup_failed", "ValueError"),
    ("explore", "request_failed", "TimeoutError"),
    ("propose", "invalid_output", "ValueError"),
])
def test_interrupted_model_task_links_call_records(tmp_path, monkeypatch, purpose, status, error_type):
    repo = tmp_path / "shipping"
    make_repository(repo, PROJECT / "examples/shipping")
    storage = tmp_path / "state"
    placeholder = "placeholder-for-private-error-detail"

    class ScriptedModel:
        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            if purpose == "explore":
                raise TimeoutError(placeholder)
            if purpose == "propose" and not isinstance(messages[-1], ToolMessage):
                return AIMessage(content="", tool_calls=[{
                    "id": "read-1", "name": "read_file", "args": {"path": "shipping.py", "end_line": 8},
                }])
            return AIMessage(content="Ready to propose a fix")

        def with_structured_output(self, schema, *, include_raw):
            class MalformedProposal:
                def invoke(self, messages):
                    return {"parsed": None, "raw": AIMessage(content=placeholder),
                            "parsing_error": ValueError(placeholder)}

            return MalformedProposal()

    def init_model(name, **options):
        if purpose == "initialize":
            raise ValueError(placeholder)
        return ScriptedModel()

    monkeypatch.setattr(model, "init_chat_model", init_model)
    assert main([
        "--state-dir", str(storage), "fix", "--repo", str(repo),
        "--test", "test_shipping.py::test_standard_shipping_at_threshold_is_free",
        "--model", "deepseek:deepseek-flash",
    ]) == 2

    report_path = next((storage / "runs").glob("*/report.json"))
    report_text = report_path.read_text(encoding="utf-8")
    report = json.loads(report_text)
    assert report["status"] == "interrupted"
    assert report["next"] == ["generate"]
    assert placeholder not in report_text
    timeline_path = report_path.parent / report["events"]
    assert placeholder not in timeline_path.read_text(encoding="utf-8")
    events = read_timeline(timeline_path)
    started, failed, interrupted = events[-3:]
    assert started["event"] == "node_started"
    assert failed["event"] == "node_failed"
    assert failed["details"]["node"] == "generate"
    assert failed["details"]["step_id"] == started["details"]["step_id"]
    assert failed["details"]["error_type"] == "ModelStepError"
    assert interrupted["event"] == "task_interrupted"
    assert interrupted["details"] == {"status": "interrupted", "next": ["generate"]}
    call_texts = [(report_path.parent / path).read_text(encoding="utf-8") for path in report["model_calls"]]
    assert all(placeholder not in text for text in call_texts)
    calls = [json.loads(text) for text in call_texts]
    failed = next(call for call in calls if call["purpose"] == purpose)
    assert failed["task_id"] == report["task"]["id"]
    assert failed["status"] == status
    assert failed["error_type"] == error_type
    if purpose == "propose":
        tool_event_path, = report["tool_events"]
        event = json.loads((report_path.parent / tool_event_path).read_text(encoding="utf-8"))
        assert event["task_id"] == report["task"]["id"]
        assert event["status"] == "succeeded"
        assert event["tool"] == "read_file"
        assert "FREE_SHIPPING_THRESHOLD" in event["result"]
        assert next(call for call in calls if call["call_id"] == event["model_call_id"])["purpose"] == "explore"
