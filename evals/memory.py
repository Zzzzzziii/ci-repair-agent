"""Offline counterfactual: replay a verified old diff against an identical target failure."""

import argparse
import json
from pathlib import Path

from run import prepare, run_repair
from ci_repair.memory import RepairMemory
from ci_repair.project import repository_id


def compare(output):
    broken = "def calculate(value):\n    return 12\n"
    correct = "def calculate(value):\n    return 0 if value >= 200 else 12\n"
    results = []
    for mode in ("off", "applicable", "misleading"):
        directory = output / mode
        storage = directory / "state"
        old_rule = "def calculate(value):\n    return 0\n" if mode == "misleading" else correct
        historical = ("history", broken, old_rule, (200, 0), (100, 0 if mode == "misleading" else 12), (201, 0))
        repo, patch, _ = prepare(directory / "old", historical)
        seed, _ = run_repair(storage, repo, "batch", None, patch, True)
        if not seed or json.loads(seed.read_text(encoding="utf-8"))["status"] != "accepted":
            raise ValueError("Historical patch failed its own verification")
        current = ("current", broken, correct, (200, 0), (199, 12), (201, 0))
        repo, reference, _ = prepare(directory / "new", current, repo)
        history = [] if mode == "off" else RepairMemory(storage / "repairs.sqlite").search(
            repository_id(repo), "test_app.py::test_reported_behavior assert calculate(200) == 0")
        if mode != "off" and not history:
            raise ValueError("Expected to retrieve the verified seed")
        selected = storage / Path(history[0]["source_report"]).with_name("fix.diff") if history else reference
        path, _ = run_repair(storage, repo, "batch", None, selected, False)
        report = json.loads(path.read_text(encoding="utf-8"))
        verdict, = report["validations"]
        results.append({"mode": mode, "source": history[0]["source_task"] if history else None,
                        "target": verdict["target"]["status"], "outcome": verdict["status"],
                        "regressions": verdict.get("regressions", []), "report": path.relative_to(output).as_posix()})
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    output = parser.parse_args().output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    results = compare(output)
    (output / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps(results, indent=2))
    raise SystemExit([row["outcome"] for row in results] != ["accepted", "accepted", "rejected"])
