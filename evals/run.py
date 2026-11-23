"""Run a reproducible patch-verification smoke test or compare live repair strategies."""

import argparse
import json
from pathlib import Path
import subprocess
import sys
from time import monotonic

from ci_repair.pytest_adapter import PytestAdapter
from ci_repair.workspace import git, worktree


def cases(boundaries=(20, 50, 100, 200)):
    for boundary in boundaries:
        yield (
            f"threshold-{boundary}",
            f"def calculate(value):\n    return 0 if value > {boundary} else 12\n",
            f"def calculate(value):\n    return 0 if value >= {boundary} else 12\n",
            (boundary, 0), (boundary - 1, 12), (boundary + 1, 0),
        )
        yield (
            f"discount-{boundary}",
            "from rules import fee\n\ndef calculate(value):\n    return value - 5 + fee(value - 5)\n",
            "from rules import fee\n\ndef calculate(value):\n    return value - 5 + fee(value)\n",
            (boundary, boundary - 5), (boundary - 1, boundary + 6), (boundary + 1, boundary - 4),
        )
        yield (
            f"count-{boundary}",
            "def calculate(value):\n    return max(0, len(value) - 1)\n",
            "def calculate(value):\n    return len(value)\n",
            ("x" * boundary, boundary), ("", 0), ("x", 1),
        )
        yield (
            f"batches-{boundary}",
            f"def calculate(value):\n    return value // {boundary}\n",
            f"def calculate(value):\n    return (value + {boundary} - 1) // {boundary}\n",
            (boundary + 1, 2), (boundary, 1), (boundary - 1, 1),
        )
        yield (
            f"zero-price-{boundary}",
            f"def calculate(value):\n    return value or {boundary}\n",
            f"def calculate(value):\n    return {boundary} if value is None else value\n",
            (0, 0), (None, boundary), (boundary + 1, boundary + 1),
        )


def write(path, content):
    path.write_text(content, encoding="utf-8", newline="\n")


def test_code(name, case):
    argument, expected = case
    return f"def test_{name}():\n    assert calculate({argument!r}) == {expected!r}\n"


def prepare(directory, case, repository=None):
    name, broken, fixed, target, regression, holdout = case
    directory.mkdir(parents=True, exist_ok=True)
    if repository is None:
        repository = directory / "repo"
        repository.mkdir()
        git(repository, "init", "--quiet")
    code = repository / "app.py"
    write(code, broken)
    if name.startswith("discount-"):
        threshold = int(name.partition("-")[2])
        write(repository / "rules.py", f"def fee(value):\n    return 0 if value >= {threshold} else 12\n")
    write(repository / "test_app.py", "from app import calculate\n\n" +
          test_code("reported_behavior", target) + "\n" + test_code("existing_behavior", regression))
    git(repository, "add", ".")
    git(repository, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "--quiet", "-m", "Failing baseline")
    write(code, fixed)
    patch = directory / "reference.diff"
    write(patch, git(repository, "diff", "--", "app.py"))
    write(code, broken)
    return repository, patch, "from app import calculate\n\n" + test_code("held_out_behavior", holdout)


def run_repair(storage, repository, strategy, model, patch, use_memory):
    command = [
        sys.executable, "-c", "from ci_repair.cli import main; raise SystemExit(main())",
        "--state-dir", str(storage), "fix", "--repo", str(repository), "--test",
        "test_app.py::test_reported_behavior", "--strategy", strategy,
    ]
    if not use_memory:
        command.append("--no-memory")
    command.extend(["--model", model] if model else ["--patch", str(patch)])
    start = monotonic()
    process = subprocess.run(command, capture_output=True, encoding="utf-8", errors="replace", timeout=900)
    task = next((line.removeprefix("Task ID: ") for line in process.stdout.splitlines()
                 if line.startswith("Task ID: ")), None)
    return storage / "runs" / task / "report.json" if task else None, process.stderr


def run_case(directory, case, strategy, model, use_memory=False):
    storage = directory / "state"
    repository = None
    if use_memory:
        family = case[0].rsplit("-", 1)[0]
        seed = next(item for item in cases((10,)) if item[0] == f"{family}-10")
        repository, seed_patch, _ = prepare(directory / "seed", seed)
        seeded, detail = run_repair(storage, repository, "batch", None, seed_patch, True)
        if not seeded or json.loads(seeded.read_text(encoding="utf-8"))["status"] != "accepted":
            return {"case": case[0], "strategy": strategy, "memory": True,
                    "status": "blocked", "detail": f"Historical seed failed: {detail[-700:]}"}
    repository, patch, hidden = prepare(directory, case, repository)
    start = monotonic()
    report_path, detail = run_repair(storage, repository, strategy, model, patch, use_memory)
    if not report_path or not report_path.is_file():
        return {"case": case[0], "strategy": strategy, "memory": use_memory,
                "status": "blocked", "detail": detail[-1000:]}
    report = json.loads(report_path.read_text(encoding="utf-8"))
    result = {
        "case": case[0], "strategy": strategy, "status": report["status"],
        "memory": use_memory,
        "history_matches": len(report.get("history", [])),
        "tool_calls": len(report.get("tool_trace", [])),
        "seconds": round(monotonic() - start, 2),
        "candidates_tested": len(report.get("validations", [])),
        "regression_rejections": sum(bool(item.get("regressions")) for item in report.get("validations", [])),
        "pytest_invocations": sum(
            key in stage for stage in [report.get("reference", {}), *report.get("validations", [])]
            for key in ("target", "regression")
        ),
        "tokens": (sum(usage["total_tokens"] for usage in report["model_usage"])
                   if report.get("model_usage") and all("total_tokens" in usage for usage in report["model_usage"])
                   else None),
        "held_out_passed": False,
    }
    if report["status"] == "accepted":
        diff = report_path.with_name("fix.diff").read_text(encoding="utf-8")
        with worktree(repository, report["task"]["commit"], directory) as tree:
            git(tree, "apply", "-", stdin=diff)
            write(tree / "test_holdout.py", hidden)
            adapter = PytestAdapter(Path(sys.executable), directory, 120)
            tests = adapter.run(tree, ["test_holdout.py"])
            result["held_out_passed"] = tests["tests"].get(
                "test_holdout.py::test_held_out_behavior", {}
            ).get("status") == "passed" and tests["status"] == "passed"
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New evaluation directory")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--model", help="Run live batch and sequential comparisons; omit for an offline smoke test")
    parser.add_argument("--compare-memory", action="store_true", help="Also run batch with verified historical seeds")
    args = parser.parse_args()
    if not 1 <= args.limit <= 20:
        parser.error("--limit must be between 1 and 20")
    if args.compare_memory and not args.model:
        parser.error("--compare-memory requires --model")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    results = []
    modes = [("batch", False)]
    if args.model:
        modes.append(("sequential", False))
    if args.compare_memory:
        modes.append(("batch", True))
    for case in list(cases())[:args.limit]:
        for strategy, memory in modes:
            group = f"{strategy}-memory" if memory else strategy
            result = run_case(output / group / case[0], case, strategy, args.model, memory)
            results.append(result)
            print(json.dumps(result, ensure_ascii=False), flush=True)
            write(output / "results.json", json.dumps(results, ensure_ascii=False, indent=2))
    summary = []
    for strategy, memory in modes:
        group = [result for result in results
                 if result["strategy"] == strategy and result["memory"] == memory]
        summary.append({
            "strategy": strategy, "memory": memory, "cases": len(group),
            "accepted": sum(result["status"] == "accepted" for result in group),
            "held_out_passed": sum(result.get("held_out_passed", False) for result in group),
            "regression_rejections": sum(result.get("regression_rejections", 0) for result in group),
        })
    write(output / "summary.json", json.dumps(summary, ensure_ascii=False, indent=2))
    return int(not args.model and not all(result["held_out_passed"] for result in results))


if __name__ == "__main__":
    sys.exit(main())
