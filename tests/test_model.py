import json

from langchain_core.messages import AIMessage, ToolMessage

from ci_repair import model
from ci_repair.workspace import commit_id, git


def test_model_uses_real_repository_tool_before_proposal(tmp_path, monkeypatch):
    git(tmp_path, "init", "--quiet")
    model_key = "fixture-deepseek-key"
    monkeypatch.setenv("DEEPSEEK_API_KEY", model_key)
    (tmp_path / "shipping.py").write_text(
        "def fee(total):\n    return 12\n" + ("# " + "x" * 450 + "\n") * 3
        + f"# end of captured source {model_key}\n", encoding="utf-8",
    )
    git(tmp_path, "add", ".")
    git(tmp_path, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "--quiet", "-m", "Baseline")
    task_directory = tmp_path / "task"
    usage = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}

    class ScriptedModel:
        def bind_tools(self, tools):
            assert {item.name for item in tools} == {"read_file", "search_code", "git_diff"}
            return self

        def with_structured_output(self, schema, *, include_raw):
            assert schema is model.Proposal and include_raw
            return self

        def invoke(self, messages):
            if str(messages[-1].content).startswith("Return up to"):
                assert any(isinstance(item, ToolMessage) and "return 12" in item.content for item in messages)
                proposal = model.Proposal(candidates=[
                    model.Candidate(hypothesis="wrong flat fee", evidence=["shipping.py:2"], diff="patch")
                ])
                return {"parsed": proposal, "raw": AIMessage(content="", usage_metadata=usage)}
            if isinstance(messages[-1], ToolMessage):
                return AIMessage(content="Found the source")
            ongoing = [
                json.loads(path.read_text(encoding="utf-8"))
                for path in (task_directory / "artifacts" / "model").glob("*.json")
            ]
            assert any(call["purpose"] == "explore" and call["status"] == "running" for call in ongoing)
            return AIMessage(content="", tool_calls=[
                {"id": "read-1", "name": "read_file", "args": {"path": "shipping.py", "end_line": 6}}
            ])

    def init_model(name, **options):
        assert name == "deepseek:deepseek-flash"
        assert options["extra_body"] == {"thinking": {"type": "disabled"}}
        return ScriptedModel()

    monkeypatch.setattr(model, "init_chat_model", init_model)
    proposal = model.propose("deepseek:deepseek-flash", tmp_path, {
        "id": "test-task", "commit": commit_id(tmp_path, "HEAD"), "targets": ["test_shipping.py::test_fee"],
        "strategy": "batch",
    }, "AssertionError", [], [], task_directory=task_directory)
    assert proposal["tool_trace"][0]["tool"] == "read_file"
    assert proposal["candidates"][0]["evidence"] == ["shipping.py:2"]
    calls = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (task_directory / "artifacts" / "model").glob("*.json")
    ]
    assert sorted(call["purpose"] for call in calls) == ["explore", "explore", "initialize", "propose"]
    assert len({call["call_id"] for call in calls}) == 4
    assert all(call["task_id"] == "test-task" and call["status"] == "succeeded" for call in calls)
    assert all(call["duration_ms"] >= 0 for call in calls)
    assert all(call["usage"] is None for call in calls if call["purpose"] == "explore")
    assert next(call for call in calls if call["purpose"] == "propose")["usage"] == usage
    trace = proposal["tool_trace"][0]
    assert len(trace["result"]) == 1000
    assert "end of captured source" not in trace["result"]
    event_path, = (task_directory / "artifacts" / "tools").glob("*.json")
    event_text = event_path.read_text(encoding="utf-8")
    assert model_key not in event_text
    event = json.loads(event_text)
    assert event["tool_call_id"] == "read-1"
    assert event["event_id"] == event_path.stem
    assert event["task_id"] == "test-task"
    assert event["status"] == "succeeded"
    assert event["args"] == {"path": "shipping.py", "end_line": 6}
    assert event["result"].rstrip("\n").endswith("end of captured source [REDACTED]")
    assert next(call for call in calls if call["call_id"] == event["model_call_id"])["purpose"] == "explore"


def test_repository_tool_records_invalid_and_skipped_calls(tmp_path):
    git(tmp_path, "init", "--quiet")
    read_file = model.repository_tools(tmp_path, "unused")[0]
    model_key = "fixture-key-in-tool-args"
    call = {"type": "tool_call", "id": "read-1", "name": "read_file", "args": {"path": model_key}}
    task_directory = tmp_path / "task"

    for item, budget_available, status in (
        (read_file, True, "failed"),
        (None, True, "unknown_tool"),
        (read_file, False, "budget_exhausted"),
    ):
        message, event = model.execute_repository_tool(
            call, item, budget_available=budget_available, model_call_id="explore-1",
            task_id="test-task", task_directory=task_directory, secrets=(model_key,),
        )
        event_text = (task_directory / "artifacts" / "tools" / f"{event['event_id']}.json").read_text(encoding="utf-8")
        assert model_key not in event_text
        assert json.loads(event_text) == event
        assert event["status"] == status
        assert event["args"]["path"] == "[REDACTED]"
        assert event["result"] == message.content
        if status == "failed":
            assert event["error_type"] == "ValueError"
    assert len(list((task_directory / "artifacts" / "tools").glob("*.json"))) == 3
