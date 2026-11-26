"""Composite Action entry point. Only the agent lives in the tool virtualenv."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory


def arguments(env, receipt):
    repo = Path(env["REPAIR_REPO"]).resolve()
    sha = env["REPAIR_SHA"].lower()
    if not re.fullmatch("[a-f0-9]{40}", sha):
        raise ValueError("failed-sha must be a full commit SHA")
    head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    if head != sha:
        raise ValueError("Check out the commit used by the failing test job before starting repair")
    python = shutil.which(env["REPAIR_PYTHON"])
    if not python:
        raise ValueError("target-python could not be found")
    args = ["--state-dir", str(Path(env["REPAIR_STATE"]).resolve()), "--result-file", str(receipt),
            "fix", "--repo", str(repo), "--reuse-task", "--commit", sha, "--log", env["REPAIR_LOG"],
            "--python", str(Path(python).absolute())]
    if not env.get("REPAIR_PATCH"):
        args += ["--strategy", "sequential"]
    for key, flag in (("REPAIR_CONFIG", "--config"), ("REPAIR_MODEL", "--model"),
                      ("REPAIR_PATCH", "--patch"), ("REPAIR_SEED", "--seed-patch")):
        if env.get(key):
            args += [flag, env[key]]
    for selector in env.get("REPAIR_REGRESSION", "").splitlines():
        if selector.strip():
            args += ["--regression", selector.strip()]
    budgets = json.loads(env.get("REPAIR_BUDGETS") or "{}")
    if not isinstance(budgets, dict):
        raise ValueError("budgets must be a JSON object")
    for key, value in budgets.items():
        if key not in ("model_calls", "tool_calls", "candidates", "tests", "seconds") or type(value) is not int or value < 0:
            raise ValueError("Invalid budget name or value")
        args += ["--max-" + key.replace("_", "-"), str(value)]
    if env.get("REPAIR_PAUSE", "false").lower() == "true":
        args.append("--pause-after-candidate")
    return args


def main():
    env = os.environ
    with TemporaryDirectory(prefix="ci-repair-tool-", dir=env["RUNNER_TEMP"]) as folder:
        root = Path(folder)
        receipt = root / "result.json"
        args = arguments(env, receipt)
        subprocess.run([sys.executable, "-I", "-m", "venv", str(root / "venv")], check=True)
        bin_dir = root / "venv" / ("Scripts" if os.name == "nt" else "bin")
        python = bin_dir / ("python.exe" if os.name == "nt" else "python")
        subprocess.run([str(python), "-I", "-m", "pip", "install", "--disable-pip-version-check",
                        str(Path(__file__).resolve().parents[1])], check=True)
        child_env = dict(env)
        for name in ("GITHUB_OUTPUT", "GITHUB_ENV", "GITHUB_PATH", "GITHUB_STEP_SUMMARY", "GITHUB_STATE"):
            child_env.pop(name, None)
        child_env["CI_REPAIR_ACTION"] = "1"
        run = subprocess.run([str(bin_dir / ("ci-repair.exe" if os.name == "nt" else "ci-repair")), *args], env=child_env)
        if receipt.exists():
            result = json.loads(receipt.read_text(encoding="utf-8"))
            report = Path(result["report"])
            if report.exists():
                result["status"] = json.loads(report.read_text(encoding="utf-8"))["status"]
                if result["status"] == "accepted":
                    result["patch"] = str(report.with_name("fix.diff"))
            with Path(env["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as stream:
                for key in ("task_id", "status", "report", "markdown", "patch"):
                    value = result.get(key, "")
                    if "\n" in value or "\r" in value:
                        raise ValueError("Invalid newline in Action output")
                    print(f"{key}={value}", file=stream)
        return run.returncode


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        sys.exit(str(error))
