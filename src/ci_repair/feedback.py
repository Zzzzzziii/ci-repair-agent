from ci_repair.schemas import RepairFeedback, TestFeedback, TestFailureEvidence


MAX_CASES = 4


def excerpt(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    marker = "\n[... truncated ...]\n"
    head = (limit - len(marker)) // 2
    tail = limit - len(marker) - head
    return text[:head] + marker + text[-tail:]


def summarize_test_run(run: dict, nodes: list[str]) -> TestFeedback:
    failures: list[TestFailureEvidence] = []
    tests = run["tests"]
    prioritized = sorted(nodes, key=lambda node: tests.get(node, {}).get("status") not in {"failed", "error"})
    for node in prioritized[:MAX_CASES]:
        test = tests.get(node, {})
        detail = "\n".join(
            f"{stage}: {phase['detail']}"
            for stage, phase in test.get("phases", {}).items()
            if phase.get("detail")
        )
        failures.append({"node_id": node, "status": test.get("status", "missing"), "detail": excerpt(detail, 1800)})

    output = ""
    if run["status"] != "passed" and not any(failure["detail"] for failure in failures):
        output = "\n".join(run.get("collection_errors", [])) or run.get("output", "")
    return {
        "status": run["status"],
        "failures": failures,
        "omitted_cases": max(0, len(nodes) - MAX_CASES),
        "output": excerpt(output, 1800),
        "artifacts": run.get("artifacts", {}),
    }


def build_feedback(candidate: dict, validation: dict, targets: list[str]) -> RepairFeedback:
    feedback: RepairFeedback = {
        "candidate": validation["candidate"],
        "hypothesis": excerpt(candidate["hypothesis"], 600),
        "diff": excerpt(candidate["diff"], 5000),
        "status": validation["status"],
        "reason": excerpt(validation["reason"], 1200) if validation.get("reason") else None,
    }
    if candidate.get("id"):
        feedback["candidate_id"] = candidate["id"]
    if candidate.get("applied_diff_artifact"):
        feedback["applied_diff_artifact"] = candidate["applied_diff_artifact"]
    if target := validation.get("target"):
        failed_targets = [node for node in targets if target["tests"].get(node, {}).get("status") != "passed"]
        feedback["target"] = summarize_test_run(target, failed_targets)
    if regression := validation.get("regression"):
        feedback["regression"] = summarize_test_run(regression, validation.get("regressions", []))
    return feedback
