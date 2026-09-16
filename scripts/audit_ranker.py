"""Independent time/query/metric and training-cache audit for completed R05."""
import argparse
import csv
import gzip
import hashlib
import json
import math
import statistics
from bisect import bisect_left
from collections import Counter,defaultdict,deque
from datetime import datetime,timezone
from itertools import groupby
from pathlib import Path

import numpy as np


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream,"sha256").hexdigest()


def events(path):
    with Path(path).open(newline="",encoding="utf-8") as stream:
        rows=sorted((int(r["timestamp"]),r["user_id"],r["parent_asin"],float(r["rating"])) for r in csv.DictReader(stream))
    assert len({(u,i) for _,u,i,_ in rows})==len(rows)
    return rows


def queries(rows,train_items,catalog,start,stop,config):
    histories=defaultdict(lambda:deque(maxlen=config["history_limit"]))
    seen=defaultdict(set); result=[]
    for time,group in groupby(rows,key=lambda row:row[0]):
        if time>=stop: break
        batch=list(group)
        for _,user,item,rating in batch:
            if time>=start and rating>=config["positive_rating_min"]:
                result.append({"query_id":hashlib.sha256(f"{user}\0{item}\0{time}".encode()).hexdigest()[:24],
                               "timestamp_ms":time,"target":item,"history":list(histories[user]),
                               "seen":frozenset(seen[user]),"target_available":catalog[item]<time,
                               "target_model_cold":item not in train_items})
        for _,user,item,rating in batch:
            seen[user].add(item)
            if rating>=config["positive_rating_min"]: histories[user].append(item)
    return result


def match_query(q,row):
    for key in ("query_id","timestamp_ms","target","history","target_available","target_model_cold"):
        assert q[key]==row[key],(q["query_id"],key)


def audit(run,output):
    s=read(run/"series.json"); c=s["configuration"]
    assert s["status"]=="completed"
    assert read(run/"configuration.json")==c
    for name,expected in s["code"]["source_sha256"].items():
        assert sha(run/"source"/name)==expected
    for source,key in ((c["dataset_path"],s["data_provenance"]["sample_sha256"]),
                       (s["model_training_provenance"]["catalog_path"],s["model_training_provenance"]["catalog_sha256"]),
                       ("datasets/video_games_r03.csv",s["model_training_provenance"]["sample_sha256"]),
                       (c["metadata_path"],s["metadata_provenance"]["metadata_sha256"])):
        assert sha(source)==key
    query_rows=events(c["dataset_path"]); model_rows=events("datasets/video_games_r03.csv")
    users={r[1] for r in query_rows}
    for stage in ("r01","r02","r03","r04"):
        assert not users & {r[1] for r in events(f"datasets/video_games_{stage}.csv")}
    catalog=read(s["data_provenance"]["catalog_path"]); catalog_times=sorted(catalog.values())
    train_items={i for t,_,i,_ in model_rows if t<c["train_end_ms"]}
    encoder=s["content_encoder"]
    for name,expected in encoder["files"].items():
        assert sha(run/"content-encoder"/name)==expected
    vectors=np.load(run/"content-encoder/vectors.npy")
    items=read(run/"content-encoder/items.json"); mapping={item:i for i,item in enumerate(items)}
    digest=hashlib.sha256(vectors.tobytes(order="C")); digest.update(json.dumps(items).encode())
    assert digest.hexdigest()==s["feature_fingerprint"]
    metadata=read(c["metadata_path"])
    fit_items=sorted({i for t,_,i,r in model_rows if t<c["encoder_fit_end_ms"] and r>=4 and metadata.get(i,"").strip()})
    assert len(fit_items)==encoder["fit_document_count"]
    assert hashlib.sha256("\n".join(fit_items).encode()).hexdigest()==encoder["fit_item_set_sha256"]
    import joblib
    fitted=joblib.load(run/"content-encoder/encoder.joblib")
    analyzer=fitted["vectorizer"].build_analyzer()
    df=Counter()
    for item in fit_items: df.update(set(analyzer(metadata[item])))
    for term,index in fitted["vectorizer"].vocabulary_.items():
        expected=math.log((1+len(fit_items))/(1+df[term]))+1
        assert math.isclose(float(fitted["vectorizer"].idf_[index]),expected,abs_tol=2e-6)
    assert sha(s["training_cache"]["path_from_project_root"])==s["training_cache"]["sha256"]
    training_file=np.load(s["training_cache"]["path_from_project_root"])
    training={key:training_file[key] for key in training_file.files}
    training_file.close()
    query_index={key:i for i,key in enumerate(training["query_ids"])}
    assert len(query_index)==len(training["query_ids"])
    train_ids,contexts,scalars=training["items"],training["contexts"],training["scalars"]
    seen_training=set(); pool_records=pool_candidates=feature_rows=0

    def check_pool(q,row,stats_items,cache=None,index=None):
        nonlocal pool_records,pool_candidates,feature_rows
        match_query(q,row)
        candidates=row["candidates"]
        assert len(candidates)<=c["pool_k"] and len(set(candidates))==len(candidates)
        assert not q["seen"].intersection(candidates)
        assert all(catalog[item]<q["timestamp_ms"] for item in candidates)
        position=candidates.index(q["target"]) if q["target"] in candidates else -1
        assert position==row["target_position"]
        raw=np.zeros(vectors.shape[1],dtype=np.float64)
        weight_sum=0.
        for distance,item in enumerate(reversed(q["history"])):
            if item in mapping and np.linalg.norm(vectors[mapping[item]])>1e-8:
                weight=c["content"]["history_decay"]**distance
                raw+=weight*vectors[mapping[item]].astype("float64"); weight_sum+=weight
        norm=np.linalg.norm(raw)
        effective=norm>1e-8
        assert row["has_content_history"]==bool(effective)
        if cache is not None:
            ids=cache["items"][index]; x=cache["scalars"][index]
            actual=[items[int(i)-1] for i in ids if i]
            assert actual==candidates
            assert not np.any(ids[len(candidates):])
            context=raw/norm if effective else raw
            np.testing.assert_allclose(cache["contexts"][index],context,atol=2e-6)
            n=len(candidates)
            if n:
                np.testing.assert_allclose(x[:n,0],vectors[[mapping[i] for i in candidates]]@context,atol=2e-6)
                np.testing.assert_array_equal(x[:n,4],[float(i not in stats_items) for i in candidates])
                age=[min(1.,math.log1p((q["timestamp_ms"]-catalog[i])/86400000)/math.log1p(3650)) for i in candidates]
                np.testing.assert_allclose(x[:n,5],age,atol=2e-7)
                np.testing.assert_allclose(x[:n,6],math.log1p(min(len(q["history"]),50))/math.log1p(50),atol=2e-7)
                coherence=min(1.,norm/weight_sum) if weight_sum else 0.
                np.testing.assert_allclose(x[:n,7],coherence,atol=2e-7)
                assert np.all((x[:n,1]>0)|(x[:n,2]>0))
                assert np.isfinite(x).all()
            feature_rows+=1
        pool_records+=1; pool_candidates+=len(candidates)
        return position,effective

    for fold in s["training_folds"]:
        prefix=[r for r in model_rows if r[0]<fold["start_ms"]]
        signature=hashlib.sha256("\n".join(f"{t}|{u}|{i}|{r}" for t,u,i,r in prefix).encode()).hexdigest()
        assert signature==fold["statistics_signature"] and len(prefix)==fold["statistics_rows"]
        fold_items={r[2] for r in prefix}
        all_q=[q for q in queries(model_rows,fold_items,catalog,fold["start_ms"],fold["end_ms"],c) if q["history"]]
        assert len(all_q)==fold["all_history_queries"]
        sampled=sorted(all_q,key=lambda q:hashlib.sha256(q["query_id"].encode()).digest())[:c["max_training_queries_per_fold"]]
        sampled.sort(key=lambda q:(q["timestamp_ms"],q["query_id"]))
        assert sha(fold["trace"]["path_from_project_root"])==fold["trace"]["sha256"]
        used=used_cold=misses=0
        with gzip.open(fold["trace"]["path_from_project_root"],"rt",encoding="utf-8") as stream:
            for q,line in zip(sampled,stream,strict=True):
                row=json.loads(line)
                index=query_index.get(q["query_id"])
                position,effective=check_pool(q,row,fold_items,training if index is not None else None,index)
                eligible=position>=0 and effective
                assert row["eligible_training"]==eligible
                assert (index is not None)==eligible
                misses+=position<0
                if eligible:
                    assert training["targets"][index]==position
                    assert bool(training["cold"][index])==q["target_model_cold"]
                    assert q["timestamp_ms"]<c["train_end_ms"]
                    seen_training.add(q["query_id"]); used+=1; used_cold+=q["target_model_cold"]
        assert used==fold["training_examples"] and used_cold==fold["cold_training_examples"]
        assert misses==fold["skipped_candidate_miss"]
    assert len(seen_training)==s["training_examples"]==len(query_index)
    assert int(training["cold"].sum())==s["cold_training_examples"]
    checked=checked_candidates=0; slice_results=[]
    metric_keys=("ndcg@10","recall@20","candidate_recall@200")
    for split,start,stop in (("validation",c["train_end_ms"],c["validation_end_ms"]),("test",c["validation_end_ms"],math.inf)):
        expected=queries(query_rows,train_items,catalog,start,stop,c)
        cache_info=s[split+"_cache"]; assert sha(cache_info["path_from_project_root"])==cache_info["sha256"]
        cache_file=np.load(cache_info["path_from_project_root"])
        cache={key:cache_file[key] for key in cache_file.files}
        cache_file.close()
        ptrace=s[split+"_pool_trace"]; assert sha(ptrace["path_from_project_root"])==ptrace["sha256"]
        pools=[]
        with gzip.open(ptrace["path_from_project_root"],"rt",encoding="utf-8") as stream:
            for index,(q,line) in enumerate(zip(expected,stream,strict=True)):
                row=json.loads(line); check_pool(q,row,train_items,cache,index)
                assert cache["targets"][index]==row["target_position"]
                pools.append(set(row["candidates"]))
        results=list(s[split+"_results"])
        if split=="validation":
            results += [{"name":t["name"],"metrics":t["best_validation"],"trace":t["validation_trace"]} for t in s["trials"]]
        for result in results:
            trace=result["trace"]; assert sha(trace["path_from_project_root"])==trace["sha256"]
            totals={group:{"n":0,"sum":[0.,0.,0.],"items":set(),"catalog":0} for group in result["metrics"]["cohorts"]}
            slices={name:{"n":0,"hits20":0} for name in ("cold_with_history","cold_without_history")}
            with gzip.open(trace["path_from_project_root"],"rt",encoding="utf-8") as stream:
                for index,(q,line) in enumerate(zip(expected,stream,strict=True)):
                    row=json.loads(line); match_query(q,row)
                    recs=row["recommendations"]
                    assert len(recs)<=200 and len(set(recs))==len(recs)
                    assert not q["seen"].intersection(recs)
                    assert all(catalog[i]<q["timestamp_ms"] for i in recs)
                    if result.get("family")!="separate-pipeline-reference":
                        assert set(recs)<=pools[index]
                    rank=recs.index(q["target"])+1 if q["target"] in recs else math.inf
                    values=[1/math.log2(rank+1) if rank<=10 else 0.,float(rank<=20),float(rank<=200)]
                    for key,value in zip(metric_keys,values,strict=True):
                        assert math.isclose(row["metrics"][key],value,abs_tol=1e-12)
                    groups=["all_positive_events","history_present" if q["history"] else "cold_user"]
                    if q["target_available"]:
                        groups.append("available_target")
                        if q["target_model_cold"]:
                            groups.append("model_cold_available")
                            sl=slices["cold_with_history" if q["history"] else "cold_without_history"]
                            sl["n"]+=1; sl["hits20"]+=int(rank<=20)
                    for group in groups:
                        acc=totals[group]; acc["n"]+=1
                        acc["sum"]=[a+b for a,b in zip(acc["sum"],values,strict=True)]
                        acc["items"].update(recs[:10]); acc["catalog"]=max(acc["catalog"],bisect_left(catalog_times,q["timestamp_ms"]))
                    checked+=1; checked_candidates+=len(recs)
            for group,acc in totals.items():
                m=result["metrics"]["cohorts"][group]
                assert m["n"]==acc["n"]
                for key,total in zip(metric_keys,acc["sum"],strict=True):
                    assert m[key] is None if not acc["n"] else math.isclose(m[key],total/acc["n"],abs_tol=1e-12)
                assert m["unique_recommended_items@10"]==len(acc["items"])
                assert m["coverage_catalog_items"]==acc["catalog"]
                if acc["catalog"]: assert math.isclose(m["catalog_coverage@10"],len(acc["items"])/acc["catalog"],abs_tol=1e-12)
            slice_results.append({"split":split,"name":result["name"],"slices":slices})
    import torch
    for trial in s["trials"]:
        best=max(trial["history"],key=lambda r:r["validation_ndcg@10"])
        assert best["epoch"]==trial["best_epoch"]
        assert trial["checkpoint_reload_verified"]
        assert sha(trial["checkpoint"]["path_from_project_root"])==trial["checkpoint"]["sha256"]
        cp=torch.load(trial["checkpoint"]["path_from_project_root"],weights_only=True,map_location="cpu")
        assert cp["protocol_id"]==s["protocol_id"] and cp["feature_fingerprint"]==s["feature_fingerprint"]
        assert cp["training_cache_sha256"]==s["training_cache"]["sha256"]
        assert cp["epoch"]==trial["best_epoch"]
    choices=[t for t in s["trials"] if t["seed"]==c["seeds"][0]]
    best=max(choices,key=lambda t:t["best_validation"]["cohorts"]["all_positive_events"]["ndcg@10"])
    assert best["setting"]==s["selected_setting"]
    baseline=next(r for r in s["validation_results"] if r["name"]=="RRF")
    floor=baseline["metrics"]["cohorts"]["all_positive_events"]["ndcg@10"]*c["selection"]["ndcg_min_ratio"]
    assert floor==s["validation_ndcg_floor"]
    options=[baseline]+[{"name":t["name"],"metrics":t["best_validation"]} for t in choices]
    valid=[r for r in options if r["metrics"]["cohorts"]["all_positive_events"]["ndcg@10"]>=floor]
    chosen=max(valid,key=lambda r:(r["metrics"]["cohorts"]["model_cold_available"]["recall@20"],
                                    r["metrics"]["cohorts"]["all_positive_events"]["ndcg@10"],r["name"]=="RRF"))
    assert chosen["name"]==s["selected_method"]
    assert s["selection_finished_at"]<s["test_opened_at"]<s["finished_at"]
    members=[r for r in s["test_results"] if r.get("family")==s["selected_setting"]["name"]]
    assert [r["seed"] for r in members]==c["seeds"]
    for key,value in s["seed_summary"].items():
        cohort="model_cold_available" if key.startswith("cold_") else "all_positive_events"
        numbers=[r["metrics"]["cohorts"][cohort][key.removeprefix("cold_")] for r in members]
        assert math.isclose(statistics.mean(numbers),value["mean"],abs_tol=1e-12)
        assert math.isclose(statistics.stdev(numbers),value["sample_std"],abs_tol=1e-12)
    result={"status":"passed","checked_at":datetime.now(timezone.utc).isoformat(),"protocol_id":s["protocol_id"],
            "series_sha256":sha(run/"series.json"),"audit_script_sha256":sha(__file__),
            "ranking_records_checked":checked,"ranking_candidate_checks":checked_candidates,
            "pool_records_checked":pool_records,"pool_candidate_checks":pool_candidates,
            "cached_feature_rows_checked":feature_rows,"training_examples_checked":len(seen_training),
            "checks":["independent strict-time queries and training-prefix signatures","user disjointness R01-R04",
                      "pre-2017 text fit and independent IDF","hash-sampled training requests and real candidate target positions",
                      "pool legality and cached context/content/cold/age/history features",
                      "all five cohort metrics and coverage","validation checkpoint selection and default method gate",
                      "checkpoint hashes/feature binding/recorded reload equality","three-seed mean and sample SD"],
            "slice_results":slice_results,
            "limits":["retrieval ranks are not re-inferred by this audit; source snapshots and target-blind unit tests cover construction",
                      "static metadata assumption; no statistical significance or online claim"]}
    output.write_text(json.dumps(result,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({key:result[key] for key in ("status","ranking_records_checked","pool_records_checked","training_examples_checked")}))


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args(); audit(args.run,args.output)
