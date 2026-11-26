import argparse
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from importlib.metadata import version
import json
from pathlib import Path
import platform
import subprocess
import sys
from time import perf_counter
from xml.etree.ElementTree import ParseError

from ci_repair.artifacts import write_json
from ci_repair.budget import DEFAULTS
from ci_repair.retrieval import build_index, failure_query, retrieve_code
from ci_repair.workspace import git
from manifest import load_cases
from isolated import image_id
from prepare import independent_tests, prepare, upstream
from summarize import summarize


TARGET = "test_public.py::test_target"
MODES = ("tools", "rag", "feedback", "memory")


def calibrate(case, cache, directory, image=None):
    row = {"case": case.id, "split": case.split, "mode": "reference", "bad": case.bad, "fix": case.fix,
           "source": case.source, "calibrated": False, "status": "environment_error"}
    directory.mkdir(parents=True)
    try:
        view, patch = prepare(case, cache, directory)
        row["snapshot"] = git(view, "rev-parse", "HEAD").strip()
        bad = independent_tests(case, view, directory / "original", None, image)
        bad["log"] = (Path(bad["log"]).relative_to(directory.parent)).as_posix()
        row["original"] = bad
        fixed = independent_tests(case, view, directory / "reference", patch, image)
        fixed["log"] = (Path(fixed["log"]).relative_to(directory.parent)).as_posix()
        row.update(original=bad, reference=fixed)
        expected = {"test_target": "failure", "test_regression": "passed", "test_hidden": "failure"}
        if bad["tests"] != expected or bad["exit"] != 1:
            row["status"] = "baseline_mismatch"
        elif fixed["exit"] != 0 or fixed["tests"] != dict.fromkeys(expected, "passed"):
            row["status"] = "reference_mismatch"
        else:
            row.update(calibrated=True, status="calibrated", localization={})
            index = directory / "index"
            stats = build_index(view, row["snapshot"], index)
            row["index"] = stats
            for mode in ("literal", "bm25"):
                search = retrieve_code(row["snapshot"], index, {
                    "text": failure_query([TARGET], bad["public_failure"]), "mode": mode, "limit": 6, "token_budget": 7000})
                found = [h["path"] + ":" + h["symbol"] for h in search["context"]]
                row["localization"][mode] = {"hits": len(set(found) & set(case.gold)), "labels": len(case.gold),
                                             "seconds": search["seconds"], "context": found,
                                             "estimated_tokens": search["estimated_tokens"]}
    except (OSError, ValueError, RuntimeError, ParseError, subprocess.SubprocessError) as error:
        row.update(calibrated=False, status="environment_error")
        row["reason"] = str(error)
    write_json(directory / "calibration.json", row)
    return row


def agent_args(view, storage, mode, model):
    command = ["--state-dir", str(storage), "fix", "--repo", str(view), "--model", model, "--strategy", "sequential",
               "--test", TARGET, "--regression", "test_public.py", "--retrieval", "none" if mode == "tools" else "bm25"]
    if mode != "memory":
        command.append("--no-memory")
    if mode in ("tools", "rag"):
        command.append("--no-retry-feedback")
    return command


def run_agent(view, storage, mode, model, image):
    command = [sys.executable, str(Path(__file__).with_name("agent_entry.py")), image, *agent_args(view, storage, mode, model)]
    start = perf_counter()
    run = subprocess.run(command, capture_output=True, encoding="utf-8", errors="replace", timeout=DEFAULTS["seconds"] + 120)
    task = next((line.removeprefix("Task ID: ") for line in run.stdout.splitlines() if line.startswith("Task ID: ")), None)
    if not task:
        raise ValueError(run.stderr[-1200:] or "Agent did not start")
    path = storage / "runs" / task / "report.json"
    return path, json.loads(path.read_text(encoding="utf-8")), round(perf_counter()-start, 3)


def costs(report, directory):
    def records(field):
        return [json.loads((directory / name).read_text(encoding="utf-8")) for name in report.get(field, [])]
    retrieval = records("retrieval")
    return {"resources": {key: len(values) for key, values in report.get("budget", {}).get("reservations", {}).items()},
            "usage": report.get("model_usage", []),
            "served_models": sorted({c["served_model"] for c in records("model_calls") if c.get("served_model")}),
            "index_seconds": sum(r["index"]["seconds"] for r in retrieval),
            "retrieval_seconds": sum(r["seconds"] for r in retrieval)}


def trial_status(report):
    if report["status"] == "interrupted":
        return "model_request_error" if "ModelStepError" in (report.get("reason") or "") else "execution_error"
    return report.get("reason_code") or report["status"]


def seed_for(case, seeds):
    eligible = [s for s in seeds if s.split == "development" and s.repository == case.repository and s.date < case.date]
    return max(eligible, key=lambda s: (s.date, s.id), default=None)


def attempt(case, row, mode, model, repeat, output, seeds, image):
    case_dir = output / case.id
    directory = case_dir / f"{mode}-{repeat}"
    directory.mkdir()
    result = {k: row[k] for k in ("case", "split", "calibrated", "source", "bad", "fix")}
    result.update(mode=mode, model=model, repeat=repeat, limits=DEFAULTS, status=row["status"])
    if not row["calibrated"]:
        result["reason"] = row.get("reason", row["status"])
        return result
    try:
        storage = directory / "state"
        if mode == "memory":
            # An independent development attempt may contribute evidence. Never seed from a reference diff.
            seed = seed_for(case, seeds)
            if seed:
                calibration = json.loads((output / seed.id / "calibration.json").read_text(encoding="utf-8"))
                if not calibration["calibrated"]:
                    raise ValueError(f"Development seed {seed.id}: {calibration.get('reason', calibration['status'])}")
                path, report, seconds = run_agent(output / seed.id / "repo", storage, mode, model, image)
                result["seed"] = {"case": seed.id, "status": report["status"], "seconds": seconds,
                                  "report": path.relative_to(output).as_posix(), **costs(report, path.parent)}
        path, report, seconds = run_agent(case_dir / "repo", storage, mode, model, image)
        checks = report.get("validations", [])
        result.update(report=path.relative_to(output).as_posix(), seconds=seconds, status=trial_status(report),
                      reason=report.get("reason"), accepted=report["status"] == "accepted", validated=len(checks),
                      interceptions=sum(bool(c.get("regressions")) for c in checks), snapshot=row["snapshot"],
                      history_matches=len(report.get("history", [])), **costs(report, path.parent))
        if result["accepted"]:
            result["status"] = "independent_check_error"
            hidden = independent_tests(case, case_dir / "repo", directory / "judge", path.with_name("fix.diff").read_bytes(), image)
            hidden["log"] = Path(hidden["log"]).relative_to(output).as_posix()
            result["independent"] = hidden
            if hidden["exit"] == 0 and hidden["tests"] == dict.fromkeys(("test_hidden", "test_target", "test_regression"), "passed"):
                result["status"] = "solved"
            elif hidden["exit"] == 1 and "failure" in hidden["tests"].values():
                result["status"] = "false_accept"
    except (OSError, ValueError, RuntimeError, ParseError, subprocess.SubprocessError) as error:
        result.update(status="independent_check_error" if result.get("accepted") else "execution_error", reason=str(error))
    return result


def main(argv):
    parser = argparse.ArgumentParser(description="Calibrate upstream failures and optionally run controlled repair trials")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--cache", default=Path(".ci-repair/s5-upstream"), type=Path)
    parser.add_argument("--split", choices=["all", "development", "evaluation"], default="all")
    parser.add_argument("--model")
    parser.add_argument("--case", action="append", help="Select a case ID, repeat to select several")
    parser.add_argument("--image", help="Local pytest image tag. Required for model trials")
    parser.add_argument("--modes", choices=MODES, nargs="+", default=list(MODES))
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--jobs", type=int, default=1, help="Parallel calibration workers")
    args = parser.parse_args(argv)
    if min(args.repeat, args.jobs) < 1 or sys.version_info[:2] != (3, 12):
        parser.error("Use Python 3.12 and positive repeat/jobs")
    cases = load_cases()
    selected = [c for c in cases if args.split in ("all", c.split) and (not args.case or c.id in args.case)]
    if not selected or set(args.case or []) - {c.id for c in selected}:
        parser.error("Unknown case or case outside selected split")
    if args.model and not args.image:
        parser.error("Model trials require --image for isolated candidate verification")
    try:
        image = image_id(args.image) if args.image else None
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.error(str(error))
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    seeds = [c for c in cases if c.split == "development"]
    seed_ids = {s.id for c in selected if (s := seed_for(c, seeds))} if args.model and "memory" in args.modes else set()
    required = [c for c in cases if c in selected or c.id in seed_ids]
    cache = args.cache.resolve()
    # Warm each mirror once before parallel preparation; failures are recorded per case below.
    for first in {c.repository: c for c in required}.values():
        try:
            upstream(first, cache)
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    with ThreadPoolExecutor(args.jobs) as workers:
        calibrated = dict(zip([c.id for c in required], workers.map(lambda c: calibrate(c, cache, output / c.id, image), required)))
    rows = [calibrated[c.id] for c in selected]
    root = Path(__file__).resolve().parents[1]
    digest = sha256()
    for path in sorted([*(root / "src").rglob("*.py"), *(root / "evals").glob("*.py"),
                        *(root / "evals/cases").glob("*.jsonl"), root / "evals/Dockerfile", root / "uv.lock"]):
        digest.update(path.relative_to(root).as_posix().encode() + b"\0" + path.read_bytes() + b"\0")
    environment = {"python": sys.version, "host_pytest": version("pytest"), "platform": platform.platform(),
                   "agent_commit": git(root, "rev-parse", "HEAD").strip(), "source_sha256": digest.hexdigest(),
                   "model": args.model, "pytest_image": image, "limits": DEFAULTS}
    def save():
        write_json(output / "results.json", {**environment, "rows": rows, "summary": summarize(rows)})
    save()
    if args.model:
        for case in selected:
            for trial in range(1, args.repeat + 1):
                for mode in dict.fromkeys(args.modes):
                    rows.append(attempt(case, calibrated[case.id], mode, args.model, trial, output, seeds, image))
                    save()
    print(json.dumps(summarize(rows), indent=2))
    errors = {"execution_error", "model_request_error", "independent_check_error"}
    return int(any(not r["calibrated"] or r["status"] in errors for r in rows))
