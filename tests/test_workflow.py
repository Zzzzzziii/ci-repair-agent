import json

import pytest

from ci_repair import budget
from ci_repair.budget import Budget, BudgetExhausted, DEFAULTS
from ci_repair.sequential import identify
from ci_repair.cli import main


def test_reservations_and_unknown_usage_survive_restart(tmp_path):
    limits = {**DEFAULTS, "model_calls": 2, "candidates": 1, "tests": 0}
    first = Budget(tmp_path, limits)
    first.reserve_call("candidates", "same-patch")
    interrupted = first.reserve_call("model_calls")
    finished = first.reserve_call("model_calls")
    # Simulate a crash after the model-call artifact was saved and before updating budget.json.
    directory = tmp_path / "artifacts/model"
    directory.mkdir(parents=True)
    (directory / f"{finished}.json").write_text(json.dumps({
        "status": "succeeded", "usage": {"total_tokens": 42},
    }), encoding="utf-8")
    resumed = Budget(tmp_path, DEFAULTS)
    assert resumed.state["limits"] == limits
    assert resumed.state["reservations"]["model_calls"][interrupted]["usage"] is None
    assert resumed.state["reservations"]["model_calls"][finished]["usage"] == {"total_tokens": 42}
    resumed.reserve_call("candidates", "same-patch")
    for kind in ("model_calls", "candidates", "tests"):
        with pytest.raises(BudgetExhausted, match=kind):
            resumed.reserve_call(kind)


def test_deadline_also_applies_after_resume(tmp_path, monkeypatch):
    monkeypatch.setattr(budget, "time", lambda: 100)
    Budget(tmp_path, {**DEFAULTS, "seconds": 5})
    monkeypatch.setattr(budget, "time", lambda: 105)
    with pytest.raises(BudgetExhausted, match="seconds"):
        Budget(tmp_path, DEFAULTS).reserve_call("tool_calls")


def test_candidate_identity_preserves_significant_diff_whitespace():
    first = identify({"diff": "+    return 1\r\n"}, "base", None)
    retry = identify({"diff": "+    return 1\n"}, "base", "parent")
    assert first["id"] == retry["id"]
    assert first["id"] != identify({"diff": "+return 1\n"}, "base", None)["id"]
    assert first["id"] != identify({"diff": "+    return 1\n"}, "another-base", None)["id"]


def test_seed_has_identity_before_budget_blocks_reproduction(tmp_path):
    assert main(["--state-dir", str(tmp_path), "fix", "--repo", ".", "--model", "deepseek:deepseek-flash",
                 "--seed-patch", "evals/fixtures/shipping-regression.diff", "--max-seconds", "0",
                 "--test", "examples/shipping/test_shipping.py::test_standard_shipping_at_threshold_is_free"]) == 1
    path, = (tmp_path / "runs").glob("*/report.json")
    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["reason_code"] == "budget_exhausted"
    candidate, = report["candidates"]
    assert candidate["commit"] == report["task"]["commit"] and candidate["parent_id"] is None
    assert candidate["diff_hash"] and candidate["id"]
    assert not report["budget"]["reservations"]
