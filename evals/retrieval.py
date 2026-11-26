"""Compare source localization and optionally run real repairs with the same settings."""

import argparse
import json
from pathlib import Path
import subprocess
import sys
from time import monotonic

from ci_repair.retrieval import build_index, failure_query, retrieve_code
from ci_repair.workspace import worktree


CASES = [
    (
        "checkout",
        "examples/checkout/test_checkout.py::test_discount_keeps_free_shipping",
        {("examples/checkout/checkout.py", "checkout_total"), ("examples/checkout/delivery.py", "shipping_fee")},
    ),
    (
        "shipping",
        "examples/shipping/test_shipping.py::test_standard_shipping_at_threshold_is_free",
        {("examples/shipping/shipping.py", "shipping_fee")},
    ),
]


def run_repair(args, commit, name, target, mode):
    storage = args.output / "repairs" / mode
    command = [
        sys.executable, "-c", "from ci_repair.cli import main; raise SystemExit(main())",
        "--state-dir", str(storage), "fix", "--repo", str(args.repo), "--commit", commit,
        "--test", target, "--regression", f"examples/{name}", "--strategy", "batch",
        "--model", args.repair_model, "--retrieval", mode, "--no-memory",
    ]
    if mode == "hybrid":
        command.extend(["--embedding-model", args.embedding_model])
    start = monotonic()
    process = subprocess.run(command, capture_output=True, encoding="utf-8", errors="replace", timeout=900)
    task_id = next((line.removeprefix("Task ID: ") for line in process.stdout.splitlines()
                    if line.startswith("Task ID: ")), None)
    report_path = storage / "runs" / task_id / "report.json" if task_id else None
    report = json.loads(report_path.read_text(encoding="utf-8")) if report_path and report_path.is_file() else {}
    usages = [item["total_tokens"] for item in report.get("model_usage", []) if "total_tokens" in item]
    return {
        "status": report.get("status", "no_report"),
        "reason": report.get("reason"),
        "accepted": report.get("status") == "accepted",
        "model_tokens_reported": sum(usages) if usages else None,
        "milliseconds": round((monotonic() - start) * 1000),
        "report": report_path.relative_to(args.output).as_posix() if report else None,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--commit", default="HEAD")
    parser.add_argument("--top-k", type=int, choices=range(1, 9), default=6)
    parser.add_argument("--embedding-model", help="Enable hybrid retrieval with this provider:model")
    parser.add_argument("--repair-model", help="Also compare verified patch acceptance with this provider:model")
    args = parser.parse_args()
    args.output = args.output.resolve()
    args.repo = args.repo.resolve()
    cache = args.output / "index"
    first = build_index(args.repo, args.commit, cache)
    commit = first["commit"]
    scratch = args.output / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    embedding = None
    modes = ["literal", "bm25"]
    if args.embedding_model:
        from langchain.embeddings import init_embeddings

        embedding = init_embeddings(args.embedding_model)
        modes.append("hybrid")
    results = []
    with worktree(args.repo, commit, scratch) as tree:
        for name, target, expected in CASES:
            pytest_run = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", "--color=no", target],
                cwd=tree, capture_output=True, encoding="utf-8", errors="replace", timeout=120,
            )
            if pytest_run.returncode != 1 or target not in pytest_run.stdout:
                raise SystemExit(f"Could not reproduce the labelled failure for {name}: {pytest_run.stdout[-900:]}")
            query = failure_query([target], pytest_run.stdout)
            for mode in modes:
                search = retrieve_code(
                    commit, cache, {"text": query, "mode": mode, "limit": args.top_k, "token_budget": 7000},
                    embedder=embedding, model_name=args.embedding_model or "",
                )
                found = {(hit["path"], hit["symbol"]) for hit in search["context"]}
                results.append({
                    "case": name, "mode": mode, "recall_at_k": len(expected & found) / len(expected),
                    "expected": sorted(expected), "matched": sorted(expected & found), "search": search,
                })
    if args.repair_model:
        for result in results:
            target = next(target for name, target, _ in CASES if name == result["case"])
            result["repair"] = run_repair(args, commit, result["case"], target, result["mode"])
    summary = {}
    for mode in modes:
        rows = [result for result in results if result["mode"] == mode]
        summary[mode] = {
            "mean_recall_at_k": sum(row["recall_at_k"] for row in rows) / len(rows),
            "mean_context_estimated_tokens": round(sum(row["search"]["estimated_tokens"] for row in rows) / len(rows)),
            "mean_query_ms": round(1000 * sum(row["search"]["seconds"] for row in rows) / len(rows), 2),
        }
        if args.repair_model:
            summary[mode]["accepted_repairs"] = sum(row["repair"]["accepted"] for row in rows)
            summary[mode]["attempted_repairs"] = len(rows)
    report = {
        "commit": commit, "top_k": args.top_k, "case_count": len(CASES),
        "embedding_model": args.embedding_model, "repair_model": args.repair_model,
        "first_index": first, "warm_index": build_index(args.repo, commit, cache),
        "summary": summary, "results": results,
    }
    destination = args.output / "results.json"
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"Results: {destination}")


if __name__ == "__main__":
    main()
