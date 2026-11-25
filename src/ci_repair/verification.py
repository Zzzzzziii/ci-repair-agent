from pathlib import Path, PurePosixPath

from ci_repair.pytest_adapter import PytestAdapter
from ci_repair.workspace import apply_patch, within, worktree


VALID_TEST_RUN = {"passed", "failed"}


def baseline(adapter: PytestAdapter, task: dict, scratch: Path) -> dict:
    with worktree(Path(task["repository"]), task["commit"], scratch) as tree:
        directory = within(tree, task["cwd"])
        target = adapter.run(directory, task["targets"], tree=tree)
        if target["status"] not in VALID_TEST_RUN:
            return {"target": target, "reason": f"Target execution: {target['status']}"}
        missing = [node for node in task["targets"] if status_of(target, node) != "failed"]
        if missing:
            return {"target": target, "reason": f"Failure not reproduced for: {', '.join(missing)}"}
        regression = adapter.run(directory, task["regression"], tree=tree)
        return {
            "target": target,
            "regression": regression,
            "reason": None if regression["status"] in VALID_TEST_RUN else
            f"Regression baseline unavailable: {regression['status']}",
        }


def status_of(run: dict, node: str) -> str:
    return run["tests"].get(node, {}).get("status", "missing")


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
        if target["status"] != "passed" or any(
            status_of(target, node) != "passed" for node in task["targets"]
        ):
            return {"status": "rejected", "reason": "Target tests did not all pass", "target": target}

        regression = adapter.run(directory, task["regression"], tree=tree)
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
            "regressions": sorted(set(regressions + new_failures)),
            "remaining_failures": remaining_failures,
        }
        if regression["status"] not in VALID_TEST_RUN:
            result["reason"] = f"Regression execution: {regression['status']}"
        elif result["regressions"]:
            result["reason"] = "New regression failures or previously passing tests did not pass"
        else:
            result.update(status="accepted", diff=actual_diff)
        return result
