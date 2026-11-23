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
