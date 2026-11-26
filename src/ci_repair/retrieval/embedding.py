from contextlib import closing
from hashlib import sha256
from heapq import nlargest
from itertools import batched
import json
from math import isfinite, sqrt
from pathlib import Path
import sqlite3


def unit_vector(values, dimensions: int | None = None) -> list[float]:
    vector = [float(value) for value in values]
    if not vector or (dimensions is not None and len(vector) != dimensions) or not all(map(isfinite, vector)):
        raise ValueError("Invalid embedding dimensions or values")
    norm = sqrt(sum(value * value for value in vector))
    return [value / norm for value in vector] if norm else vector


def embedding_request(request, value):
    try:
        return request(value)
    except Exception as error:
        raise ValueError(f"Embedding provider request failed ({type(error).__name__})") from None


def semantic_search(chunks, query: str, embedder, model_name: str, cache: Path, limit: int) -> list[dict]:
    query_vector = unit_vector(embedding_request(embedder.embed_query, query))
    cache.mkdir(parents=True, exist_ok=True)

    def score_chunks(connection):
        for rows in batched(chunks, 16):
            documents = [f"{row['symbol']}\n{row['text']}" for row in rows]
            keys = [sha256(document.encode("utf-8")).hexdigest() for document in documents]
            vectors = {}
            missing = {}
            for key, document in zip(keys, documents):
                cached = connection.execute("SELECT dimensions, vector FROM vectors WHERE model=? AND hash=?",
                                            (model_name, key)).fetchone()
                if cached:
                    if cached[0] != len(query_vector):
                        raise ValueError("Embedding model dimensions differ from the cached vectors")
                    vectors[key] = unit_vector(json.loads(cached[1]), cached[0])
                else:
                    missing[key] = document
            if missing:
                supplied = embedding_request(embedder.embed_documents, list(missing.values()))
                if len(supplied) != len(missing):
                    raise ValueError("Embedding provider returned an unexpected number of vectors")
                values = [unit_vector(vector, len(query_vector)) for vector in supplied]
                vectors.update(zip(missing, values))
                with connection:
                    connection.executemany("INSERT OR IGNORE INTO vectors VALUES (?,?,?,?)", (
                        (model_name, key, len(query_vector), json.dumps(vectors[key])) for key in missing
                    ))
            for row, key in zip(rows, keys):
                score = sum(a * b for a, b in zip(query_vector, vectors[key]))
                yield {**row, "score": score, "sources": ["embedding"]}

    with closing(sqlite3.connect(cache / "embeddings.sqlite", timeout=60)) as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS vectors (model TEXT, hash TEXT, dimensions INTEGER, vector TEXT, "
                           "PRIMARY KEY (model, hash))")
        return nlargest(limit, score_chunks(connection), key=lambda hit: hit["score"])
