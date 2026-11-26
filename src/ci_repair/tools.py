from collections.abc import Callable
from contextlib import closing
import json
from pathlib import Path
import subprocess

from langchain.tools import tool

from ci_repair.retrieval import load_commit_view, search_symbols as find_symbols
from ci_repair.retrieval.chunks import split_source_lines
from ci_repair.retrieval.index import MAX_FILE_BYTES, tree_files
from ci_repair.workspace import git


def repository_tools(tree: Path, commit: str, *, cache: Path | None = None,
                     retrieve: Callable[[str, int], dict] | None = None):
    tracked = tree_files(tree, commit)

    @tool
    def read_file(path: str, start_line: int = 1, end_line: int = 120) -> str:
        """Read up to 120 numbered lines from a tracked file at the failing commit."""
        path = path.replace("\\", "/")
        if start_line < 1 or not start_line <= end_line < start_line + 120:
            raise ValueError("Select a valid range of at most 120 lines")
        lines = source_lines(tree, tracked, path)
        return "".join(f"{number}: {line[:500]}\n"
                       for number, line in enumerate(lines[start_line - 1:end_line], start_line))[:18000]

    @tool
    def search_code(query: str) -> str:
        """Find literal text in tracked Python files at the failing commit."""
        if not query.strip() or len(query) > 160 or "\n" in query or "\r" in query:
            raise ValueError("Provide a single nonempty literal query of up to 160 characters")
        result = subprocess.run(
            ["git", "-C", str(tree), "grep", "-n", "-I", "-F", "-m", "4", "-e", query, commit, "--", "*.py"],
            capture_output=True, encoding="utf-8", errors="replace", timeout=10,
        )
        if result.returncode not in (0, 1):
            raise ValueError(result.stderr.strip())
        return "\n".join(line.removeprefix(commit + ":") for line in result.stdout.splitlines()[:40])[:12000] or "No matches"

    @tool
    def git_diff(path: str = "") -> str:
        """Inspect changes introduced by the failing commit, optionally for one tracked path."""
        path = path.replace("\\", "/")
        if path and path not in tracked:
            raise ValueError("Provide a tracked repository path")
        return git(tree, "--literal-pathspecs", "show", "--format=", "--no-ext-diff", commit, "--", path or ".")[:16000]

    @tool
    def list_files(prefix: str = "", limit: int = 60) -> str:
        """List tracked paths at the failing commit, optionally under a directory prefix."""
        if not 1 <= limit <= 100:
            raise ValueError("Select a file limit from 1 to 100")
        prefix = prefix.replace("\\", "/").removeprefix("./").rstrip("/")
        paths = [path for path in tracked if not prefix or path == prefix or path.startswith(prefix + "/")]
        return json.dumps({"commit": commit, "paths": paths[:limit], "truncated": len(paths) > limit}, ensure_ascii=False)

    @tool
    def search_symbols(symbol: str, path: str = "") -> str:
        """Find exact Python definitions by name or qualified name, optionally scoped to a repository-relative path."""
        if not symbol.strip() or len(symbol) > 240:
            raise ValueError("Provide a Python symbol name of up to 240 characters")
        with closing(load_commit_view(commit, cache)) as connection:
            matches = find_symbols(connection, commit, symbol, path)
        return json.dumps({"commit": commit, "matches": [
            {key: value for key, value in hit.items() if key != "text"} for hit in matches
        ]}, ensure_ascii=False)

    @tool
    def retrieve_code(query: str, limit: int = 4) -> str:
        """Find source snippets for failure clues and statically imported definitions."""
        if not query.strip() or len(query) > 1200 or not 1 <= limit <= 8:
            raise ValueError("Provide a query up to 1200 characters and a result limit from 1 to 8")
        report = retrieve(query, limit)
        return json.dumps({"commit": commit, "context": report["context"],
                           "estimated_tokens": report["estimated_tokens"]}, ensure_ascii=False)

    tools = [read_file, search_code, git_diff]
    if cache is not None and retrieve is not None:
        tools.extend([list_files, search_symbols, retrieve_code])
    return tools


def source_lines(tree: Path, tracked: dict[str, tuple[str, int]], path: str) -> list[str]:
    blob = tracked.get(path)
    if blob is None or blob[1] > MAX_FILE_BYTES:
        raise ValueError("Select a tracked file of at most 512 KiB")
    return split_source_lines(git(tree, "cat-file", "blob", blob[0]))


def verify_references(tree: Path, commit: str, candidates: list[dict]) -> None:
    tracked = tree_files(tree, commit)
    line_counts = {}
    for candidate in candidates:
        verified, unverified = [], []
        for reference in candidate["evidence"]:
            path, separator, line = reference.rpartition(":")
            path = path.replace("\\", "/").removeprefix("./")
            if separator and line.isascii() and line.isdecimal() and len(line) <= 9:
                if path not in line_counts:
                    blob = tracked.get(path)
                    line_counts[path] = len(source_lines(tree, tracked, path)) if blob and blob[1] <= MAX_FILE_BYTES else 0
                if 1 <= int(line) <= line_counts[path]:
                    verified.append(f"{path}:{int(line)}")
                    continue
            unverified.append(reference)
        candidate["evidence"] = verified
        if unverified:
            candidate["unverified_evidence"] = unverified
