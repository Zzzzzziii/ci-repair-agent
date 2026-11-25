import sys
from time import sleep

from ci_repair import runner


def test_runner_preserves_large_logs_and_exit_code(tmp_path):
    stdout = tmp_path / "stdout.log"
    stderr = tmp_path / "stderr.log"
    script = "import sys; print('START'); print('x' * 50000); print('details', file=sys.stderr); sys.exit(3)"
    result = runner.run_process(
        [sys.executable, "-c", script],
        cwd=tmp_path, env=None, stdout_path=stdout, stderr_path=stderr, timeout=15,
    )
    assert result["exit_code"] == 3
    assert not result["timed_out"]
    assert stdout.read_text(encoding="utf-8").startswith("START\n")
    assert stderr.read_text(encoding="utf-8") == "details\n"
    assert len(result["output"]) == runner.OUTPUT_LIMIT
    assert "START" not in result["output"]
    assert "details" in result["output"]


def test_timeout_stops_child_process_and_keeps_output(tmp_path):
    stdout = tmp_path / "stdout.log"
    stderr = tmp_path / "stderr.log"
    child = "import time\nfor _ in range(200):\n    print('child heartbeat', flush=True)\n    time.sleep(0.05)"
    parent = (
        "import subprocess, sys\n"
        "print('parent started', flush=True)\n"
        f"subprocess.Popen([sys.executable, '-c', {child!r}]).wait()\n"
    )
    result = runner.run_process(
        [sys.executable, "-c", parent], cwd=tmp_path, env=None,
        stdout_path=stdout, stderr_path=stderr, timeout=2,
    )

    assert result["timed_out"]
    assert result["exit_code"] is None
    assert result["cleanup_error"] is None, result
    assert "parent started" in result["output"]
    assert "child heartbeat" in result["output"]
    length = stdout.stat().st_size
    sleep(0.35)
    assert stdout.stat().st_size == length


def test_reports_cleanup_error_after_stopping_parent(tmp_path, monkeypatch):
    def fail_tree_cleanup(process):
        raise PermissionError("Process tree could not be stopped")

    monkeypatch.setattr(runner, "terminate_process_tree", fail_tree_cleanup)
    result = runner.run_process(
        [sys.executable, "-c", "import time; time.sleep(2)"], cwd=tmp_path, env=None,
        stdout_path=tmp_path / "stdout.log", stderr_path=tmp_path / "stderr.log", timeout=0.1,
    )
    assert result["timed_out"]
    assert result["cleanup_error"] == "Process tree could not be stopped"
