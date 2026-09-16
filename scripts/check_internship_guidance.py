"""Recheck internship-guidance claims against saved R05 traces, without training."""
import gzip
import hashlib
import json
import math
from collections import Counter
from pathlib import Path

from evorec.research.data import load_events

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def check():
    run = ROOT / "artifacts/runs/r05-ranker-20260916"
    source = run / "series.json"
    series = read(source)
    assert series["status"] == "completed"
    results = {row["name"]: row for row in series["test_results"]}
    names = ("CF-blend", "RRF", "ListMLP-s17", "ColdListMLP-s17")
    output = {}
    reference = None
    input_hashes = {"series.json": sha(source)}
    for name in names:
        path = run / f"test-{name}.jsonl.gz"
        input_hashes[path.name] = sha(path)
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            rows = [json.loads(line) for line in stream]
        assert len({row["query_id"] for row in rows}) == len(rows)
        identities = [(r["query_id"], r["timestamp_ms"], r["target"], r["history"]) for r in rows]
        if reference is None:
            reference = identities
            cold_fallback = {
                r["query_id"]: r["recommendations"] for r in rows if not r["history"]
            }
        else:
            assert reference == identities
            assert all(
                r["recommendations"] == cold_fallback[r["query_id"]]
                for r in rows if not r["history"]
            )
        cohorts = {}
        for cohort in ("all_positive_events", "history_present", "cold_user", "model_cold_available"):
            selected = [
                r for r in rows
                if cohort == "all_positive_events"
                or (cohort == "history_present" and r["history"])
                or (cohort == "cold_user" and not r["history"])
                or (cohort == "model_cold_available"
                    and r["target_available"] and r["target_model_cold"])
            ]
            count = len(selected)
            expected = results[name]["metrics"]["cohorts"][cohort]
            assert count == expected["n"]
            ndcg = math.fsum(r["metrics"]["ndcg@10"] for r in selected) / count
            hits = sum(r["metrics"]["recall@20"] for r in selected)
            assert math.isclose(ndcg, expected["ndcg@10"], abs_tol=1e-14)
            assert math.isclose(hits / count, expected["recall@20"], abs_tol=1e-14)
            cohorts[cohort] = {"n": count, "ndcg@10": ndcg, "hits@20": int(hits),
                               "recall@20": hits / count}
        assert cohorts["history_present"]["n"] + cohorts["cold_user"]["n"] == len(rows)
        output[name] = cohorts
    sample = ROOT / series["configuration"]["dataset_path"]
    events, stats = load_events(sample)
    user_counts = Counter(event.user_id for event in events)
    single = sum(count == 1 for count in user_counts.values())
    # Query ID reconstruction checks whether events within a user are independent units.
    user_for_query = {}
    threshold = series["configuration"]["positive_rating_min"]
    start = series["configuration"]["validation_end_ms"]
    for event in events:
        if event.timestamp_ms >= start and event.rating >= threshold:
            identity = f"{event.user_id}\0{event.item_id}\0{event.timestamp_ms}"
            query_id = hashlib.sha256(identity.encode()).hexdigest()[:24]
            assert query_id not in user_for_query
            user_for_query[query_id] = event.user_id
    assert set(user_for_query) == {identity[0] for identity in reference}
    requests_per_user = Counter(user_for_query.values())
    ratios = {
        name: {
            group: output[name][group]["ndcg@10"] / output["CF-blend"][group]["ndcg@10"] - 1
            for group in ("all_positive_events", "history_present")
        }
        for name in names if name != "CF-blend"
    }
    result = {
        "status": "passed",
        "scope": "posthoc guidance fact check; no new training or model selection",
        "script_sha256": sha(Path(__file__)),
        "protocol_id": series["protocol_id"],
        "inputs_sha256": input_hashes,
        "sample_sha256": sha(sample),
        "sample_statistics": stats,
        "users_with_exactly_one_deduplicated_interaction": single,
        "single_interaction_user_fraction": single / len(user_counts),
        "test_users": len(requests_per_user),
        "test_users_with_multiple_positive_events": sum(n > 1 for n in requests_per_user.values()),
        "maximum_test_positive_events_per_user": max(requests_per_user.values()),
        "test_results": output,
        "relative_ndcg_change_vs_cf": ratios,
        "identical_no_history_recommendations_for_four_methods": True,
        "test_pool_diagnostics": series["test_pool_diagnostics"],
        "limits": [
            "this recheck trusts saved per-query metrics; the independent ranker audit reconstructs ranks",
            "four R05 methods only; no claim that all past methods have identical fallbacks",
            "query-weighted metrics; user clusters should be retained in uncertainty estimation",
            "no bootstrap confidence intervals calculated by this script",
        ],
    }
    destination = ROOT / "docs/validation/internship-guidance-checks.json"
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    check()
