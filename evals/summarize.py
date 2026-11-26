from collections import Counter


def ratio(n, d):
    return {"numerator": n, "denominator": d, "rate": n / d if d else None}


def summarize(rows):
    groups = {}
    for key in sorted({(r["split"], r["mode"]) for r in rows}):
        group = [r for r in rows if (r["split"], r["mode"]) == key]
        reproduced = [r for r in group if r.get("calibrated")]
        summary = {"cases": len({r["case"] for r in group}), "attempts": len(group),
                   "reproduction": ratio(len(reproduced), len(group)),
                   "statuses": dict(Counter(r["status"] for r in group))}
        if key[1] == "reference":
            summary["index_seconds"] = round(sum(r.get("index", {}).get("seconds", 0) for r in group), 3)
            summary["verification_seconds"] = round(sum(r.get(stage, {}).get("seconds", 0) for r in group
                                                       for stage in ("original", "reference")), 3)
            for mode in ("literal", "bm25"):
                samples = [r["localization"][mode] for r in reproduced if mode in r.get("localization", {})]
                summary[f"{mode}_recall_at_6"] = ratio(sum(s["hits"] for s in samples), sum(s["labels"] for s in samples))
                summary[f"{mode}_query_seconds"] = round(sum(s["seconds"] for s in samples), 3)
        else:
            solved = sum(r["status"] == "solved" for r in reproduced)
            accepted = [r for r in reproduced if r.get("accepted")]
            judged = [r for r in accepted if r["status"] in ("solved", "false_accept")]
            costs = Counter()
            for row in group:
                costs.update(row.get("resources", {}))
            tokens = [call for row in group for call in row.get("usage", [])]
            summary.update(resources=dict(costs), reported_tokens=sum(c.get("total_tokens", 0) for c in tokens),
                           calls_without_token_count=sum("total_tokens" not in c for c in tokens),
                           trial_seconds=round(sum(r.get("seconds", 0) for r in group), 3),
                           index_seconds=round(sum(r.get("index_seconds", 0) for r in group), 3),
                           query_seconds=round(sum(r.get("retrieval_seconds", 0) for r in group), 3),
                           seed_seconds=round(sum(r.get("seed", {}).get("seconds", 0) for r in group), 3),
                           judge_seconds=round(sum(r.get("independent", {}).get("seconds", 0) for r in group), 3))
            summary.update(solved_all=ratio(solved, len(group)), solved_reproducible=ratio(solved, len(reproduced)),
                           false_accept=ratio(sum(r["status"] == "false_accept" for r in judged), len(judged)),
                           accepted_unjudged=len(accepted)-len(judged),
                           regression_interceptions=ratio(sum(r.get("interceptions", 0) for r in group),
                                                          sum(r.get("validated", 0) for r in group)))
        groups["/".join(key)] = summary
    return groups
