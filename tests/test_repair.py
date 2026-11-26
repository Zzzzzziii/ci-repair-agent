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
    assert result["failure_kind"] == "test_failure"
    assert Path(result["command"][0]) == Path(sys.executable)
    assert result["command"][-1] == sample.name
    assert Path(result["cwd"]) == tmp_path
    assert result["session_finished"]
    assert set(result["collected"]) == result["tests"].keys()
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
    assert collection["failure_kind"] == "missing_dependency"
    assert collection["missing_dependencies"][0]["module"] == "missing_test_dependency"
    assert collection["missing_dependencies"][0]["phase"] == "collect"
    assert "missing_test_dependency" in collection["collection_errors"][0]
    sample.write_text("def test_incomplete(\n", encoding="utf-8")
    invalid = adapter.run(tmp_path, [sample.name])
    assert invalid["failure_kind"] == "collection_error"
    assert invalid["missing_dependencies"] == []
    timed_out = PytestAdapter(
        Path(sys.executable), tmp_path, 0.001, task_directory=task_directory
    ).run(tmp_path, [sample.name])
    assert timed_out["status"] == "timeout"
    assert timed_out["failure_kind"] == "timeout"
    assert (task_directory / timed_out["artifacts"]["stdout"]).is_file()
    assert (task_directory / timed_out["artifacts"]["stderr"]).is_file()


def test_records_collected_tests_without_execution(tmp_path, monkeypatch):
    sample = tmp_path / "test_wait.py"
    sample.write_text("import time\n\ndef test_wait():\n    time.sleep(12)\n", encoding="utf-8")
    task_directory = tmp_path / "task"
    node = "test_wait.py::test_wait"

    monkeypatch.setenv("PYTEST_ADDOPTS", "--collect-only")
    result = PytestAdapter(Path(sys.executable), tmp_path, 15, task_directory=task_directory).run(
        tmp_path, [sample.name],
    )
    assert result["exit_code"] == 0
    assert result["session_finished"]
    assert result["collected"] == [node]
    assert result["status"] == "incomplete"
    assert result["tests"][node]["status"] == "not_run"

    monkeypatch.delenv("PYTEST_ADDOPTS")
    interrupted = PytestAdapter(Path(sys.executable), tmp_path, 3, task_directory=task_directory).run(
        tmp_path, [sample.name],
    )
    assert interrupted["status"] == "timeout"
    assert not interrupted["session_finished"]
    assert interrupted["collected"] == [node]
    assert interrupted["tests"] == {}
    assert (task_directory / interrupted["artifacts"]["pytest_report"]).is_file()


def test_records_module_imported_during_test(tmp_path):
    source = tmp_path / "local_module.py"
    source.write_text("value = 1\n", encoding="utf-8")
    sample = tmp_path / "test_sample.py"
    sample.write_text("def test_value():\n    from local_module import value\n    assert value == 1\n", encoding="utf-8")
    result = PytestAdapter(
        Path(sys.executable), tmp_path, 15, import_modules=["local_module", "unused_module"],
    ).run(tmp_path, [sample.name])
    assert result["status"] == "passed"
    assert result["imports"]["local_module"] == {"status": "in_worktree", "origins": [str(source.resolve())]}
    assert result["imports"]["unused_module"] == {"status": "not_observed", "origins": []}
    assert result["runtime"]["pytest_version"]


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


@pytest.mark.parametrize("decision", ["read_then_fix", "fix", "stop", "duplicate", "invalid", "budget"])
def test_seeded_regression_drives_model_retry(tmp_path, monkeypatch, decision):
    fixture = tmp_path / "fixture"
    example = Path("examples/shipping")
    shutil.copytree(PROJECT / example, fixture / example, ignore=shutil.ignore_patterns("__pycache__"))
    repo = tmp_path / "repo"
    make_repository(repo, fixture)
    storage = tmp_path / "state"
    source = example / "shipping.py"
    original = (repo / source).read_text(encoding="utf-8")
    fixed = tmp_path / "fixed.diff"
    create_patch(repo, source.as_posix(), original.replace("subtotal >", "subtotal >="), fixed)
    captured_feedback = []

    class ScriptedModel:
        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            recorded = json.loads(messages[1].content.partition("Rejected attempts in this task: ")[2])
            if not captured_feedback:
                captured_feedback.append(recorded)
            attempt, = recorded
            failure, = attempt["regression"]["failures"]
            assert attempt["target"]["status"] == "passed"
            assert failure["node_id"].endswith("::test_remote_shipping_at_threshold_is_charged")
            assert "Decimal('0')" in failure["detail"] and "Decimal('25')" in failure["detail"]
            assert "test_shipping.py:" in failure["detail"]
            saved, = (storage / "runs").glob("*/artifacts/feedback/after-candidate-1.json")
            assert json.loads(saved.read_text(encoding="utf-8"))["attempts"] == recorded
            if decision == "invalid":
                return AIMessage(content="Unstructured reply")
            if decision in ("stop", "duplicate") and (decision == "stop" or "already failed" in messages[-1].content):
                return AIMessage(content="", tool_calls=[{"id": "stop", "name": "stop_repair",
                                 "args": {"reason": "Insufficient evidence for a distinct fix"}}])
            if decision in ("fix", "duplicate") or isinstance(messages[-1], ToolMessage):
                if decision == "read_then_fix":
                    assert "if remote_area:" in messages[-1].content
                return AIMessage(content="", tool_calls=[{"id": "patch", "name": "submit_patch", "args": {
                    "hypothesis": "Keep the remote fee of 25 before the free-shipping threshold check",
                    "evidence": [f"{source.as_posix()}:10"],
                    "diff": (seed if decision == "duplicate" else fixed).read_text(encoding="utf-8"),
                }}])
            return AIMessage(content="", tool_calls=[{
                "id": "read-shipping", "name": "read_file", "args": {"path": source.as_posix(), "end_line": 14},
            }])

    monkeypatch.setattr(model, "init_chat_model", lambda *args, **kwargs: ScriptedModel())
    seed = PROJECT / "evals/fixtures/shipping-regression.diff"
    assert main([
        "--state-dir", str(storage), "fix", "--repo", str(repo),
        "--test", f"{example.as_posix()}/test_shipping.py::test_standard_shipping_at_threshold_is_free",
        "--regression", example.as_posix(), "--model", "deepseek:deepseek-flash", "--seed-patch", str(seed),
        "--pause-after-candidate",
        *(["--max-tool-calls", "0"] if decision == "budget" else []),
    ]) == 0

    report_path, = (storage / "runs").glob("*/report.json")
    paused = json.loads(report_path.read_text(encoding="utf-8"))
    assert paused["status"] == "paused"
    assert paused["next"] == ["retrieve"]
    assert paused["validations"][0]["status"] == "rejected"
    assert not captured_feedback

    successful = decision in ("read_then_fix", "fix")
    assert main(["--state-dir", str(storage), "resume", paused["task"]["id"]]) == (0 if successful else 1)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if not successful:
        assert report["status"] == "rejected"
        assert report["reason_code"] == {"stop": "model_stopped", "duplicate": "model_stopped",
                                        "invalid": "invalid_decision", "budget": "budget_exhausted"}[decision]
        assert len(report["validations"]) == len(report["candidates"]) == 1
        if decision == "duplicate":
            assert any(event["event"] == "duplicate_candidate" for event in read_timeline(report_path.parent / "events.jsonl"))
        if decision == "invalid":
            assert len(report["budget"]["reservations"]["model_calls"]) == 2
        return
    assert report["status"] == "accepted"
    assert report["task"]["strategy"] == "sequential"
    assert [validation["status"] for validation in report["validations"]] == ["rejected", "accepted"]
    feedback_path, = report["retry_feedback"]
    payload = json.loads((report_path.parent / feedback_path).read_text(encoding="utf-8"))
    assert captured_feedback == [payload["attempts"]]
    pytest_path = payload["attempts"][0]["regression"]["artifacts"]["pytest_report"]
    assert (report_path.parent / pytest_path).is_file()
    assert report["candidates"][1]["parent_id"] == report["validations"][0]["candidate_id"]
    if decision == "read_then_fix":
        assert report["tool_trace"][0]["tool"] == "read_file"
    assert "subtotal >=" in report_path.with_name("fix.diff").read_text(encoding="utf-8")


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


def test_model_localizes_checkout_with_commit_scoped_retrieval(tmp_path, monkeypatch):
    repo = tmp_path / "checkout"
    make_repository(repo, PROJECT / "examples/checkout")
    patch = tmp_path / "fix.diff"
    checkout = (repo / "checkout.py").read_text(encoding="utf-8")
    create_patch(repo, "checkout.py", checkout.replace("shipping_fee(discounted,", "shipping_fee(subtotal,"), patch)
    storage = tmp_path / "state"
    initial_contexts = []

    class ScriptedModel:
        def bind_tools(self, tools):
            assert {item.name for item in tools} == {
                "read_file", "search_code", "git_diff", "list_files", "search_symbols", "retrieve_code",
            }
            return self

        def with_structured_output(self, schema, *, include_raw):
            assert schema is model.Proposal and include_raw
            return self

        def invoke(self, messages):
            if str(messages[-1].content).startswith("Return up to"):
                results = {message.tool_call_id: message.content for message in messages if isinstance(message, ToolMessage)}
                assert "return 0 if subtotal >= 200" in results["read-delivery"]
                assert json.loads(results["search-delivery"])["context"][0]["symbol"] == "shipping_fee"
                proposal = model.Proposal(candidates=[model.Candidate(
                    hypothesis="The caller uses the discounted amount for shipping eligibility",
                    evidence=["checkout.py:7", "delivery.py:4", "delivery.py:50"],
                    diff=patch.read_text(encoding="utf-8"),
                )])
                return {"parsed": proposal, "raw": AIMessage(content="")}
            if isinstance(messages[-1], ToolMessage):
                return AIMessage(content="Verified threshold in delivery.py")
            context = json.loads(messages[-1].content.partition("\n")[2])
            initial_contexts.append(context)
            assert {hit["path"] for hit in context} >= {"checkout.py", "delivery.py", "test_checkout.py"}
            assert any(hit["symbol"] == "shipping_fee" and "import" in hit["sources"] for hit in context)
            return AIMessage(content="", tool_calls=[
                {"id": "read-delivery", "name": "read_file", "args": {"path": "delivery.py", "end_line": 4}},
                {"id": "search-delivery", "name": "retrieve_code", "args": {"query": "shipping_fee"}},
            ])

    monkeypatch.setattr(model, "init_chat_model", lambda *args, **kwargs: ScriptedModel())
    assert main(["--state-dir", str(storage), "fix", "--repo", str(repo), "--model", "deepseek:deepseek-flash",
                 "--test", "test_checkout.py::test_discount_keeps_free_shipping", "--no-memory"]) == 0
    report_path, = (storage / "runs").glob("*/report.json")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "accepted"
    candidate, = report["candidates"]
    assert candidate["evidence"] == ["checkout.py:7", "delivery.py:4"]
    assert candidate["unverified_evidence"] == ["delivery.py:50"]
    traces = [json.loads((report_path.parent / path).read_text(encoding="utf-8")) for path in report["retrieval"]]
    assert {trace["purpose"] for trace in traces} == {"initial", "tool"}
    assert all(trace["commit"] == report["task"]["commit"] for trace in traces)
    initial = next(trace for trace in traces if trace["purpose"] == "initial")
    assert initial["context"] == initial_contexts[0]
    assert report_path.with_name("fix.diff").is_file()


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


def test_rejects_dropped_regression_case(tmp_path):
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "shop.py").write_text('price = 5\ncases = ["regular", "old_failure"]\n', encoding="utf-8")
    (fixture / "test_shop.py").write_text(
        'import pytest\nfrom shop import price, cases\n\n'
        'def test_price():\n    assert price == 9\n\n'
        '@pytest.mark.parametrize("case", cases)\n'
        'def test_case(case):\n    assert case != "old_failure"\n', encoding="utf-8",
    )
    repo = tmp_path / "repo"
    make_repository(repo, fixture)
    storage = tmp_path / "state"
    patch = tmp_path / "repair.diff"
    create_patch(repo, "shop.py", 'price = 9\ncases = ["regular"]\n', patch)

    result = cli(storage, "fix", "--repo", str(repo), "--test", "test_shop.py::test_price", "--patch", str(patch))
    assert result.returncode == 1, result.stdout + result.stderr
    report_path, = (storage / "runs").glob("*/report.json")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    missing = "test_shop.py::test_case[old_failure]"
    assert report["reference"]["regression"]["tests"][missing]["status"] == "failed"
    validation, = report["validations"]
    assert validation["target"]["status"] == "passed"
    assert validation["regression"]["status"] == "passed"
    assert validation["missing_tests"] == validation["regressions"] == [missing]
    assert validation["status"] == "rejected"
    assert validation["reason"] == "Previously collected regression tests are missing"
    assert not (report_path.parent / "fix.diff").exists()


def test_config_uses_candidate_src(tmp_path, monkeypatch):
    fixture = tmp_path / "fixture"
    source = Path("service/src/demo_shop/__init__.py")
    (fixture / source).parent.mkdir(parents=True)
    (fixture / source).write_text("price = 5\n", encoding="utf-8")
    (fixture / "service" / "settings.py").write_text("prefer_installed = False\n", encoding="utf-8")
    tests = fixture / "service" / "tests"
    tests.mkdir()
    (tests / "test_price.py").write_text(
        "import os\nimport sys\nfrom settings import prefer_installed\n"
        'if prefer_installed:\n    sys.path.insert(0, os.environ["CI_REPAIR_FIXTURE_INSTALLED"])\n'
        "from demo_shop import price\n\ndef test_price():\n    assert price == 9\n", encoding="utf-8",
    )
    (fixture / ".ci-repair.toml").write_text(
        'cwd = "service"\nsource_paths = ["service/src"]\nimport_modules = ["demo_shop"]\nregression = ["tests"]\n',
        encoding="utf-8",
    )
    old_package = tmp_path / "installed" / "demo_shop"
    old_package.mkdir(parents=True)
    (old_package / "__init__.py").write_text("price = 9\n", encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(old_package.parent))
    monkeypatch.setenv("CI_REPAIR_FIXTURE_INSTALLED", str(old_package.parent))
    repo = tmp_path / "repo"
    make_repository(repo, fixture)
    storage = tmp_path / "state"

    doctor = cli(storage, "doctor", "--repo", str(repo))
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
    wrong_patch = tmp_path / "prefer-installed.diff"
    create_patch(repo, source.as_posix(), "price = 9\n", patch)
    create_patch(repo, "service/settings.py", "prefer_installed = True\n", wrong_patch)
    result = cli(storage, "fix", "--repo", str(repo), "--test", "tests/test_price.py::test_price",
                 "--patch", str(wrong_patch), "--patch", str(patch))
    assert result.returncode == 0, result.stdout + result.stderr
    report_path, = (storage / "runs").glob("*/report.json")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["reference"]["target"]["status"] == "failed"
    assert report["reference"]["target"]["imports"]["demo_shop"]["status"] == "in_worktree"
    wrong, correct = report["validations"]
    assert wrong["status"] == "blocked"
    assert wrong["target"]["status"] == "passed"
    assert wrong["target"]["imports"]["demo_shop"] == {
        "status": "outside_worktree", "origins": [str((old_package / "__init__.py").resolve())],
    }
    assert correct["status"] == "accepted"
    assert correct["regression"]["status"] == "passed"
    assert correct["regression"]["imports"]["demo_shop"]["status"] == "in_worktree"
    manifest = json.loads((report_path.parent / report["manifest"]).read_text(encoding="utf-8"))
    assert manifest["cwd"] == diagnosis["cwd"] == "service"
    assert manifest["source_paths"] == diagnosis["source_paths"] == ["service/src"]
    assert manifest["import_modules"] == diagnosis["import_modules"] == ["demo_shop"]
    assert manifest["regression"] == diagnosis["regression"] == ["tests"]

    missing_runtime = cli(
        storage, "fix", "--repo", str(repo), "--test", "tests/test_price.py::test_price",
        "--import", "unobserved_project_fixture", "--patch", str(patch),
    )
    assert missing_runtime.returncode == 2, missing_runtime.stdout + missing_runtime.stderr
    task_id = missing_runtime.stdout.splitlines()[0].split()[-1]
    blocked_report = json.loads((storage / "runs" / task_id / "report.json").read_text(encoding="utf-8"))
    assert blocked_report["status"] == "blocked"
    assert blocked_report["reason"] == "Project imports were not observed: unobserved_project_fixture"


@pytest.mark.parametrize(("directory", "source_directory"), [
    (".", "."), (".", "src"), ("services/shop", "services/shop/src"),
], ids=["flat", "src", "subproject"])
def test_repair_uses_declared_layout(tmp_path, directory, source_directory):
    fixture = tmp_path / "fixture"
    source = Path(source_directory) / "sample_price.py"
    (fixture / source).parent.mkdir(parents=True)
    (fixture / source).write_text("price = 5\n", encoding="utf-8")
    tests = fixture / directory / "tests"
    tests.mkdir(parents=True)
    (fixture / directory / "pytest.ini").write_text("[pytest]\nmarkers = unit: unit case\n", encoding="utf-8")
    (tests / "test_price.py").write_text(
        "import pytest\nfrom sample_price import price\n\n"
        "@pytest.mark.unit\ndef test_price():\n    assert price == 9\n\n"
        "def test_excluded():\n    assert False\n", encoding="utf-8",
    )
    (fixture / ".ci-repair.toml").write_text(
        f'cwd = "{directory}"\nsource_paths = ["{source_directory}"]\n'
        'import_modules = ["sample_price"]\nregression = ["tests"]\npytest_args = ["-m", "unit"]\n', encoding="utf-8",
    )
    repo = tmp_path / "repo"
    make_repository(repo, fixture)
    storage = tmp_path / "state"
    patch = tmp_path / "price.diff"
    create_patch(repo, source.as_posix(), "price = 9\n", patch)
    result = cli(storage, "fix", "--repo", str(repo), "--test", "tests/test_price.py::test_price",
                 "--patch", str(patch))
    assert result.returncode == 0, result.stdout + result.stderr
    report_path, = (storage / "runs").glob("*/report.json")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    baseline = report["reference"]["target"]
    candidate = report["validations"][0]["regression"]
    assert (baseline["status"], candidate["status"], report["status"]) == ("failed", "passed", "accepted")
    assert candidate["collected"] == ["tests/test_price.py::test_price"]
    fingerprint = baseline["runtime"]["environment_fingerprint"]
    assert fingerprint == candidate["runtime"]["environment_fingerprint"]
    manifest = json.loads(report_path.with_name("manifest.json").read_text(encoding="utf-8"))
    assert manifest["pytest_args"] == ["-m", "unit"]
    assert manifest["environment_fingerprint"] == fingerprint
    records = [json.loads((report_path.parent / ref).read_text(encoding="utf-8")) for ref in report["worktrees"]]
    assert {record["purpose"]: record["status"] for record in records} == {"baseline": "removed", "candidate": "removed"}
    assert all(record["commit"] == report["task"]["commit"] for record in records)
    for run in (baseline, candidate):
        imported = run["imports"]["sample_price"]
        origin, = imported["origins"]
        assert imported["status"] == "in_worktree"
        assert Path(origin).relative_to(run["cwd"]) == source.relative_to(directory)


def test_resume_blocks_when_pytest_environment_changes(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    make_repository(repo, PROJECT / "examples/shipping")
    storage = tmp_path / "state"
    wrong, right = tmp_path / "wrong.diff", tmp_path / "right.diff"
    wrong.write_text("invalid patch", encoding="utf-8")
    original = (repo / "shipping.py").read_text(encoding="utf-8")
    create_patch(repo, "shipping.py", original.replace("subtotal >", "subtotal >="), right)
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)
    paused = cli(storage, "fix", "--repo", str(repo), "--test",
                 "test_shipping.py::test_standard_shipping_at_threshold_is_free",
                 "--patch", str(wrong), "--patch", str(right), "--pause-after-candidate")
    assert paused.returncode == 0, paused.stdout + paused.stderr
    report_path, = (storage / "runs").glob("*/report.json")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "paused"

    monkeypatch.setenv("PYTEST_ADDOPTS", "-q")
    resumed = cli(storage, "resume", report["task"]["id"])
    assert resumed.returncode == 2, resumed.stdout + resumed.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "blocked"
    assert report["reason_code"] == "environment_changed"
    assert report["validations"][1]["target"]["status"] == "passed"
    assert not report_path.with_name("fix.diff").exists()


def test_repair_reports_unreproduced_skip_and_missing_dependency(tmp_path):
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "price.py").write_text("price = 5\n", encoding="utf-8")
    (fixture / "test_price.py").write_text(
        "import pytest\nfrom price import price\n\n"
        'def test_price():\n    if price is None:\n        pytest.skip("fixture unavailable")\n    assert price == 9\n\n'
        "def test_positive():\n    assert price > 0\n", encoding="utf-8",
    )
    repo = tmp_path / "repo"
    make_repository(repo, fixture)
    skip, missing = tmp_path / "skip.diff", tmp_path / "missing.diff"
    create_patch(repo, "price.py", "price = None\n", skip)
    create_patch(repo, "price.py", "import missing_pricing_dependency\nprice = 9\n", missing)
    first_storage = tmp_path / "unreproduced"
    unreproduced = cli(first_storage, "fix", "--repo", str(repo), "--test", "test_price.py::test_positive",
                       "--patch", str(skip))
    assert unreproduced.returncode == 2, unreproduced.stdout + unreproduced.stderr
    first_path, = (first_storage / "runs").glob("*/report.json")
    report = json.loads(first_path.read_text(encoding="utf-8"))
    assert report["reason_code"] == "failure_not_reproduced"
    assert report["reference"]["target_outcomes"] == {"test_price.py::test_positive": "passed"}

    storage = tmp_path / "state"
    result = cli(storage, "fix", "--repo", str(repo), "--test", "test_price.py::test_price",
                 "--patch", str(skip), "--patch", str(missing))
    assert result.returncode == 2, result.stdout + result.stderr
    path, = (storage / "runs").glob("*/report.json")
    report = json.loads(path.read_text(encoding="utf-8"))
    skipped, blocked = report["validations"]
    assert skipped["status"] == "rejected"
    assert skipped["target_outcomes"] == {"test_price.py::test_price": "skipped"}
    assert report["status"] == blocked["status"] == "blocked"
    assert report["reason_code"] == blocked["reason_code"] == "missing_dependency"
    assert "missing_pricing_dependency" in report["reason"]
    assert not path.with_name("fix.diff").exists()


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
