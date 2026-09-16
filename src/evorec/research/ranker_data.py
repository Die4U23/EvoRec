"""Time-prefix statistics and target-independent candidate pools for R05."""
import copy
import csv
import gzip
import hashlib
import json
from pathlib import Path

import numpy as np

from evorec.research.baselines import ItemCF
from evorec.research.content import ContentProtocol, ContentFeatures, ContentPredictor
from evorec.research.protocol import AvailableAt
from evorec.research.ranker import PoolInputs, build_pool_inputs, target_positions
from evorec.research.runner import file_sha
from evorec.research.training_baselines import RecentPopular, CollaborativeBlend


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def prefix_protocol(parent, start, stop):
    p=copy.copy(parent)
    p.config={**parent.config,"train_end_ms":start,"validation_end_ms":stop}
    p.train=[event for event in parent.events if event.timestamp_ms < start]
    p.train_items={e.item_id for e in p.train}
    p.vocabulary=tuple(sorted({e.item_id for e in p.train if e.rating >= p.config["positive_rating_min"]}))
    return p


def training_signature(events):
    text="\n".join(f"{e.timestamp_ms}|{e.user_id}|{e.item_id}|{e.rating}" for e in events)
    return hashlib.sha256(text.encode()).hexdigest()


def load_protocols(config):
    frozen=read(Path(config["training_run"])/"series.json")
    if frozen["status"] != "completed":
        raise ValueError("training provenance incomplete")
    parent=ContentProtocol(frozen["configuration"])
    query=ContentProtocol(config)
    users={e.user_id for e in query.events}
    overlaps={}
    for stage in ("r01","r02","r03","r04"):
        with Path(f"datasets/video_games_{stage}.csv").open(encoding="utf-8",newline="") as stream:
            overlap=users & {row["user_id"] for row in csv.DictReader(stream)}
        overlaps[stage]=len(overlap)
        if overlap:
            raise ValueError(f"query users overlap {stage}")
    query.train,query.train_items,query.vocabulary=parent.train,parent.train_items,parent.vocabulary
    query.fingerprint={
        "protocol":"r05-rolling-cold-listwise-v1","configuration":config,
        "query_sample_sha256":query.manifest["sample_sha256"],
        "model_training_sample_sha256":parent.manifest["sample_sha256"],
        "catalog_sha256":query.manifest["catalog_sha256"],
        "metadata_sha256":query.metadata_manifest["metadata_sha256"],
    }
    query.protocol_id=hashlib.sha256(json.dumps(query.fingerprint,sort_keys=True).encode()).hexdigest()[:16]
    return parent,query,overlaps


def fit_early_encoder(parent, config, output):
    early=prefix_protocol(parent,config["encoder_fit_end_ms"],config["train_end_ms"])
    early.config={**early.config,"content":config["content"]}
    early.protocol_id=hashlib.sha256(json.dumps({
        "parent":parent.protocol_id,"cutoff":config["encoder_fit_end_ms"],"content":config["content"]
    },sort_keys=True).encode()).hexdigest()[:16]
    features,manifest=ContentFeatures.fit(early,output/"content-encoder")
    manifest.update({"fit_end_ms":config["encoder_fit_end_ms"],"fit_training_rows":len(early.train),
                     "fit_training_signature":training_signature(early.train)})
    return features,manifest


def prepare_pool(protocol,features,queries,config):
    prior=RecentPopular(365).fit(protocol.train,protocol.config["train_end_ms"],config["positive_rating_min"])
    core=ItemCF(max_user_items=100,neighbors=100).fit(protocol.train,config["positive_rating_min"])
    blend=CollaborativeBlend(core,prior,.25)
    collaborative=[blend.rank(q.history,q.seen,AvailableAt(protocol.catalog,q.timestamp_ms),config["retrieval_k"]) for q in queries]
    predictor=ContentPredictor(features,protocol.catalog,prior,device="cuda",
                               batch_size=config["evaluation_batch_size"],decay=config["content"]["history_decay"])
    raw=predictor.rank_many(queries,k=config["retrieval_k"])
    pool=build_pool_inputs(features,protocol.catalog,protocol.train_items,blend.priors,queries,collaborative,raw,
                           pool_k=config["pool_k"],decay=config["content"]["history_decay"],constant=config["rrf_constant"])
    return pool,collaborative,raw


def write_pool_trace(path,pool,features,queries,training=False):
    positions=target_positions(pool,features,queries)
    effective=np.linalg.norm(pool.contexts,axis=1)>1e-8
    with gzip.open(path,"wt",encoding="utf-8") as stream:
        for row,q in enumerate(queries):
            entry={"query_id":q.query_id,"timestamp_ms":q.timestamp_ms,"target":q.target,"history":q.history,
                   "target_available":q.target_available,"target_model_cold":q.target_model_cold,
                   "candidates":[features.items[int(i)-1] for i in pool.items[row] if i>0],
                   "target_position":int(positions[row]),"has_content_history":bool(effective[row])}
            if training:
                entry["eligible_training"]=bool(positions[row]>=0 and effective[row])
            stream.write(json.dumps(entry,separators=(",",":"))+"\n")
    return {"path_from_project_root":path.as_posix(),"sha256":file_sha(path)}


def pool_diagnostics(pool,features,queries):
    positions=target_positions(pool,features,queries)
    cold=np.array([q.target_available and q.target_model_cold for q in queries])
    return {
        "queries":len(queries),"target_in_pool":int((positions>=0).sum()),
        "pool_recall":float((positions>=0).mean()),
        "cold_available_targets":int(cold.sum()),
        "cold_targets_in_pool":int(((positions>=0)&cold).sum()),
        "cold_pool_recall":float((positions[cold]>=0).mean()) if cold.any() else None,
        "has_content_history":int((np.linalg.norm(pool.contexts,axis=1)>1e-8).sum()),
        "mean_pool_candidates":float((pool.items>0).sum(axis=1).mean()),
    }


def prepare_training(parent,features,config,output):
    pools,labels,cold_labels,identities,records=[],[],[],[],[]
    for fold in config["folds"]:
        p=prefix_protocol(parent,fold["start_ms"],fold["end_ms"])
        all_queries=[q for q in p.queries("validation") if q.history]
        sampled=sorted(all_queries,key=lambda q:hashlib.sha256(q.query_id.encode()).digest())[:config["max_training_queries_per_fold"]]
        queries=sorted(sampled,key=lambda q:(q.timestamp_ms,q.query_id))
        pool,_,_=prepare_pool(p,features,queries,config)
        positions=target_positions(pool,features,queries)
        eligible=(positions>=0)&(np.linalg.norm(pool.contexts,axis=1)>1e-8)
        used=np.flatnonzero(eligible)
        pools.append(pool.subset(used)); labels.append(positions[used])
        cold_labels.append(np.array([queries[i].target_model_cold for i in used],dtype=bool))
        identities.extend(queries[i].query_id for i in used)
        record={**fold,"statistics_rows":len(p.train),"statistics_signature":training_signature(p.train),
                "all_history_queries":len(all_queries),"sampled_queries":len(queries),
                "training_examples":len(used),"cold_training_examples":int(cold_labels[-1].sum()),
                "skipped_candidate_miss":int((positions<0).sum()),
                "skipped_unrepresented_history":int(((positions>=0)&~eligible).sum()),
                "diagnostics":pool_diagnostics(pool,features,queries),
                "trace":write_pool_trace(output/f"train-{fold['name']}.jsonl.gz",pool,features,queries,training=True)}
        records.append(record)
        (output/"training-folds.json").write_text(json.dumps(records,indent=2)+"\n",encoding="utf-8")
        print(json.dumps({"phase":"training_fold_ready",**{k:record[k] for k in ("name","sampled_queries","training_examples","cold_training_examples")}}),flush=True)
    combined=PoolInputs.join(pools)
    targets=np.concatenate(labels); cold=np.concatenate(cold_labels)
    if len(targets)<100 or int(cold.sum())<10:
        raise ValueError("insufficient real retrieved positives for registered cold training")
    path=output/"training-inputs.npz"
    combined.save(path,targets=targets,cold=cold,query_ids=np.array(identities))
    return combined,targets,cold,records,{"path_from_project_root":path.as_posix(),"sha256":file_sha(path)}
