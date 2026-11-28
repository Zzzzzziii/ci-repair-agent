"""Run benchmark pytest in a networkless container with a fresh evidence directory."""
from pathlib import Path
import os
import subprocess
from uuid import uuid4

from ci_repair.runner import run_process


def image_id(tag):
    found = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", tag],
                           capture_output=True, text=True, timeout=20)
    if found.returncode:
        raise ValueError("Start Docker and build the benchmark image as described in evals/README.md")
    return found.stdout.strip()


def container_run(image, tree, evidence, args, stdout, stderr, timeout, reporter=None):
    name = "ci-repair-eval-" + uuid4().hex
    command = ["docker", "run", "--rm", "--name", name, "--network", "none", "--read-only",
               "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--tmpfs", "/tmp",
               "--pids-limit", "128", "--memory", "512m", "--cpus", "1", "--workdir", "/subject",
               "--mount", f"type=bind,source={tree.resolve()},target=/subject,readonly",
               "--mount", f"type=bind,source={evidence.resolve()},target=/out",
               "-e", "PYTHONPATH=/subject:/subject/src", "-e", "PYTHONDONTWRITEBYTECODE=1",
               "-e", "LANGUAGE=en", "-e", "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1"]
    if os.name == "posix":
        command.extend(["--user", f"{os.getuid()}:{os.getgid()}"])
    if reporter:
        command.extend(["--mount", f"type=bind,source={reporter.resolve()},target=/pytest_reporter.py,readonly"])
    try:
        return run_process([*command, image, "python", *args], cwd=tree, env=None,
                           stdout_path=stdout, stderr_path=stderr, timeout=timeout)
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=15)


def reporter_runner(image):
    def run(command, *, cwd, env, stdout_path, stderr_path, timeout):
        worker, report = map(Path, command[4:6])
        return container_run(image, cwd, report.parent,
                             ["-B", "-X", "utf8", "/pytest_reporter.py", "/out/pytest.json", *command[6:]],
                             stdout_path, stderr_path, timeout, reporter=worker)
    return run
