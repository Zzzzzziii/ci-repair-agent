from ci_repair.feedback import build_feedback


def test_feedback_prioritizes_regression_assertion_and_original_evidence():
    candidate = {"hypothesis": "Apply the free-shipping threshold", "diff": "start\n" + "x" * 6000 + "\nend"}
    broken = "test_shipping.py::test_remote_fee"
    missing = "test_shipping.py::test_missing_case"
    target = "test_shipping.py::test_threshold"
    detail = "test_shipping.py:20: in test_remote_fee\nAssertionError: assert 0 == 25"
    validation = {
        "candidate": 1,
        "status": "rejected",
        "reason": "Introduced a regression",
        "target": {"status": "passed", "tests": {target: {"status": "passed"}}, "output": "target output"},
        "regression": {
            "status": "failed",
            "artifacts": {"pytest_report": "artifacts/pytest/abc/pytest.json"},
            "tests": {broken: {"status": "failed", "phases": {"call": {"detail": detail}}}},
        },
        "regressions": [missing, "case3", "case4", "case5", broken],
    }

    feedback = build_feedback(candidate, validation, [target])
    assert feedback["target"]["failures"] == []
    assert feedback["target"]["output"] == ""
    assert feedback["regression"]["failures"][:2] == [
        {"node_id": broken, "status": "failed", "detail": "call: " + detail},
        {"node_id": missing, "status": "missing", "detail": ""},
    ]
    assert feedback["regression"]["artifacts"] == validation["regression"]["artifacts"]
    assert feedback["regression"]["omitted_cases"] == 1
    assert len(feedback["diff"]) <= 5000
    assert feedback["diff"].startswith("start\n") and feedback["diff"].endswith("\nend")


def test_feedback_preserves_patch_error_without_test_results():
    feedback = build_feedback(
        {"hypothesis": "Fix fee", "diff": "supplied diff"},
        {"candidate": 1, "status": "rejected", "reason": "patch does not apply: shipping.py:12"},
        ["test_shipping.py::test_threshold"],
    )
    assert feedback["reason"] == "patch does not apply: shipping.py:12"
    assert feedback["diff"] == "supplied diff"
    assert "target" not in feedback
    assert "regression" not in feedback
