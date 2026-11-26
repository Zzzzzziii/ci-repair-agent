from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3

from ci_repair.schemas import RepairExperience
from ci_repair.artifacts import read_report
from hashlib import file_digest


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def tokens(value: str) -> set[str]:
    return set(re.findall(r"[^\W_]+", value.casefold())) - {
        "test", "tests", "py", "failed", "error", "assert", "assertionerror", "in", "the", "at", "e"
    }


class RepairMemory:
    def __init__(self, database: Path):
        self.database = database

    def initialize(self):
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.database, timeout=30)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            version, = db.execute("PRAGMA user_version").fetchone()
            if version not in (0, 1, 2):
                raise ValueError(f"Unsupported repair memory version {version}")
            if version < 2:
                self.migrate(db)

    def migrate(self, db):
        db.execute("CREATE TABLE IF NOT EXISTS experiences (id TEXT PRIMARY KEY, repository TEXT NOT NULL, "
                   "record TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, checked TEXT)")
        db.execute("CREATE INDEX IF NOT EXISTS by_repository ON experiences(repository, active)")
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='repairs'").fetchone():
            for task, repo, raw in db.execute("SELECT task_id, repository, record FROM repairs"):
                data = json.loads(raw)
                data.update(schema_version=2, source_task=task, outcome="accepted",
                            source_report=f"runs/{task}/report.json", failure_category="legacy", candidate=None)
                db.execute("INSERT INTO experiences(id,repository,record) VALUES(?,?,?)", (task, repo, json.dumps(data)))
            db.execute("DROP TABLE repairs")
        db.execute("PRAGMA user_version=2")

    def save_experience(self, task: dict, candidate: dict, validation: dict, signature: str):
        number = validation["candidate"]
        outcome = validation["status"]
        identifier = task["id"] if outcome == "accepted" else f"{task['id']}.{number}"
        record: RepairExperience = {
            "schema_version": 2, "source_task": task["id"], "source_report": f"runs/{task['id']}/report.json",
            "candidate": number, "commit": task["commit"], "targets": task["targets"],
            "signature": signature[-1000:], "hypothesis": candidate["hypothesis"], "evidence": candidate["evidence"],
            "outcome": outcome, "failure_category": validation.get("reason_code") or "test_failure",
            "verified_tests": sum(test["status"] == "passed" for test in validation.get("regression", {}).get("tests", {}).values()),
            "regression": task["regression"], "reason": validation.get("reason"),
        }
        self._store(identifier, task["repository_key"], record)

    def save(self, task_id: str, repository: str, record: dict):
        """Accept the original accepted-repair payload from older callers."""
        self._store(task_id, repository, {**record, "schema_version": 2, "source_task": task_id,
                    "source_report": f"runs/{task_id}/report.json", "outcome": "accepted",
                    "candidate": None, "failure_category": "legacy"})

    def _store(self, identifier, repository, record):
        self.initialize()
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("INSERT INTO experiences(id,repository,record) VALUES(?,?,?) ON CONFLICT(id) "
                       "DO UPDATE SET record=excluded.record", (identifier, repository, json.dumps(record)))

    def list_experiences(self, repo):
        self.initialize()
        with closing(sqlite3.connect(self.database)) as db:
            rows = db.execute("SELECT id,record,active,checked FROM experiences WHERE repository=? ORDER BY rowid DESC", (repo,))
            return [{**json.loads(raw), "id": key, "active": bool(active), "last_checked": checked}
                    for key, raw, active, checked in rows]

    def invalidate(self, repo, identifier, *, delete=False):
        self.initialize()
        with closing(sqlite3.connect(self.database)) as db, db:
            action = "DELETE FROM experiences" if delete else "UPDATE experiences SET active=0"
            if not db.execute(action + " WHERE repository=? AND id=?", (repo, identifier)).rowcount:
                raise ValueError("Experience not found in this repository")

    def adopt_legacy(self, repo, old_key, repository):
        from ci_repair.workspace import commit_id

        for record in self.list_experiences(old_key):
            try:
                commit_id(repository, record["commit"])
            except ValueError:
                continue
            with closing(sqlite3.connect(self.database)) as db, db:
                db.execute("UPDATE experiences SET repository=? WHERE id=? AND repository=?", (repo, record["id"], old_key))

    def source_exists(self, record):
        root = self.database.parent.resolve()
        path = (root / record["source_report"]).resolve()
        if not path.is_relative_to(root / "runs"):
            return False
        try:
            report = read_report(path)
            if report["task"]["id"] != record["source_task"] or report["task"]["commit"] != record["commit"]:
                return False
            for verdict in report["validations"]:
                if verdict["status"] != record["outcome"] or record["candidate"] not in (None, verdict["candidate"]):
                    continue
                if record["outcome"] == "accepted" and not all(phase in verdict for phase in ("target", "regression")):
                    return False
                files = [verdict.get("candidate_diff")]
                for phase in ("target", "regression"):
                    if phase in verdict:
                        files.append(verdict[phase]["artifacts"].get("pytest_report"))
                for name in files:
                    if not name:
                        return False
                    file = (path.parent / name).resolve()
                    if not file.is_relative_to(path.parent) or not file.is_file():
                        return False
                    if expected := report.get("evidence_hashes", {}).get(name):
                        with file.open("rb") as stream:
                            if file_digest(stream, "sha256").hexdigest() != expected:
                                return False
                if candidates := report.get("candidates"):
                    candidate = candidates[verdict["candidate"] - 1]
                    if (path.parent / files[0]).read_bytes() != candidate["diff"].encode("utf-8"):
                        return False
                return True
        except (OSError, KeyError, IndexError, TypeError, ValueError):
            pass
        return False

    def search(self, repository: str, query: str, limit: int = 3, *, exclude_task=None) -> list[dict]:
        if limit <= 0:
            return []
        words = tokens(query)
        ranked = []
        for record in self.list_experiences(repository):
            if not record["active"] or record["source_task"] == exclude_task:
                continue
            names = tokens(" ".join(record["targets"]))
            overlap = words & names
            if not overlap:
                continue
            exact = any(node in query for node in record["targets"])
            score = 4 * exact + len(overlap) / max(len(names), 1) + len(words & tokens(record["signature"])) / max(len(words), 1)
            ranked.append((score, record["outcome"] == "accepted", record))
        results = []
        with closing(sqlite3.connect(self.database)) as db, db:
            for score, _, record in sorted(ranked, key=lambda row: row[:2], reverse=True):
                valid = self.source_exists(record)
                checked = timestamp()
                db.execute("UPDATE experiences SET checked=? WHERE id=?", (checked, record["id"]))
                if valid:
                    results.append({**record, "last_checked": checked, "score": score})
                    if len(results) >= limit:
                        break
        return results
