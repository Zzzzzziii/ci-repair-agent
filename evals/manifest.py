import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath


@dataclass(frozen=True)
class BenchmarkCase:
    id: str
    split: str
    repository: str
    bad: str
    fix: str
    date: str
    source: str
    package: str
    allowed_history: str
    environment: str
    gold: list[str]
    imports: str
    target: str
    regression: str
    hidden: str

    @property
    def source_root(self):
        return str(PurePosixPath(self.package).parent)


def load_cases(folder=Path(__file__).with_name("cases")):
    cases = []
    for split in ("development", "evaluation"):
        for line in (folder / f"{split}.jsonl").read_text(encoding="utf-8").splitlines():
            case = BenchmarkCase(**json.loads(line))
            if not (case.split == split and re.fullmatch(r"case-\d+", case.id)
                    and re.fullmatch(r"[\w.-]+/[\w.-]+", case.repository)
                    and re.fullmatch(r"\w+(?:/\w+)*", case.package)
                    and all(re.fullmatch("[0-9a-f]{40}", sha) for sha in (case.bad, case.fix))
                    and case.bad != case.fix and case.allowed_history == "snapshot_only"
                    and case.environment == "cpython3.12-pytest"
                    and case.source == f"https://github.com/{case.repository}/commit/{case.fix}"
                    and date.fromisoformat(case.date).isoformat() == case.date
                    and case.gold and all(label.startswith(case.package + "/") and ".py:" in label
                                          and ".." not in PurePosixPath(label).parts for label in case.gold)
                    and all(text.strip() for text in (case.target, case.regression, case.hidden))
                    and case.hidden != case.target):
                raise ValueError(f"Invalid benchmark manifest entry {case.id}")
            cases.append(case)
    if len({c.id for c in cases}) != len(cases) or len({(c.repository, c.fix) for c in cases}) != len(cases):
        raise ValueError("Duplicate benchmark case")
    if max(c.date for c in cases if c.split == "development") >= min(c.date for c in cases if c.split == "evaluation"):
        raise ValueError("Development history must predate evaluation")
    return cases
