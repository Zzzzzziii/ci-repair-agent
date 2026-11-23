from typing import TypedDict


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
