"""Check the external judge using a tiny upstream history with a known overfit."""
import importlib
import json
from pathlib import Path

import pytest

from ci_repair.workspace import git


@pytest.fixture
def benchmark(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "evals"))
    return importlib.import_module("real")


def test_reference_is_private_and_overfit_fails_hidden_check(tmp_path, benchmark, monkeypatch):
    from manifest import BenchmarkCase
    from prepare import git_bytes, prepare

    cache = tmp_path / "cache"
    upstream = cache / "local.git"
    pkg = upstream / "prices"
    pkg.mkdir(parents=True)
    git(upstream, "init", "-q")
    git(upstream, "config", "user.name", "Example")
    git(upstream, "config", "user.email", "example@example.invalid")
    source = pkg / "__init__.py"
    source.write_text("def double(x):\n    return 0\n", encoding="utf-8")
    git(upstream, "add", ".")
    git(upstream, "commit", "-qm", "Baseline")
    bad = git(upstream, "rev-parse", "HEAD").strip()
    source.write_text("def double(x):\n    return x * 2\n", encoding="utf-8")
    (upstream / "future.txt").write_text("Private reference", encoding="utf-8")
    git(upstream, "add", ".")
    git(upstream, "commit", "-qm", "Official fix")
    fix = git(upstream, "rev-parse", "HEAD").strip()
    date = git(upstream, "show", "-s", "--format=%cs", fix).strip()
    case = BenchmarkCase("case-00", "evaluation", "example/local", bad, fix, date, "", "prices",
                         "snapshot_only", "cpython3.12-pytest", ["prices/__init__.py:double"],
                         "from prices import double", "assert double(3) == 6", "assert double(0) == 0",
                         "assert double(5) == 10")
    folder = tmp_path / case.id
    row = benchmark.calibrate(case, cache, folder)
    assert row["calibrated"], row
    view = folder / "repo"
    assert git(view, "rev-parse", "HEAD:prices/__init__.py") == git(upstream, "rev-parse", bad + ":prices/__init__.py")
    assert git(view, "rev-list", "--all", "--count").strip() == "1"
    assert not git(view, "remote").strip()
    assert not {"test_private.py", "future.txt"}.intersection(git(view, "ls-files").splitlines())
    with pytest.raises(ValueError):
        git(view, "cat-file", "-e", fix)
    another, _ = prepare(case, cache, tmp_path / "second")
    assert git(another, "rev-parse", "HEAD").strip() == row["snapshot"]
    (view / "prices/__init__.py").write_text("def double(x):\n    return 6 if x == 3 else 0\n", encoding="utf-8")
    patch = git_bytes(view, "diff")
    assert "assert double(5)" not in row["original"]["public_failure"]
    git(view, "restore", "prices/__init__.py")

    # Run the normal model-driven graph with a known, locally authored answer.
    from ci_repair import cli, model
    from langchain_core.messages import AIMessage

    class Script:
        def bind_tools(self, bindings):
            names = [b.name for b in bindings if hasattr(b, "name")]
            assert "read_file" in names and "retrieve_code" not in names
            return self

        def invoke(self, messages):
            return AIMessage(content="", response_metadata={"model_name": "script-fixture"},
                             tool_calls=[{"id": "patch", "name": "submit_patch",
                                          "args": {"hypothesis": "Handle the public case", "diff": patch.decode(),
                                                   "evidence": ["prices/__init__.py:1"]}}])

    def local_agent(view, storage, mode, name, image):
        assert cli.main(benchmark.agent_args(view, storage, mode, name)) == 0
        report, = (storage / "runs").glob("*/report.json")
        return report, json.loads(report.read_text(encoding="utf-8")), 0

    monkeypatch.setattr(model, "init_chat_model", lambda *a, **kw: Script())
    monkeypatch.setattr(benchmark, "run_agent", local_agent)
    result = benchmark.attempt(case, row, "tools", "fixture", 1, tmp_path, [], None)
    assert result["status"] == "false_accept", result
    assert result["independent"]["tests"] == {"test_target": "passed", "test_regression": "passed", "test_hidden": "failure"}
    assert result["served_models"] == ["script-fixture"]
    assert result["resources"]["model_calls"] == 1


def test_denominators_include_environment_failures(benchmark):
    records = [
        {"case": "a", "status": "solved", "accepted": True, "calibrated": True},
        {"case": "b", "status": "false_accept", "accepted": True, "calibrated": True},
        {"case": "c", "status": "independent_check_error", "accepted": True, "calibrated": True},
        {"case": "d", "status": "environment_error", "calibrated": False},
    ]
    summary = benchmark.summarize([dict(r, split="evaluation", mode="tools") for r in records])["evaluation/tools"]
    assert summary["reproduction"]["rate"] == 3 / 4
    assert summary["solved_all"]["rate"] == 1 / 4
    assert summary["solved_reproducible"]["rate"] == 1 / 3
    assert summary["false_accept"]["rate"] == 1 / 2
    assert summary["accepted_unjudged"] == 1


def test_fixed_dataset(benchmark):
    cases = benchmark.load_cases()
    assert len({case.repository for case in cases}) >= 3
    assert len([case for case in cases if case.split == "evaluation"]) >= 15


def test_benchmark_test_container_mounts_only_its_inputs(tmp_path, benchmark, monkeypatch):
    import isolated
    observed = []
    monkeypatch.setattr(isolated, "run_process", lambda cmd, **kw: observed.append(cmd) or {"exit_code": 0})
    monkeypatch.setattr(isolated.subprocess, "run", lambda *args, **kwargs: None)
    report = tmp_path / "evidence" / "pytest.json"
    worker = tmp_path / "pytest_reporter.py"
    tree = tmp_path / "public"
    run = isolated.reporter_runner("test-image")
    run(["python", "-B", "-X", "utf8", str(worker), str(report), "{}", "test_public.py"],
        cwd=tree, env={"DEEPSEEK_API_KEY": "private-marker"}, stdout_path=report.with_suffix(".out"),
        stderr_path=report.with_suffix(".err"), timeout=30)
    cmd, = observed
    assert cmd[cmd.index("--network") + 1] == "none"
    assert cmd[-5:] == ["utf8", "/pytest_reporter.py", "/out/pytest.json", "{}", "test_public.py"]
    assert not any("private-marker" in arg for arg in cmd)
    assert [arg.split(",target=")[1] for arg in cmd if arg.startswith("type=bind,")] == [
        "/subject,readonly", "/out", "/pytest_reporter.py,readonly"]
