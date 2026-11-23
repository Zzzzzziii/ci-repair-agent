import json
from pathlib import Path
import re
import sqlite3
from contextlib import closing


def tokens(value: str) -> set[str]:
    return set(re.findall(r"[^\W_]+", value.casefold()))


class RepairMemory:
    def __init__(self, database: Path):
        self.database = database

    def save(self, task_id: str, repository: str, record: dict) -> None:
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS repairs "
                "(task_id TEXT PRIMARY KEY, repository TEXT NOT NULL, record TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT OR REPLACE INTO repairs VALUES (?, ?, ?)",
                (task_id, repository, json.dumps(record, ensure_ascii=False)),
            )

    def search(self, repository: str, query: str, limit: int = 3) -> list[dict]:
        if not self.database.exists():
            return []
        with closing(sqlite3.connect(self.database)) as connection:
            rows = connection.execute(
                "SELECT record FROM repairs WHERE repository = ? ORDER BY rowid DESC", (repository,)
            ).fetchall()
        terms = tokens(query)
        matches = []
        for (raw,) in rows:
            record = json.loads(raw)
            score = len(terms & tokens(" ".join(record["targets"]) + " " + record["signature"]))
            if score:
                matches.append((score, record))
        return [record for _, record in sorted(matches, key=lambda match: -match[0])[:limit]]
