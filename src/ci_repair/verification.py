from pathlib import Path, PurePosixPath

from ci_repair.pytest_adapter import PytestAdapter
from ci_repair.workspace import apply_patch, within, worktree


VALID_TEST_RUN = {"passed", "failed"}


def execution_problem(run: dict, label: str) -> dict[str, str]:
    kind = run.get("failure_kind") or run["status"]
    modules = sorted({item["module"] for item in run.get("missing_dependencies", [])})
    detail = f" ({', '.join(modules)})" if modules else ""
    return {"reason_code": kind, "reason": f"{label}: {kind}{detail}"}


def import_problem(*runs: dict, require_all: bool = False) -> str | None:
    outcomes = {}
    for run in runs:
        for name, record in run.get("imports", {}).items():
            outcomes.setdefault(name, set()).add(record["status"])

    outside = sorted(name for name, seen in outcomes.items() if "outside_worktree" in seen)
    if outside:
        return f"Project imports came from outside the worktree: {', '.join(outside)}"
    if require_all:
        missing = sorted(name for name, seen in outcomes.items() if "in_worktree" not in seen)
        if missing:
            return f"Project imports were not observed: {', '.join(missing)}"
    return None


def baseline(adapter: PytestAdapter, task: dict, scratch: Path) -> dict:
    with worktree(Path(task["repository"]), task["commit"], scratch) as tree:
        directory = within(tree, task["cwd"])
        target = adapter.run(directory, task["targets"], tree=tree)
        if target["status"] not in VALID_TEST_RUN:
            return {"target": target, **execution_problem(target, "Target execution")}
        if problem := import_problem(target):
            return {"target": target, "reason": problem, "reason_code": "import_origin"}
        outcomes = {node: status_of(target, node) for node in task["targets"]}
        missing = [node for node, outcome in outcomes.items() if outcome != "failed"]
        if missing:
            return {
                "target": target, "target_outcomes": outcomes, "reason_code": "failure_not_reproduced",
                "reason": f"Failure not reproduced for: {', '.join(missing)}",
            }
        regression = adapter.run(directory, task["regression"], tree=tree)
        reference = {"target": target, "regression": regression}
        if regression["status"] not in VALID_TEST_RUN:
            return {**reference, **execution_problem(regression, "Regression baseline unavailable")}
        if problem := import_problem(target, regression, require_all=True):
            return {**reference, "reason": problem, "reason_code": "import_origin"}
        return {**reference, "reason": None}


def status_of(run: dict, node: str) -> str:
    return run["tests"].get(node, {}).get("status", "missing")


def collected_tests(run: dict) -> set[str] | None:
    collected = run.get("collected")
    if collected is None and not run["tests"]:
        return None
    return set(collected or ()).union(run["tests"])


def verify(adapter: PytestAdapter, task: dict, reference: dict, patch: str, scratch: Path) -> dict:
    with worktree(Path(task["repository"]), task["commit"], scratch) as tree:
        directory = within(tree, task["cwd"])
        test_paths = [
            (PurePosixPath(task["cwd"]) / node.partition("::")[0]).as_posix()
            for run in (reference["target"], reference["regression"])
            for node in run["tests"]
        ]
        try:
            actual_diff = apply_patch(tree, patch, test_paths)
        except (ValueError, OSError) as error:
            return {"status": "rejected", "reason": str(error)}
        target = adapter.run(directory, task["targets"], tree=tree)
        if target["status"] not in VALID_TEST_RUN:
            return {"status": "blocked", "target": target, **execution_problem(target, "Target execution")}
        if problem := import_problem(target):
            return {"status": "blocked", "reason": problem, "reason_code": "import_origin", "target": target}
        target_outcomes = {node: status_of(target, node) for node in task["targets"]}
        if target["status"] != "passed" or any(outcome != "passed" for outcome in target_outcomes.values()):
            return {
                "status": "rejected", "reason": "Target tests did not all pass",
                "reason_code": "target_not_passed", "target": target, "target_outcomes": target_outcomes,
            }

        regression = adapter.run(directory, task["regression"], tree=tree)
        if regression["status"] not in VALID_TEST_RUN:
            return {
                "status": "blocked", "target": target, "regression": regression,
                **execution_problem(regression, "Regression execution"),
            }
        if problem := import_problem(target, regression, require_all=True):
            return {
                "status": "blocked", "reason": problem, "reason_code": "import_origin",
                "target": target, "regression": regression,
            }
        baseline_collection = collected_tests(reference["regression"])
        candidate_collection = collected_tests(regression)
        missing_tests = (
            sorted(baseline_collection - candidate_collection)
            if baseline_collection is not None and candidate_collection is not None else []
        )
        unexecuted_tests = sorted(node for node, record in regression["tests"].items() if record["status"] == "not_run")
        regressions = [
            node for node, record in reference["regression"]["tests"].items()
            if record["status"] == "passed" and status_of(regression, node) != "passed"
        ]
        remaining_failures = [
            node for node, record in regression["tests"].items() if record["status"] == "failed"
        ]
        new_failures = [
            node for node in remaining_failures if status_of(reference["regression"], node) != "failed"
        ]
        result = {
            "status": "rejected",
            "target": target,
            "regression": regression,
            "regressions": sorted(set(regressions + new_failures + missing_tests + unexecuted_tests)),
            "remaining_failures": remaining_failures,
            "missing_tests": missing_tests,
            "unexecuted_tests": unexecuted_tests,
        }
        if missing_tests:
            result.update(reason="Previously collected regression tests are missing", reason_code="missing_tests")
        elif unexecuted_tests:
            result.update(reason="Collected regression tests did not run", reason_code="unexecuted_tests")
        elif result["regressions"]:
            result.update(reason="New regression failures or previously passing tests did not pass", reason_code="regression")
        else:
            result.update(status="accepted", diff=actual_diff)
        return result
