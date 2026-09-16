"""Train R05 residual listwise rankers and open test only after all selections."""
import argparse
import json
import shutil
import statistics
import time
from datetime import datetime,timezone
from pathlib import Path

import numpy as np
import torch

from evorec.research.content import ContentFeatures,ContentPredictor,reciprocal_fusion
from evorec.research.neural import configure_seed
from evorec.research.protocol import summarize
from evorec.research.ranker import (ResidualListRanker,listwise_loss,predict,load_ranker,SCALAR_NAMES,
                                   baseline_rankings,target_positions)
from evorec.research.ranker_data import (read,load_protocols,fit_early_encoder,prepare_training,
                                        prepare_pool,write_pool_trace,pool_diagnostics)
from evorec.research.ranker_report import report
from evorec.research.runner import file_sha,git_snapshot,peak_memory_bytes
from evorec.research.train import write_trace
from evorec.research.train_content import load_tower
from evorec.research.training_baselines import RecentPopular


def now():
    return datetime.now(timezone.utc).isoformat()


def save(series,output):
    temporary=output/"series.json.tmp"
    temporary.write_text(json.dumps(series,indent=2)+"\n",encoding="utf-8")
    temporary.replace(output/"series.json")
    report(series)


def fit(protocol,features,train,targets,cold,validation,pool,setting,seed,series,output):
    c=series["configuration"]
    configure_seed(seed)
    torch.cuda.reset_peak_memory_stats()
    model=ResidualListRanker(features.vectors.shape[1],**c["model"]).to("cuda")
    optimizer=torch.optim.AdamW(model.parameters(),lr=c["learning_rate"],weight_decay=c["weight_decay"])
    vectors=torch.from_numpy(np.vstack((np.zeros((1,features.vectors.shape[1]),dtype=np.float32),features.vectors))).to("cuda")
    rng=np.random.default_rng(seed)
    trial={"name":setting["name"]+f"-s{seed}","setting":setting,"seed":seed,"status":"training","history":[]}
    series["trials"].append(trial)
    directory=output/trial["name"]; directory.mkdir()
    checkpoint=directory/"best.pt"
    best,stale=-1.,0
    started=time.perf_counter()
    for epoch in range(1,c["max_epochs"]+1):
        tick=time.perf_counter()
        model.train()
        numerator=denominator=0.
        order=rng.permutation(len(targets))
        for start in range(0,len(order),c["batch_size"]):
            indices=order[start:start+c["batch_size"]]
            ids=torch.from_numpy(train.items[indices].astype(np.int64)).to("cuda")
            context=torch.from_numpy(train.contexts[indices]).to("cuda")
            scalars=torch.from_numpy(train.scalars[indices]).to("cuda")
            labels=torch.from_numpy(targets[indices]).to("cuda")
            cold_batch=torch.from_numpy(cold[indices]).to("cuda")
            optimizer.zero_grad(set_to_none=True)
            logits=model(context,vectors[ids],scalars,ids>0)
            loss=listwise_loss(logits,labels,cold_batch,setting["cold_weight"])
            if not torch.isfinite(loss): raise ValueError("non-finite loss")
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(),1.,error_if_nonfinite=True)
            optimizer.step()
            weight=float(np.where(cold[indices],setting["cold_weight"],1).sum())
            numerator+=loss.item()*weight; denominator+=weight
        metrics=summarize(protocol,validation,predict(model,pool,features,batch_size=c["evaluation_batch_size"]))
        ndcg=metrics["cohorts"]["all_positive_events"]["ndcg@10"]
        row={"epoch":epoch,"train_loss":numerator/denominator,"validation_ndcg@10":ndcg,
             "validation_cold_recall@20":metrics["cohorts"]["model_cold_available"]["recall@20"],
             "wall_seconds":time.perf_counter()-tick}
        trial["history"].append(row)
        if ndcg>best:
            best,stale=ndcg,0
            trial.update({"best_epoch":epoch,"best_validation":metrics})
            payload={"protocol_id":protocol.protocol_id,"feature_fingerprint":features.fingerprint,
                     "scalar_names":list(SCALAR_NAMES),"model_config":c["model"],
                     "setting":setting,"seed":seed,"epoch":epoch,
                     "training_cache_sha256":series["training_cache"]["sha256"],
                     "state_dict":{k:v.detach().cpu() for k,v in model.state_dict().items()}}
            tmp=checkpoint.with_suffix(".tmp"); torch.save(payload,tmp); tmp.replace(checkpoint)
        else: stale+=1
        save(series,output)
        print(json.dumps({"phase":"epoch","trial":trial["name"],**row,"best_epoch":trial["best_epoch"]}),flush=True)
        if stale>=c["patience"]: break
    trial.update({"status":"completed","wall_seconds":time.perf_counter()-started,
                  "checkpoint":{"path_from_project_root":checkpoint.as_posix(),"sha256":file_sha(checkpoint)},
                  "gpu_peak_allocated_bytes":torch.cuda.max_memory_allocated()})
    del model,optimizer,vectors
    torch.cuda.empty_cache()
    save(series,output)
    return trial


def run(config_path,output):
    config=read(config_path)
    if config["stage"]!="R05-ranker": raise ValueError("unexpected stage")
    configure_seed(config["seeds"][0])
    if not torch.cuda.is_available(): raise RuntimeError("registered run requires CUDA")
    parent,protocol,overlaps=load_protocols(config)
    output.mkdir(parents=True,exist_ok=False); (output/"source").mkdir()
    for path in Path(__file__).parent.glob("*.py"): shutil.copyfile(path,output/"source"/path.name)
    shutil.copyfile(config_path,output/"configuration.json")
    series={"status":"preparing","started_at":now(),"configuration":config,
            "protocol_id":protocol.protocol_id,"protocol":protocol.fingerprint,"user_overlaps":overlaps,
            "data_provenance":protocol.manifest,"model_training_provenance":parent.manifest,
            "metadata_provenance":protocol.metadata_manifest,
            "code":{**git_snapshot(),"source_sha256":{p.name:file_sha(p) for p in (output/"source").glob("*.py")}},
            "device":torch.cuda.get_device_name(),"torch":torch.__version__,"trials":[],
            "validation_results":[],"test_results":[]}
    save(series,output)
    try:
        features,encoder=fit_early_encoder(parent,config,output)
        series["content_encoder"]=encoder
        series["feature_fingerprint"]=features.fingerprint
        save(series,output)
        print(json.dumps({"phase":"encoder_ready","fit_documents":encoder["fit_document_count"],
                          "represented_items":encoder["represented_items"]}),flush=True)
        train,targets,cold,folds,cache=prepare_training(parent,features,config,output)
        series.update({"training_folds":folds,"training_cache":cache,
                       "training_examples":len(targets),"cold_training_examples":int(cold.sum())})
        save(series,output)
        validation=protocol.queries("validation")
        vpool,vcf,vraw=prepare_pool(protocol,features,validation,config)
        series["validation_pool_diagnostics"]=pool_diagnostics(vpool,features,validation)
        series["validation_pool_trace"]=write_pool_trace(output/"validation-pool.jsonl.gz",vpool,features,validation)
        vpool.save(output/"validation-inputs.npz",targets=target_positions(vpool,features,validation))
        series["validation_cache"]={"path_from_project_root":(output/"validation-inputs.npz").as_posix(),
                                     "sha256":file_sha(output/"validation-inputs.npz")}
        for name,rankings in (("RRF",baseline_rankings(vpool,features)),("CF-blend",vcf),("Content-SVD",vraw)):
            series["validation_results"].append({"name":name,"metrics":summarize(protocol,validation,rankings),
                                                  "trace":write_trace(output/f"validation-{name}.jsonl.gz",validation,rankings)})
        del vcf,vraw
        series["status"]="training"; save(series,output)
        choices=[fit(protocol,features,train,targets,cold,validation,vpool,setting,config["seeds"][0],series,output)
                 for setting in config["trials"]]
        best=max(choices,key=lambda t:t["best_validation"]["cohorts"]["all_positive_events"]["ndcg@10"])
        series["selected_setting"]=best["setting"]; series["setting_selected_at"]=now()
        for seed in config["seeds"][1:]:
            fit(protocol,features,train,targets,cold,validation,vpool,best["setting"],seed,series,output)
        del train,targets,cold
        # Reload every trained model before opening test and verify complete validation metrics.
        for trial in series["trials"]:
            cp=Path(trial["checkpoint"]["path_from_project_root"])
            if file_sha(cp)!=trial["checkpoint"]["sha256"]: raise ValueError("checkpoint integrity failure")
            model=load_ranker(cp,protocol.protocol_id,features,config["model"])
            rankings=predict(model,vpool,features,batch_size=config["evaluation_batch_size"])
            if summarize(protocol,validation,rankings)!=trial["best_validation"]:
                raise ValueError("checkpoint reload changes validation metrics")
            trial["checkpoint_reload_verified"]=True
            trial["validation_trace"]=write_trace(output/f"validation-{trial['name']}.jsonl.gz",validation,rankings)
            del model
        baseline=series["validation_results"][0]
        floor=baseline["metrics"]["cohorts"]["all_positive_events"]["ndcg@10"]*config["selection"]["ndcg_min_ratio"]
        options=[baseline]+[{"name":t["name"],"metrics":t["best_validation"]} for t in choices]
        eligible=[r for r in options if r["metrics"]["cohorts"]["all_positive_events"]["ndcg@10"]>=floor]
        chosen=max(eligible,key=lambda r:(r["metrics"]["cohorts"]["model_cold_available"]["recall@20"],
                                           r["metrics"]["cohorts"]["all_positive_events"]["ndcg@10"],
                                           r["name"]=="RRF"))
        series.update({"selected_method":chosen["name"],"validation_ndcg_floor":floor,
                       "selection_finished_at":now()})
        save(series,output)
        del validation,vpool
        series.update({"status":"final_test","test_opened_at":now()}); save(series,output)
        test=protocol.queries("test",test_authorized=True)
        pool,cf,raw=prepare_pool(protocol,features,test,config)
        series["test_queries"]=len(test)
        series["test_pool_diagnostics"]=pool_diagnostics(pool,features,test)
        series["test_pool_trace"]=write_pool_trace(output/"test-pool.jsonl.gz",pool,features,test)
        pool.save(output/"test-inputs.npz",targets=target_positions(pool,features,test))
        series["test_cache"]={"path_from_project_root":(output/"test-inputs.npz").as_posix(),
                               "sha256":file_sha(output/"test-inputs.npz")}
        def record(name,rankings,**extra):
            metrics=summarize(protocol,test,rankings)
            series["test_results"].append({"name":name,**extra,"metrics":metrics,
                                          "trace":write_trace(output/f"test-{name}.jsonl.gz",test,rankings)})
            save(series,output)
            print(json.dumps({"phase":"test","method":name,"ndcg":metrics["cohorts"]["all_positive_events"]["ndcg@10"],
                              "cold_recall":metrics["cohorts"]["model_cold_available"]["recall@20"]}),flush=True)
        for name,rankings in (("RRF",baseline_rankings(pool,features)),("CF-blend",cf),("Content-SVD",raw)):
            record(name,rankings,family="baseline")
        for trial in series["trials"]:
            model=load_ranker(trial["checkpoint"]["path_from_project_root"],protocol.protocol_id,features,config["model"])
            record(trial["name"],predict(model,pool,features,batch_size=config["evaluation_batch_size"]),
                   family=trial["setting"]["name"],seed=trial["seed"])
            del model
        # Separate pipeline reference: a frozen R03 model, never used to tune R05.
        frozen=read(Path(config["training_run"])/"series.json")
        source=Path(config["training_run"])/"content-encoder"
        for name,expected in frozen["content_encoder"]["files"].items():
            if file_sha(source/name)!=expected: raise ValueError("R03 reference encoder drift")
        old_features=ContentFeatures(read(source/"items.json"),np.load(source/"vectors.npy"))
        trial=next(t for t in frozen["trials"] if t["seed"]==17 and t["setting"]==frozen["selected_setting"])
        if file_sha(Path(trial["checkpoint"]["path_from_project_root"]))!=trial["checkpoint"]["sha256"]:
            raise ValueError("R03 reference checkpoint drift")
        model=load_tower(Path(trial["checkpoint"]["path_from_project_root"]),parent,old_features,"cuda")
        prior=RecentPopular(365).fit(parent.train,config["train_end_ms"],config["positive_rating_min"])
        old_rankings=ContentPredictor(old_features,protocol.catalog,prior,model,"cuda",
                                     config["evaluation_batch_size"],config["content"]["history_decay"]).rank_many(test)
        record("R03-Tower-RRF-s17",reciprocal_fusion(cf,old_rankings,.5,60),family="separate-pipeline-reference",
               checkpoint=trial["checkpoint"])
        family=series["selected_setting"]["name"]
        members=[r for r in series["test_results"] if r.get("family")==family]
        series["seed_summary"]={}
        for key in ("ndcg@10","recall@20","candidate_recall@200","cold_recall@20","cold_candidate_recall@200"):
            cohort="model_cold_available" if key.startswith("cold_") else "all_positive_events"
            values=[r["metrics"]["cohorts"][cohort][key.removeprefix("cold_")] for r in members]
            series["seed_summary"][key]={"mean":statistics.mean(values),"sample_std":statistics.stdev(values),
                                          "seeds":[r["seed"] for r in members]}
        series.update({"status":"completed","finished_at":now(),"peak_working_set_bytes":peak_memory_bytes()})
        save(series,output)
        archive=Path("docs/experiments/archive")/(output.name+".json")
        if archive.exists(): raise FileExistsError("archive already exists")
        archive.write_bytes((output/"series.json").read_bytes())
    except Exception as error:
        series.update({"status":"failed","error":f"{type(error).__name__}: {error}"})
        save(series,output)
        raise
    return series


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config",type=Path,default=Path("research/configs/r05-ranker.json"))
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    s=run(args.config,args.output)
    print(json.dumps({"status":s["status"],"selected_method":s["selected_method"],"seed_summary":s["seed_summary"]}),flush=True)
