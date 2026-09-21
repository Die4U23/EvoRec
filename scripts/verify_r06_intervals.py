"""Repeat only R06 user-cluster intervals from real saved ranks, without retraining."""
import argparse
import gzip
import hashlib
import json
from pathlib import Path

import numpy as np

from evorec.research.evaluation import ranking_metrics
from evorec.research.ranker_data import read
from evorec.research.r06_data import load_inputs, group_masks
from evorec.research.replication_analysis import query_users
from evorec.research.replicate_ranker import checked_file
from evorec.research.runner import file_sha
from evorec.research.uncertainty import clustered_mean_interval


def verify(run, output):
    series, analysis = read(run/"series.json"), read(run/"analysis.json")
    if series["status"] != "completed" or analysis["status"] != "passed":
        raise ValueError("complete run and passed analysis required")
    if file_sha(run/"series.json") != analysis["series_sha256"]:
        raise ValueError("analysis binding differs")
    config = series["configuration"]
    _, protocol, _, _, _, _ = load_inputs(config)
    queries = protocol.queries("test", test_authorized=True)
    users = query_users(protocol.events, queries, config)
    values = {}
    for row in series["test_results"]:
        points = []
        with gzip.open(checked_file(row["trace"]), "rt", encoding="utf-8") as stream:
            for query in queries:
                saved = json.loads(stream.readline())
                if saved["query_id"] != query.query_id:
                    raise ValueError("query order differs")
                metrics = ranking_metrics(saved["recommendations"], query.target)
                if metrics != saved["metrics"]:
                    raise ValueError("rank metric differs")
                points.append([metrics["ndcg@10"], metrics["recall@20"]])
            if stream.readline():
                raise ValueError("extra query rows")
        values[row["name"]] = np.asarray(points)
    for family in ("A-frozen", "B-frozen", "C-adapted", "D-adapted"):
        values[family] = np.stack([values[f"{family}-s{seed}"] for seed in config["seeds"]]).mean(axis=0)
    matched = 0
    settings = config["bootstrap"]
    for cohort in settings["cohorts"]:
        mask = group_masks(queries)[cohort]
        columns, expected = [], []
        for method, baseline in settings["contrasts"]:
            for index, metric in enumerate(settings["metrics"]):
                columns.append((values[method]-values[baseline])[mask,index])
                expected.append(next(row for row in analysis["intervals"] if
                    (row["cohort"], row["method"], row["baseline"], row["metric"]) == (cohort,method,baseline,metric)))
        result = clustered_mean_interval(np.column_stack(columns), users[mask], replicates=settings["replicates"],
                                         seed=settings["seed"], confidence=settings["confidence"])
        for index,row in enumerate(expected):
            if row["status"] != "estimated":
                raise ValueError("repeat currently expects estimable cohorts")
            for key in ("estimate", "low", "high"):
                if row[key] != result[key][index]:
                    raise ValueError("recomputed interval differs")
            if row["requests"] != result["requests"] or row["users"] != result["users"]:
                raise ValueError("bootstrap grouping differs")
            matched += 1
        print(json.dumps({"phase":"interval_repeat","cohort":cohort,"matched":len(expected)}),flush=True)
    result={"status":"passed","intervals_recomputed":matched,"all_point_estimates_and_bounds_identical":True,
            "series_sha256":file_sha(run/"series.json"),"analysis_sha256":file_sha(run/"analysis.json"),
            "checker_sha256":file_sha(Path(__file__)),"source":"metrics independently recomputed from saved recommendation ranks"}
    output.write_text(json.dumps(result,indent=2)+"\n",encoding="utf-8")
    return result


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run",type=Path,required=True)
    parser.add_argument("--output",type=Path,default=Path("docs/validation/r06-interval-repeat.json"))
    args=parser.parse_args()
    print(json.dumps(verify(args.run,args.output)))
