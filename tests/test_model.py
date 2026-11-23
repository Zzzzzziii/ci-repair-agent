import json

from langchain_core.messages import AIMessage, ToolMessage

from ci_repair import model
from ci_repair.workspace import commit_id, git


def test_model_uses_real_repository_tool_before_proposal(tmp_path, monkeypatch):
    git(tmp_path, "init", "--quiet")
    (tmp_path / "shipping.py").write_text("def fee(total):\n    return 12\n", encoding="utf-8")
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
                {"id": "read-1", "name": "read_file", "args": {"path": "shipping.py", "end_line": 2}}
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
