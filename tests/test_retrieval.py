from contextlib import closing
import json

from ci_repair.retrieval import build_index, load_commit_view, retrieve_code, search_symbols
from ci_repair.retrieval.chunks import MAX_LINES, extract_chunks
from ci_repair.tools import repository_tools
from ci_repair.workspace import commit_id, git


def snapshot(repository):
    git(repository, "add", "-A")
    git(repository, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "--quiet", "-m", "Source snapshot")
    return commit_id(repository, "HEAD")


def query(text, mode="bm25", budget=7000):
    return {"text": text, "mode": mode, "limit": 6, "token_budget": budget}


def test_ast_ranges_and_fallback():
    source = (
        "from .settings import LIMIT as CAP\n"
        "class A:\n"
        "    @staticmethod\n"
        "    def fee():\n"
        "        def nested():\n"
        "            return CAP\n"
        "        return nested()\n"
        "class B:\n"
        "    async def fee(self):\n"
        "        return 0\n"
        "def large():\n"
        + "    x = 1\n" * (MAX_LINES + 10)
    )
    chunks, imports = extract_chunks(source)
    definitions = {chunk["symbol"]: chunk for chunk in chunks if chunk["symbol"] != "large"}
    assert definitions["A.fee"]["start_line"] == 3
    assert definitions["A.fee"]["end_line"] == 7
    assert definitions["A.fee"]["text"].lstrip().startswith("@staticmethod")
    assert definitions["A.fee.nested"]["parent_symbol"] == "A.fee"
    assert definitions["B.fee"]["start_line"] == 9
    assert imports == [{"module": "settings", "name": "LIMIT", "alias": "CAP", "level": 1, "line": 1}]
    large = [chunk for chunk in chunks if chunk["symbol"] == "large"]
    assert len(large) == 2
    assert all(chunk["diagnostic"] == "range_split" for chunk in large)
    assert all(chunk["end_line"] - chunk["start_line"] < MAX_LINES for chunk in chunks)
    assert all(chunk["text"] == "\n".join(source.splitlines()[chunk["start_line"] - 1:chunk["end_line"]])
               for chunk in chunks)
    broken, imports = extract_chunks("def unfinished(\n    source_fragment\n")
    assert imports == []
    assert broken[0]["diagnostic"] == "ast_parse_failed"
    assert broken[0]["end_line"] == 2
    unicode_lines, _ = extract_chunks('text = "a\u2028b"\r\ndef fee(): return 2\r\n')
    assert unicode_lines[-1]["start_line"] == unicode_lines[-1]["end_line"] == 2
    assert unicode_lines[-1]["text"] == "def fee(): return 2"


def test_commit_views_reuse_blobs_and_ignore_worktree(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "--quiet")
    a, b, gone = [repo / name for name in ("a.py", "b.py", "gone.py")]
    a.write_text("class A:\n    def fee(self):\n        return 12\n", encoding="utf-8")
    b.write_text("class B:\n    def fee(self):\n        return 9\n", encoding="utf-8")
    gone.write_text("def removed(): return 3\n", encoding="utf-8")
    first = snapshot(repo)
    a.write_text("class A:\n    def fee(self):\n        return 20\n", encoding="utf-8")
    gone.unlink()
    second = snapshot(repo)
    a.write_text("UNCOMMITTED = True\n", encoding="utf-8")
    cache = tmp_path / "cache"
    assert build_index(repo, first, cache)["new_blobs"] == 3
    stats = build_index(repo, second, cache)
    assert (stats["files"], stats["new_blobs"], stats["reused_blobs"]) == (2, 1, 1)
    assert build_index(repo, second, cache)["cache_hit"]
    for commit, expected in ((first, "return 12"), (second, "return 20")):
        with closing(load_commit_view(commit, cache)) as connection:
            assert {hit["symbol"] for hit in search_symbols(connection, commit, "fee")} == {"A.fee", "B.fee"}
            old, = search_symbols(connection, commit, "A.fee")
            assert old["commit"] == commit and expected in old["text"]
            assert bool(search_symbols(connection, commit, "removed")) == (commit == first)
            assert [hit["symbol"] for hit in search_symbols(connection, commit, "fee", "a.py")] == ["A.fee"]
        tools = {tool.name: tool for tool in repository_tools(repo, commit, cache=cache,
                 retrieve=lambda query_text, limit: retrieve_code(commit, cache, query(query_text)))}
        assert expected in tools["read_file"].invoke({"path": "a.py"})
        assert "UNCOMMITTED" not in tools["read_file"].invoke({"path": "a.py"})
        assert tools["search_code"].invoke({"query": "UNCOMMITTED"}) == "No matches"
        assert ("gone.py" in json.loads(tools["list_files"].invoke({}))["paths"]) == (commit == first)


def test_retrieval_follows_relative_import_and_respects_budget(tmp_path):
    repo = tmp_path / "repo"
    (repo / "shop").mkdir(parents=True)
    git(repo, "init", "--quiet")
    (repo / "shop/checkout.py").write_text(
        "from .delivery import shipping_fee as charge\n"
        "def checkout_total(subtotal):\n    return subtotal + charge(subtotal)\n", encoding="utf-8",
    )
    (repo / "shop/delivery.py").write_text("def shipping_fee(total): return 12\n", encoding="utf-8")
    (repo / "delivery.py").write_text("def shipping_fee(total): return 99\n", encoding="utf-8")
    commit = snapshot(repo)
    cache = tmp_path / "cache"
    build_index(repo, commit, cache)
    report = retrieve_code(commit, cache, query("checkout_total"))
    assert any(hit["path"] == "shop/checkout.py" and hit["symbol"] == "checkout_total" for hit in report["context"])
    imported = {hit["path"] for hit in report["context"] if "import" in hit["sources"]}
    assert imported == {"shop/delivery.py"}
    assert report["estimated_tokens"] >= len(json.dumps(report["context"], ensure_ascii=False).encode("utf-8"))
    small = retrieve_code(commit, cache, query("checkout_total", budget=1))
    assert small["context"] == [] and small["estimated_tokens"] == 0 and small["hits"]


def test_hybrid_vector_cache_is_scoped_to_embedding_model(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "--quiet")
    (repo / "codes.py").write_text("def rebate(value): return value // 10\n", encoding="utf-8")
    (repo / "noise.py").write_text("def account(): return 'balance'\n", encoding="utf-8")
    commit = snapshot(repo)
    cache = tmp_path / "cache"
    build_index(repo, commit, cache)

    class FixedEmbeddings:
        document_calls = 0

        def embed_documents(self, documents):
            self.document_calls += 1
            return [[1, 0] if "rebate" in document else [0, 1] for document in documents]

        def embed_query(self, _text):
            return [1, 0]

    embedder = FixedEmbeddings()
    search = query("cashback", "hybrid")
    assert retrieve_code(commit, cache, query("cashback"))["context"] == []
    for _ in range(2):
        result = retrieve_code(commit, cache, search, embedder=embedder, model_name="fixed:v1")
        assert result["context"][0]["symbol"] == "rebate"
        assert result["context"][0]["sources"] == ["embedding"]
    assert embedder.document_calls == 1
    fused = retrieve_code(commit, cache, query("rebate", "hybrid"), embedder=embedder, model_name="fixed:v2")
    assert set(fused["context"][0]["sources"]) == {"symbol", "bm25", "embedding"}
    assert embedder.document_calls == 2
