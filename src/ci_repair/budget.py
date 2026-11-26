"""Durable reservations under the CLI's exclusive per-task lock."""

import json
from pathlib import Path
from time import time
from uuid import uuid4

from ci_repair.artifacts import write_json
from ci_repair.schemas import BudgetState


DEFAULTS = {"model_calls": 12, "tool_calls": 18, "candidates": 3, "tests": 16, "seconds": 900}


class BudgetExhausted(RuntimeError):
    pass


class Budget:
    def __init__(self, directory: Path, limits: dict):
        self.path = directory / "budget.json"
        self.state: BudgetState
        if self.path.exists():
            self.state = json.loads(self.path.read_text(encoding="utf-8"))
            changed = False
            for identifier, entry in self.state["reservations"].get("model_calls", {}).items():
                if entry.get("status") in ("succeeded", "request_failed"):
                    continue
                record = directory / "artifacts" / "model" / f"{identifier}.json"
                if record.is_file():
                    call = json.loads(record.read_text(encoding="utf-8"))
                    if call["status"] != "running":
                        entry.update(usage=call["usage"], status=call["status"])
                        changed = True
            if changed:
                write_json(self.path, self.state)
        else:
            self.state = {"limits": limits, "deadline": time() + limits["seconds"], "reservations": {}}
            write_json(self.path, self.state)

    def check_budget(self):
        remaining = self.state["deadline"] - time()
        if remaining <= 0:
            raise BudgetExhausted("seconds")
        return remaining

    def reserve_call(self, kind, identifier=None):
        self.check_budget()
        entries = self.state["reservations"].setdefault(kind, {})
        identifier = identifier or uuid4().hex
        if identifier not in entries:
            if len(entries) >= self.state["limits"][kind]:
                raise BudgetExhausted(kind)
            entries[identifier] = {"usage": None}
            write_json(self.path, self.state)
        return identifier

    def record_usage(self, identifier, usage):
        self.state["reservations"]["model_calls"][identifier].update(usage=usage, status="succeeded")
        write_json(self.path, self.state)
