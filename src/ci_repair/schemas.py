from typing import Literal, TypedDict


class RunManifest(TypedDict):
    schema_version: int
    task_id: str
    created_at: str
    repository: str
    commit: str
    commit_source: str
    cwd: str
    targets: list[str]
    model: str | None
    input_log: str | None


ModelCallPurpose = Literal["initialize", "explore", "propose"]


class ModelCallRecord(TypedDict):
    schema_version: int
    call_id: str
    task_id: str
    purpose: ModelCallPurpose
    status: Literal["running", "succeeded", "request_failed", "invalid_output", "setup_failed"]
    started_at: str
    duration_ms: int | None
    usage: dict[str, int] | None
    error_type: str | None


class ToolEvent(TypedDict):
    schema_version: int
    event_id: str
    task_id: str
    model_call_id: str
    tool_call_id: str
    tool: str
    args: dict[str, object]
    status: Literal["running", "succeeded", "failed", "unknown_tool", "budget_exhausted"]
    started_at: str
    duration_ms: int | None
    result: str | None
    error_type: str | None
