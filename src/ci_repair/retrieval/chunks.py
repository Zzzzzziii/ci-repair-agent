import ast
from hashlib import sha256
from io import StringIO
import re

from ci_repair.schemas import CodeChunk


MAX_LINES = 80
MAX_CHARS = 6000
DEFINITION = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
IDENTIFIERS = re.compile(r"[^\W\d]\w*", re.UNICODE)
WORD_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])|_+")


def terms(text: str):
    for match in IDENTIFIERS.finditer(text):
        value = match[0]
        yield value.casefold()
        yield from (part.casefold() for part in WORD_BOUNDARY.split(value) if part and part != value)


def split_source_lines(source: str) -> list[str]:
    normalized = StringIO(source, newline=None).read()
    return normalized.removesuffix("\n").split("\n") if normalized else []


def split_range(lines: list[str], first: int, last: int, symbol: str, parent: str | None,
                kind: str, diagnostic: str | None) -> list[CodeChunk]:
    chunks = []
    start = first
    while start <= last:
        end = start
        length = min(len(lines[end - 1]), MAX_CHARS)
        while end < last and end - start + 1 < MAX_LINES and length + len(lines[end]) + 1 <= MAX_CHARS:
            length += len(lines[end]) + 1
            end += 1
        text = "\n".join(lines[start - 1:end])[:MAX_CHARS]
        if text.strip():
            note = diagnostic
            if len(lines[start - 1]) > MAX_CHARS:
                note = "line_truncated"
            elif start != first or end != last:
                note = diagnostic or "range_split"
            chunks.append({
                "start_line": start, "end_line": end, "symbol": symbol, "parent_symbol": parent,
                "kind": kind, "text": text, "content_hash": sha256(text.encode("utf-8")).hexdigest(),
                "diagnostic": note,
            })
        start = end + 1
    return chunks


def start_line(node) -> int:
    return min([node.lineno, *(decorator.lineno for decorator in node.decorator_list)])


def definitions(node, parent=None):
    for child in ast.iter_child_nodes(node):
        if isinstance(child, DEFINITION):
            symbol = f"{parent}.{child.name}" if parent else child.name
            yield child, symbol, parent
            yield from definitions(child, symbol)
        else:
            yield from definitions(child, parent)


def imports_in(tree: ast.AST) -> list[dict]:
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend({"module": alias.name, "name": None, "alias": alias.asname or alias.name.partition(".")[0],
                            "level": 0, "line": node.lineno} for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.extend({"module": node.module or "", "name": alias.name, "alias": alias.asname or alias.name,
                            "level": node.level, "line": node.lineno} for alias in node.names if alias.name != "*")
    return imports


def extract_chunks(source: str) -> tuple[list[CodeChunk], list[dict]]:
    lines = split_source_lines(source)
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return split_range(lines, 1, len(lines), "<module>", None, "fallback", "ast_parse_failed"), []

    chunks = []
    next_line = 1
    for node in tree.body:
        if isinstance(node, DEFINITION):
            chunks.extend(split_range(lines, next_line, start_line(node) - 1, "<module>", None, "module", None))
            next_line = node.end_lineno + 1
    chunks.extend(split_range(lines, next_line, len(lines), "<module>", None, "module", None))
    for node, symbol, parent in definitions(tree):
        kind = "class" if isinstance(node, ast.ClassDef) else "function"
        chunks.extend(split_range(lines, start_line(node), node.end_lineno, symbol, parent, kind, None))
    return chunks, imports_in(tree)
