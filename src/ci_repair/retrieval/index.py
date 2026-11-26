from contextlib import closing
from hashlib import sha256
import io
import json
from pathlib import Path
import sqlite3
import subprocess
from time import perf_counter

from ci_repair.retrieval.chunks import extract_chunks, terms
from ci_repair.workspace import commit_id, git


SCHEMA_VERSION = 1
MAX_FILE_BYTES = 512 * 1024
BLOB_BATCH = 32


def tree_files(repository: Path, commit: str) -> dict[str, tuple[str, int]]:
    paths = {}
    for record in git(repository, "ls-tree", "-r", "-z", "-l", commit).split("\0"):
        if record:
            header, path = record.split("\t", 1)
            mode, kind, oid, size = header.split()
            if mode.startswith("100") and kind == "blob":
                paths[path] = (oid, int(size))
    return paths


def load_blobs(repository: Path, identifiers: list[str]):
    for offset in range(0, len(identifiers), BLOB_BATCH):
        batch = identifiers[offset:offset + BLOB_BATCH]
        process = subprocess.run(
            ["git", "-C", str(repository), "cat-file", "--batch"],
            input=("\n".join(batch) + "\n").encode("ascii"), capture_output=True, timeout=60,
        )
        if process.returncode:
            raise ValueError(process.stderr.decode("utf-8", errors="replace").strip() or "Could not read Git blobs")
        buffer = io.BytesIO(process.stdout)
        for expected in batch:
            header = buffer.readline().decode("ascii").split()
            if len(header) != 3 or header[:2] != [expected, "blob"]:
                raise ValueError(f"Invalid Git blob response for {expected}")
            size = int(header[2])
            data = buffer.read(size)
            if len(data) != size or buffer.read(1) != b"\n":
                raise ValueError(f"Incomplete Git blob response for {expected}")
            yield expected, data.decode("utf-8", errors="replace")


def index_path(commit: str, cache: Path) -> Path:
    if len(commit) not in (40, 64) or any(char not in "0123456789abcdef" for char in commit):
        raise ValueError("A full Git commit ID is required for the index")
    return cache / f"v{SCHEMA_VERSION}" / f"{commit}.sqlite"


def load_commit_view(commit: str, cache: Path) -> sqlite3.Connection:
    path = index_path(commit, cache)
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def load_chunks(repository: Path, connection: sqlite3.Connection, identifiers: list[str]):
    cached = {}
    for offset in range(0, len(identifiers), 500):
        values = identifiers[offset:offset + 500]
        parameters = ",".join("?" for _ in values)
        for oid, payload in connection.execute(f"SELECT oid, payload FROM blobs WHERE oid IN ({parameters})", values):
            cached[oid] = json.loads(payload)
    uncached = [oid for oid in identifiers if oid not in cached]
    for oid, source in load_blobs(repository, uncached):
        extracted = extract_chunks(source)
        connection.execute("INSERT OR IGNORE INTO blobs VALUES (?, ?)", (oid, json.dumps(extracted)))
        cached[oid] = extracted
    return cached, len(uncached)


def build_index(repository: Path, ref: str, cache: Path) -> dict:
    started = perf_counter()
    commit = commit_id(repository, ref)
    path = index_path(commit, cache)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=60)) as view, view:
        view.execute("BEGIN IMMEDIATE")
        view.execute("CREATE TABLE IF NOT EXISTS metadata (data TEXT NOT NULL)")
        existing = view.execute("SELECT data FROM metadata").fetchone()
        if existing:
            stats = json.loads(existing[0])
            return {**stats, "cache_hit": True, "new_blobs": 0,
                    "reused_blobs": stats["new_blobs"] + stats["reused_blobs"],
                    "seconds": round(perf_counter() - started, 6)}

        files = tree_files(repository, commit)
        source_paths = {name: oid for name, (oid, size) in files.items()
                        if name.endswith(".py") and size <= MAX_FILE_BYTES}
        blobs = list(dict.fromkeys(source_paths.values()))
        with closing(sqlite3.connect(path.parent / "blobs.sqlite", timeout=60)) as blob_db, blob_db:
            blob_db.execute("BEGIN IMMEDIATE")
            blob_db.execute("CREATE TABLE IF NOT EXISTS blobs (oid TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            chunks, new_blobs = load_chunks(repository, blob_db, blobs)

        view.execute("CREATE TABLE files (path TEXT PRIMARY KEY, imports TEXT NOT NULL)")
        view.execute("CREATE TABLE chunks (rowid INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL, path TEXT NOT NULL, "
                     "symbol TEXT NOT NULL, leaf TEXT NOT NULL, payload TEXT NOT NULL)")
        view.execute("CREATE INDEX symbols ON chunks(symbol)")
        view.execute("CREATE INDEX leaves ON chunks(leaf)")
        view.execute("CREATE INDEX chunk_paths ON chunks(path)")
        view.execute("CREATE VIRTUAL TABLE code_search USING fts5(body, names)")
        chunk_count = 0
        for name, oid in source_paths.items():
            parts, imports = chunks[oid]
            view.execute("INSERT INTO files VALUES (?, ?)", (name, json.dumps(imports)))
            for chunk in parts:
                symbol = chunk["symbol"]
                identifier = sha256(f"{name}\0{oid}\0{chunk['start_line']}\0{symbol}".encode("utf-8")).hexdigest()
                row = view.execute("INSERT INTO chunks(id,path,symbol,leaf,payload) VALUES (?,?,?,?,?)",
                                   (identifier, name, symbol.casefold(), symbol.rsplit(".", 1)[-1].casefold(),
                                    json.dumps(chunk)))
                view.execute("INSERT INTO code_search(rowid,body,names) VALUES (?,?,?)",
                             (row.lastrowid, " ".join(terms(chunk["text"])), " ".join(terms(f"{name} {symbol}"))))
                chunk_count += 1
        stats = {"commit": commit, "schema_version": SCHEMA_VERSION, "files": len(source_paths), "chunks": chunk_count,
                 "reused_blobs": len(blobs) - new_blobs, "new_blobs": new_blobs,
                 "skipped_large_files": sum(name.endswith(".py") and size > MAX_FILE_BYTES
                                            for name, (_, size) in files.items())}
        view.execute("INSERT INTO metadata VALUES (?)", (json.dumps(stats),))
    return {**stats, "cache_hit": False, "seconds": round(perf_counter() - started, 6)}
