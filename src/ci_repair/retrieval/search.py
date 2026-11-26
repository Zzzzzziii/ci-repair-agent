from contextlib import closing
import json
from pathlib import Path, PurePosixPath
import re
from time import perf_counter

from ci_repair.retrieval.chunks import IDENTIFIERS, terms
from ci_repair.retrieval.embedding import semantic_search
from ci_repair.retrieval.index import index_path, load_commit_view
from ci_repair.schemas import RetrievalQuery, RetrievedEvidence


def failure_query(targets: list[str], output: str) -> str:
    lines = output[-12000:].splitlines()
    statements = [line.lstrip()[1:].strip() for line in lines if line.lstrip().startswith(">")]
    locations = [line.strip() for line in lines if re.match(r"^\s*[^\s]+\.py:\d+:", line)]
    clues = " ".join([*statements[:6], *targets, *locations[:8]])
    return (clues + " " + output[-1600:])[:2400]


def evidence(row, commit: str, score: float, source: str) -> RetrievedEvidence:
    return {
        **json.loads(row["payload"]), "id": row["id"], "path": row["path"], "commit": commit,
        "score": score, "sources": [source],
    }


def search_symbols(connection, commit: str, symbol: str, path: str = "", limit: int = 8) -> list[RetrievedEvidence]:
    value = symbol.casefold()
    path = path.replace("\\", "/").removeprefix("./")
    rows = connection.execute(
        "SELECT * FROM chunks WHERE (symbol=? OR leaf=?) "
        "AND (?='' OR path=?) ORDER BY symbol != ?, path, rowid LIMIT ?",
        (value, value, path, path, value, limit),
    )
    return [evidence(row, commit, 1.0, "symbol") for row in rows]


def search_bm25(connection, commit: str, query: str, limit: int) -> list[RetrievedEvidence]:
    tokens = list(dict.fromkeys(token for token in terms(query) if len(token) >= 2))[:48]
    if not tokens:
        return []
    match = " OR ".join(f'"{token}"' for token in tokens)
    rows = connection.execute(
        "SELECT chunks.*, bm25(code_search, 1.0, 2.0) AS rank FROM code_search "
        "JOIN chunks ON chunks.rowid=code_search.rowid WHERE code_search MATCH ? "
        "ORDER BY rank, chunks.rowid LIMIT ?", (match, limit),
    )
    return [evidence(row, commit, -row["rank"], "bm25") for row in rows]


def search_literal(connection, commit: str, query: str, limit: int) -> list[RetrievedEvidence]:
    call = re.search(r"([^\W\d]\w*)\s*\(", query)
    match = call or IDENTIFIERS.search(query)
    if not match:
        return []
    # Search for the first call name in the failure, or the first identifier when no call is shown.
    literal = call[1] if call else match[0]
    rows = connection.execute("SELECT * FROM chunks ORDER BY rowid")
    hits = []
    for row in rows:
        hit = evidence(row, commit, 1.0, "literal")
        if literal in hit["text"]:
            hits.append(hit)
            if len(hits) == limit:
                break
    return hits


def merge_hits(hits):
    merged = {}
    for hit in hits:
        if current := merged.get(hit["id"]):
            current["sources"] = list(dict.fromkeys([*current["sources"], *hit["sources"]]))
        else:
            merged[hit["id"]] = {**hit, "sources": list(hit["sources"])}
    return list(merged.values())


def reciprocal_rank_fusion(*rankings) -> list[RetrievedEvidence]:
    points = {}
    for ranking in rankings:
        for rank, hit in enumerate(ranking, start=1):
            points[hit["id"]] = points.get(hit["id"], 0) + 1 / (60 + rank)
    results = merge_hits(hit for ranking in rankings for hit in ranking)
    for hit in results:
        hit["score"] = points[hit["id"]]
    return sorted(results, key=lambda hit: -hit["score"])


def resolve_import(paths: list[str], source: str, item: dict) -> str | None:
    parent = PurePosixPath(source).parent
    module = item["module"].replace(".", "/")
    if item["level"]:
        for _ in range(item["level"] - 1):
            if parent == PurePosixPath("."):
                return None
            parent = parent.parent
        target = parent / module
        if not module:
            target = target / item["name"]
        candidates = [target.with_suffix(".py").as_posix(), (target / "__init__.py").as_posix()]
        return next((path for path in candidates if path in paths), None)
    if not module:
        return None
    suffixes = (module + ".py", module + "/__init__.py")
    local = tuple((parent / name).as_posix() for name in suffixes)
    direct = next((path for path in local if path in paths), None)
    if direct:
        return direct
    matches = [path for path in paths if any(path == name or path.endswith("/" + name) for name in suffixes)]
    return matches[0] if len(matches) == 1 else None


def expand_references(connection, commit: str, seeds: list[RetrievedEvidence], limit: int) -> list[RetrievedEvidence]:
    paths = [row[0] for row in connection.execute("SELECT path FROM files ORDER BY path")]
    # An existing lexical hit can also be evidence of an actual import edge.
    visited = set()
    expansion = []
    frontier = seeds
    for _ in range(2):
        next_frontier = []
        for seed in frontier:
            row = connection.execute("SELECT imports FROM files WHERE path=?", (seed["path"],)).fetchone()
            identifiers = set(terms(seed["text"]))
            for item in json.loads(row[0]) if row else []:
                if item["alias"].casefold() not in identifiers:
                    continue
                path = resolve_import(paths, seed["path"], item)
                if path is None or path == seed["path"]:
                    continue
                symbol = item["name"]
                if symbol is None:
                    attribute = re.search(rf"\b{re.escape(item['alias'])}\.(\w+)", seed["text"])
                    symbol = attribute[1] if attribute else None
                hits = search_symbols(connection, commit, symbol, path, 2) if symbol else []
                if not hits and (symbol is None or not item["module"]):
                    rows = connection.execute("SELECT * FROM chunks WHERE path=? ORDER BY rowid LIMIT 2", (path,))
                    hits = [evidence(row, commit, 0.0, "import") for row in rows]
                for hit in hits:
                    if hit["id"] not in visited:
                        visited.add(hit["id"])
                        hit["sources"] = ["import"]
                        hit["score"] = 0.0
                        expansion.append(hit)
                        next_frontier.append(hit)
                        if len(expansion) == limit:
                            return expansion
        frontier = next_frontier
    return expansion


def select_context(hits: list[RetrievedEvidence], budget: int, limit: int) -> tuple[list[RetrievedEvidence], int]:
    selected = []
    cost = 0
    for hit in hits:
        # UTF-8 bytes including JSON escaping provide a conservative estimate for code tokens.
        size = len(json.dumps(hit, ensure_ascii=False).encode("utf-8")) + 2
        if cost + size <= budget:
            selected.append(hit)
            cost += size
            if len(selected) == limit:
                break
    return selected, cost


def retrieve_code(commit: str, cache: Path, query: RetrievalQuery, *, embedder=None, model_name: str = "") -> dict:
    started = perf_counter()
    text = query["text"]
    limit = query["limit"]
    if not 1 <= limit <= 30 or query["token_budget"] < 1:
        raise ValueError("Invalid retrieval limit or context budget")
    with closing(load_commit_view(commit, cache)) as connection:
        if query["mode"] == "literal":
            ranked = search_literal(connection, commit, text, limit)
        else:
            lexical = search_bm25(connection, commit, text, limit * 3)
            names = list(dict.fromkeys(match[0] for match in IDENTIFIERS.finditer(text)))[:32]
            exact = merge_hits(hit for name in names for hit in search_symbols(connection, commit, name, limit=3))
            ranked = merge_hits([*exact, *lexical])
            if query["mode"] == "hybrid":
                if embedder is None or not model_name:
                    raise ValueError("Hybrid retrieval requires an embedding model")

                rows = connection.execute("SELECT * FROM chunks ORDER BY rowid")
                chunks = (evidence(row, commit, 0, "embedding") for row in rows)
                semantic = semantic_search(chunks, text, embedder, model_name, index_path(commit, cache).parent, limit * 3)
                ranked = reciprocal_rank_fusion(ranked, semantic)
            expanded = expand_references(connection, commit, ranked[:4], limit)
            ranked = merge_hits([*ranked[:2], *expanded, *ranked[2:]])
    selected, cost = select_context(ranked, query["token_budget"], limit)
    hits = [{key: value for key, value in hit.items() if key != "text"} for hit in ranked[:limit * 3]]
    return {"query": query, "commit": commit, "hits": hits, "context": selected, "estimated_tokens": cost,
            "seconds": round(perf_counter() - started, 6)}
