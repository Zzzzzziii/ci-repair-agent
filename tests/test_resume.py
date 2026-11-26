"""The worker exits without unwinding the graph or publishing a final report."""
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Thread
from time import sleep

import pytest

from ci_repair.cli import main
from ci_repair.workspace import git


def worker(directory, fault, args):
    from ci_repair import model, workflow
    from langchain_core.messages import AIMessage, ToolMessage

    class Script:
        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            if isinstance(messages[-1], ToolMessage):
                name, arguments = "submit_patch", {"hypothesis": "Adjust the fee", "evidence": ["shop.py:1"],
                                                   "diff": (directory / "fix.diff").read_text()}
            else:
                name, arguments = "read_file", {"path": "shop.py"}
            return AIMessage(content="", tool_calls=[{"name": name, "args": arguments, "id": name}])

    model.init_chat_model = lambda *args, **kwargs: Script()
    original = workflow.append_workflow_event
    decisions = 0

    def interrupt(*args, **details):
        nonlocal decisions
        original(*args, **details)
        if args[2] != "node_started":
            return
        name = details["node"]
        if name == "model_step":
            decisions += 1
        if ((fault == "after_tool" and decisions == 2 and name == "model_step")
                or (fault == "after_apply" and name == "test_target")
                or (fault == "after_test" and name == "test_related")):
            os._exit(73)
        if fault == "during_test" and name == "test_target":
            marker = directory / "in-test"
            os.environ["REPAIR_TEST_MARKER"] = str(marker)

            def watch():
                while not marker.exists():
                    sleep(0.02)
                os._exit(73)
            Thread(target=watch, daemon=True).start()

    workflow.append_workflow_event = interrupt
    return main(["--state-dir", str(directory / "state"), *args])


def invoke(directory, fault, *args):
    return subprocess.run([sys.executable, __file__, str(directory), fault, *args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=90)


@pytest.mark.parametrize("fault,next_node", [("after_tool", "model_step"), ("after_apply", "test_target"),
                                             ("during_test", "test_target"), ("after_test", "test_related")])
def test_resume_after_process_exit(tmp_path, fault, next_node):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "--quiet")
    source = repo / "shop.py"
    source.write_text("fee = 0\n")
    (repo / "test_shop.py").write_text(
        "import os, time\nfrom pathlib import Path\nfrom shop import fee\n"
        "def test_fee():\n    if marker := os.getenv('REPAIR_TEST_MARKER'):\n"
        "        Path(marker).touch()\n        time.sleep(0.8)\n    assert fee == 10\n"
        "def test_other(): assert fee >= 0\n"
    )
    git(repo, "add", ".")
    git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture")
    source.write_text("fee = 10\n")
    (tmp_path / "fix.diff").write_text(git(repo, "diff"))
    source.write_text("fee = 0\n")
    stopped = invoke(tmp_path, fault, "fix", "--repo", str(repo), "--model", "script", "--strategy", "sequential",
                     "--test", "test_shop.py::test_fee")
    assert stopped.returncode == 73, stopped.stdout + stopped.stderr
    run, = (tmp_path / "state/runs").iterdir()
    status = json.loads(invoke(tmp_path, "", "status", run.name).stdout)
    assert status["status"] == "interrupted" and status["next"] == [next_node]
    args = []
    if fault == "after_apply":
        different = tmp_path / "other"
        git(tmp_path, "clone", "--quiet", str(repo), str(different))
        refused = invoke(tmp_path, "", "resume", run.name, "--repo", str(different))
        assert refused.returncode == 2 and "repository_mismatch" in refused.stderr
        moved = tmp_path / "moved"
        repo.rename(moved)
        args = ["--repo", str(moved)]
    if fault == "after_test":
        # Corrupt already completed target evidence, then restore the original bytes.
        target = next(p for p in (run / "artifacts/pytest").glob("*/pytest.json")
                      if json.loads(p.read_text(encoding="utf-8"))["phases"]["test_shop.py::test_fee"]["call"]["status"] == "passed")
        original = target.read_bytes()
        target.write_bytes(b"{}")
        refused = invoke(tmp_path, "", "resume", run.name)
        assert refused.returncode == 2 and "Evidence missing or changed" in refused.stderr
        target.write_bytes(original)
    resumed = invoke(tmp_path, "", "resume", run.name, *args)
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    report = json.loads((run / "report.json").read_text(encoding="utf-8"))
    assert report["status"] == "accepted"
    assert len(report["tool_events"]) == 1
    counts = report["budget"]["reservations"]
    assert len(counts["model_calls"]) == 2 and len(counts["candidates"]) == 1
    assert len(counts["tests"]) == (6 if fault == "during_test" else 5)
    assert any(json.loads((run / path).read_text())["next"] == [next_node] for path in report["recovery"])


if __name__ == "__main__":
    raise SystemExit(worker(Path(sys.argv[1]), sys.argv[2], sys.argv[3:]))
