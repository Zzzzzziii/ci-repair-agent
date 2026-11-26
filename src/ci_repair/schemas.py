from typing import Literal, NotRequired, TypedDict


class CodeChunk(TypedDict):
    start_line: int
    end_line: int
    symbol: str
    parent_symbol: str | None
    kind: str
    text: str
    content_hash: str
    diagnostic: str | None


class RetrievalQuery(TypedDict):
    text: str
    mode: Literal["literal", "bm25", "hybrid"]
    limit: int
    token_budget: int


class RetrievedEvidence(CodeChunk):
    id: str
    commit: str
    path: str
    score: float
    sources: list[str]


class ProcessResult(TypedDict):
    exit_code: int | None
    timed_out: bool
    output: str
    cleanup_error: str | None


class TestFailureEvidence(TypedDict):
    node_id: str
    status: str
    detail: str


class TestFeedback(TypedDict):
    status: str
    failures: list[TestFailureEvidence]
    omitted_cases: int
    output: str
    artifacts: dict[str, str]


class RepairFeedback(TypedDict):
    candidate: int
    hypothesis: str
    diff: str
    status: str
    reason: str | None
    target: NotRequired[TestFeedback]
    regression: NotRequired[TestFeedback]


class ProjectProfile(TypedDict):
    repository: str
    project_config: str | None
    cwd: str
    python: str
    source_paths: list[str]
    import_modules: list[str]
    pytest_args: list[str]
    regression: list[str]
    timeout: float
    embedding_model: str | None


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
    import_modules: list[str]
    pytest_args: list[str]
    regression: list[str]
    timeout: float
    targets: list[str]
    model: str | None
    retrieval_mode: NotRequired[str | None]
    embedding_model: NotRequired[str | None]
    input_log: str | None
    environment_fingerprint: NotRequired[str]


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
