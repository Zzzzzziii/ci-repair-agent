from pathlib import Path
import subprocess
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from ci_repair.memory import RepairMemory
from ci_repair.model import propose
from ci_repair.pytest_adapter import PytestAdapter
from ci_repair.verification import baseline, verify
from ci_repair.workspace import worktree


class RepairState(TypedDict, total=False):
    task: dict
    reference: dict
    candidates: list[dict]
    validations: list[dict]
    tool_trace: list[dict]
    model_usage: list[dict]
    history: list[dict]
    status: str
    reason: str


def build_workflow(checkpointer, storage: Path, *, pause_after_candidate: bool = False):
    scratch = storage / "scratch"
    scratch.mkdir(exist_ok=True)
    memory = RepairMemory(storage / "repairs.sqlite")

    def adapter(task):
        return PytestAdapter(Path(task["python"]), scratch, task["timeout"])

    def reproduce(state: RepairState):
        task = state["task"]
        try:
            reference = baseline(adapter(task), task, scratch)
        except (ValueError, OSError, subprocess.TimeoutExpired) as error:
            return {"status": "blocked", "reason": str(error)}
        if reference["reason"]:
            return {"reference": reference, "status": "blocked", "reason": reference["reason"]}
        return {"reference": reference, "status": "running"}

    def generate(state: RepairState):
        task = state["task"]
        output = state["reference"]["target"]["output"]
        query = " ".join(task["targets"]) + " " + output[-4000:]
        history = memory.search(task["repository_key"], query) if task["use_memory"] else []
        candidates = state.get("candidates", [])
        feedback = [
            {
                "hypothesis": candidate["hypothesis"],
                "diff": candidate["diff"][:5000],
                "reason": result.get("reason"),
                "regressions": result.get("regressions", []),
                "test_output": result.get("target", {}).get("output", "")[-2000:],
            }
            for candidate, result in zip(candidates, state.get("validations", []))
        ]
        with worktree(Path(task["repository"]), task["commit"], scratch) as tree:
            proposal = propose(task["model"], tree, task, output, history, feedback)
        return {
            "candidates": [*candidates, *proposal["candidates"]],
            "tool_trace": [*state.get("tool_trace", []), *proposal["tool_trace"]],
            "model_usage": [*state.get("model_usage", []), *proposal["model_usage"]],
            "history": history,
        }

    def validate(state: RepairState):
        task = state["task"]
        previous = state.get("validations", [])
        candidate = state["candidates"][len(previous)]
        try:
            result = verify(adapter(task), task, state["reference"], candidate["diff"], scratch)
        except (ValueError, OSError, subprocess.TimeoutExpired) as error:
            result = {"status": "blocked", "reason": str(error)}
        return {"validations": [*previous, {"candidate": len(previous) + 1, **result}]}

    def finish(state: RepairState):
        if state["status"] == "blocked":
            return {}
        results = state.get("validations", [])
        accepted = next((result for result in results if result["status"] == "accepted"), None)
        if not accepted:
            status = "blocked" if any(result["status"] == "blocked" for result in results) else "rejected"
            return {"status": status, "reason": "No candidate passed verification"}
        task = state["task"]
        if task["use_memory"]:
            candidate = state["candidates"][accepted["candidate"] - 1]
            memory.save(task["id"], task["repository_key"], {
                "commit": task["commit"],
                "targets": task["targets"],
                "signature": state["reference"]["target"]["output"][-1000:],
                "hypothesis": candidate["hypothesis"],
                "evidence": candidate["evidence"],
                "verified_tests": sum(
                    item["status"] == "passed" for item in accepted["regression"]["tests"].values()
                ),
            })
        return {"status": "accepted"}

    def after_baseline(state):
        if state["status"] == "blocked":
            return "finish"
        return "validate" if state.get("candidates") else "generate"

    def after_candidate(state):
        results = state["validations"]
        if len(results) < len(state["candidates"]):
            return "validate"
        task = state["task"]
        if task["model"] and task["strategy"] == "sequential" and len(results) < 2:
            if results[-1]["status"] == "rejected":
                return "generate"
        return "finish"

    graph = StateGraph(RepairState)
    graph.add_node("reproduce", reproduce)
    graph.add_node("generate", generate)
    graph.add_node("validate", validate)
    graph.add_node("finish", finish)
    graph.add_edge(START, "reproduce")
    graph.add_conditional_edges("reproduce", after_baseline, ["finish", "validate", "generate"])
    graph.add_edge("generate", "validate")
    graph.add_conditional_edges("validate", after_candidate, ["validate", "generate", "finish"])
    graph.add_edge("finish", END)
    return graph.compile(
        checkpointer=checkpointer,
        interrupt_after=["validate"] if pause_after_candidate else None,
    )
