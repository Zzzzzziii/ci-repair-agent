from pathlib import Path
import subprocess
from time import monotonic
from typing import TypedDict
from uuid import uuid4

from langgraph.graph import END, START, StateGraph

from ci_repair.artifacts import append_workflow_event, record_retry_feedback
from ci_repair.budget import Budget, BudgetExhausted
from ci_repair.sequential import register_sequential
from ci_repair.feedback import build_feedback
from ci_repair.memory import RepairMemory
from ci_repair.recovery import checkpoint_evidence, resume_config
from ci_repair.model import propose
from ci_repair.pytest_adapter import PytestAdapter
from ci_repair.verification import baseline, verify
from ci_repair.workspace import worktree


class RepairState(TypedDict, total=False):
    evidence_hashes: dict[str, str]
    prompt: str
    observations: list[dict]
    corrections: int
    decision: dict
    applied_diff: str
    target_run: dict
    related_run: dict
    related_nodes: list[str]
    verdict: dict
    task: dict
    reference: dict
    candidates: list[dict]
    validations: list[dict]
    tool_trace: list[dict]
    model_usage: list[dict]
    history: list[dict]
    status: str
    reason: str
    reason_code: str


def build_workflow(checkpointer, storage: Path, *, pause_after_candidate: bool = False, relocated_repo=None, config_override=None):
    scratch = storage / "scratch"
    scratch.mkdir(exist_ok=True)
    memory = RepairMemory(storage / "repairs.sqlite")

    def record_node(name, action):
        def run(state: RepairState):
            if relocated_repo is not None or config_override is not None:
                repo = relocated_repo or Path(state["task"]["repository"])
                cfg = resume_config(state["task"], repo, config_override)
                state = {**state, "task": {**state["task"], "repository": str(repo), "project_config": str(cfg) if cfg else None}}
            task_id = state["task"]["id"]
            directory = storage / "runs" / task_id
            details = {"node": name, "step_id": uuid4().hex}
            if name == "validate":
                details["candidate"] = len(state.get("validations", [])) + 1
            append_workflow_event(directory, task_id, "node_started", **details)
            started = monotonic()
            try:
                budget = Budget(directory, state["task"]["limits"]) if state["task"].get("limits") and name != "finish" else None
                if budget:
                    budget.check_budget()
                updates = action(state)
                if relocated_repo is not None or config_override is not None:
                    updates["task"] = state["task"]
                if state["task"].get("data_version") == 2:
                    updates["evidence_hashes"] = checkpoint_evidence(directory, updates, state.get("evidence_hashes", {}))
                if budget:
                    budget.check_budget()
            except BudgetExhausted as error:
                updates = {"status": "rejected", "reason_code": "budget_exhausted", "reason": f"Budget exhausted: {error}"}
            except Exception as error:
                append_workflow_event(
                    directory, task_id, "node_failed", **details,
                    duration_ms=round((monotonic() - started) * 1000), error_type=type(error).__name__,
                )
                raise
            outcome = updates.get("status", state["status"])
            if name == "validate":
                outcome = updates["validations"][-1]["status"]
            append_workflow_event(
                directory, task_id, "node_finished", **details,
                duration_ms=round((monotonic() - started) * 1000), outcome=outcome,
            )
            return updates

        return run

    def adapter(task):
        return PytestAdapter(
            Path(task["python"]),
            scratch,
            task["timeout"],
            task_directory=storage / "runs" / task["id"],
            source_paths=task.get("source_paths"),
            import_modules=task.get("import_modules"),
            pytest_args=task.get("pytest_args"),
            budget=Budget(storage / "runs" / task["id"], task["limits"]) if task.get("limits") else None,
        )

    def reproduce(state: RepairState):
        task = state["task"]
        try:
            reference = baseline(adapter(task), task, scratch)
        except (ValueError, OSError, subprocess.TimeoutExpired) as error:
            return {"status": "blocked", "reason": str(error), "reason_code": "execution_error"}
        if reference["reason"]:
            return {
                "reference": reference, "status": "blocked",
                "reason": reference["reason"], "reason_code": reference["reason_code"],
            }
        return {"reference": reference, "status": "running"}

    def generate(state: RepairState):
        task = state["task"]
        output = state["reference"]["target"]["output"]
        query = " ".join(task["targets"]) + " " + output[-4000:]
        history = memory.search(task["repository_key"], query, exclude_task=task["id"]) if task["use_memory"] else []
        candidates = state.get("candidates", [])
        feedback = [
            build_feedback(candidate, result, task["targets"])
            for candidate, result in zip(candidates, state.get("validations", []))
            if result["status"] == "rejected"
        ]
        task_directory = storage / "runs" / task["id"]
        with worktree(Path(task["repository"]), task["commit"], scratch,
                      task_directory=task_directory, purpose="exploration") as tree:
            if feedback:
                record_retry_feedback(task_directory, feedback)
            proposal = propose(
                task["model"], tree, task, output, history, feedback,
                task_directory=task_directory, index_cache=storage / "retrieval",
            )
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
            result = {"status": "blocked", "reason": str(error), "reason_code": "execution_error"}
        return {"validations": [*previous, {"candidate": len(previous) + 1, **result}]}

    def finish(state: RepairState):
        task = state["task"]
        if task["use_memory"]:
            for verdict in state.get("validations", []):
                if verdict["status"] in ("accepted", "rejected"):
                    memory.save_experience(task, state["candidates"][verdict["candidate"] - 1], verdict,
                                           state["reference"]["target"]["output"])
        if state["status"] in ("blocked", "rejected"):
            return {}
        results = state.get("validations", [])
        accepted = next((result for result in results if result["status"] == "accepted"), None)
        if not accepted:
            blocked = next((result for result in results if result["status"] == "blocked"), None)
            if blocked:
                return {
                    "status": "blocked", "reason": blocked["reason"],
                    "reason_code": blocked.get("reason_code", "execution_error"),
                }
            return {"status": "rejected", "reason": "No candidate passed verification"}
        return {"status": "accepted"}

    def after_baseline(state):
        if state["status"] != "running":
            return "finish"
        if state["task"].get("limits"):
            return "apply" if state.get("candidates") else "retrieve"
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
    register_sequential(graph, record_node, storage, memory, adapter)
    graph.add_node("reproduce", record_node("reproduce", reproduce))
    graph.add_node("generate", record_node("generate", generate))
    graph.add_node("validate", record_node("validate", validate))
    graph.add_node("finish", record_node("finish", finish))
    graph.add_edge(START, "reproduce")
    graph.add_conditional_edges("reproduce", after_baseline, ["finish", "validate", "generate", "retrieve", "apply"])
    graph.add_edge("generate", "validate")
    graph.add_conditional_edges("validate", after_candidate, ["validate", "generate", "finish"])
    graph.add_edge("finish", END)
    return graph.compile(
        checkpointer=checkpointer,
        interrupt_after=["validate", "judge"] if pause_after_candidate else None,
    )
