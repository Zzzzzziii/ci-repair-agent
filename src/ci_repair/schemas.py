from typing import Literal, TypedDict


class ProcessResult(TypedDict):
    exit_code: int | None
    timed_out: bool
    output: str
    cleanup_error: str | None


class ProjectProfile(TypedDict):
    repository: str
    project_config: str | None
    cwd: str
    python: str
    source_paths: list[str]
    regression: list[str]
    timeout: float


class RunManifest(TypedDict):
    schema_version: int
    task_id: str
    created_at: str
    repository: str
    commit: str
    commit_source: str
    cwd: str
    project_config: str | None
    python: str
    source_paths: list[str]
    regression: list[str]
    timeout: float
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


WorkflowEventType = Literal[
    "task_started", "task_resumed", "task_paused", "task_interrupted", "task_finished",
    "node_started", "node_finished", "node_failed",
]


class WorkflowEvent(TypedDict):
    schema_version: int
    task_id: str
    timestamp: str
    event: WorkflowEventType
    details: dict[str, object]
