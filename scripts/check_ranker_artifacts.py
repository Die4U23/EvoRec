"""Check R05 published artifacts against the completed, audited local run."""
import ast
import json
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime,timezone
from pathlib import Path

from PIL import Image
from check_gating_artifacts import Page,sha


def check():
    run=Path("artifacts/runs/r05-ranker-20260916")
    directory=Path("docs/experiments/r05-ranker")
    s=json.loads((run/"series.json").read_text(encoding="utf-8"))
    published=json.loads((directory/"results.json").read_text(encoding="utf-8"))
    assert s["status"]=="completed"
    assert {k:published[k] for k in s}==s
    assert published["report_renderer_sha256"]==sha("src/evorec/research/ranker_report.py")
    assert (Path("docs/experiments/archive")/(run.name+".json")).read_bytes()==(run/"series.json").read_bytes()
    audit=json.loads(Path("docs/validation/ranker-checks.json").read_text(encoding="utf-8"))
    assert audit["status"]=="passed" and audit["series_sha256"]==sha(run/"series.json")
    assert audit["audit_script_sha256"]==sha("scripts/audit_ranker.py")
    for name in ("ranker.py","ranker_data.py","train_ranker.py"):
        assert sha(Path("src/evorec/research")/name)==s["code"]["source_sha256"][name]
        ast.parse((Path("src/evorec/research")/name).read_text(encoding="utf-8"))
    assert sum(len(t["history"]) for t in s["trials"])==35
    assert all(t["checkpoint_reload_verified"] for t in s["trials"])
    import torch
    from evorec.research.ranker import ResidualListRanker,SCALAR_NAMES
    model=ResidualListRanker(s["configuration"]["content"]["dimensions"],**s["configuration"]["model"])
    model_card={
        "schema_version":1,"name":"EvoRec R05 ResidualListRanker","status":"trained_and_offline_audited",
        "protocol_id":s["protocol_id"],"selected_validation_method":s["selected_method"],
        "architecture":[520,128,64,1],"parameters":sum(p.numel() for p in model.parameters()),
        "scalar_features":list(SCALAR_NAMES),"feature_fingerprint":s["feature_fingerprint"],
        "training_examples":s["training_examples"],"simulated_cold_examples":s["cold_training_examples"],
        "checkpoints":[{"name":t["name"],"seed":t["seed"],"best_epoch":t["best_epoch"],**t["checkpoint"]} for t in s["trials"]],
        "evaluation_summary":s["seed_summary"],"summary_family":s["selected_setting"]["name"],
        "source_series_sha256":sha(run/"series.json"),
        "limits":["selected ColdListMLP has only seed 17; reported seed SD belongs to ListMLP",
                  "offline candidate ranking only; no public serving endpoint","static metadata access assumption",
                  "requires the exact frozen encoder and feature schema","candidate misses excluded from training loss but retained in evaluation denominator"],
    }
    for trial in s["trials"]:
        cp=trial["checkpoint"]
        assert sha(cp["path_from_project_root"])==cp["sha256"]
        payload=torch.load(cp["path_from_project_root"],weights_only=True,map_location="cpu")
        assert payload["scalar_names"]==list(SCALAR_NAMES)
        assert payload["model_config"]==s["configuration"]["model"]
        assert payload["training_cache_sha256"]==s["training_cache"]["sha256"]
    (directory/"model-card.json").write_text(json.dumps(model_card,indent=2)+"\n",encoding="utf-8")
    assets=Path("docs/blog/assets/evorec/r05")
    # Publishing assets are optional, local-only material.
    blog_image_count = 0
    if assets.exists():
        manifest=json.loads((assets/"manifest.json").read_text(encoding="utf-8"))
        assert len(manifest["assets"])==4
        assert manifest["generator_sha256"]==sha(manifest["generator"])
        for item in manifest["assets"]:
            path=assets/item["file"]
            assert sha(path)==item["sha256"]==sha(item["source_image"])
            assert sha(item["source_results"])==item["source_results_sha256"]
            assert item["protocol_id"]==s["protocol_id"]
            if path.suffix==".png":
                with Image.open(path) as im: im.verify()
                with Image.open(path) as im: assert im.size==(item["width"],item["height"])
            else: assert ET.parse(path).getroot().tag.endswith("svg")
        blog_image_count = len(manifest["assets"])
    html_pages=[]
    for page in [directory/"report.html"] + ([assets/"index.html"] if assets.exists() else []):
        parsed=Page(); parsed.feed(page.read_text(encoding="utf-8"))
        assert not parsed.stack and len(parsed.images)==2
        for target in parsed.images+parsed.links: assert (page.parent/target).exists(),target
        html_pages.append(page.as_posix())
    tests={}
    for name,count in (("ranker-core-tests.xml",96),("ranker-research-tests.xml",28)):
        root=ET.parse(Path("docs/validation")/name).getroot()
        assert not root.findall(".//error") and not root.findall(".//failure")
        cases=root.findall(".//testcase")
        passed=sum(case.find("skipped") is None for case in cases)
        assert passed==count
        tests[name]={"passed":passed,"skipped":len(cases)-passed}
    paths=[s["configuration"]["dataset_path"],s["trials"][1]["checkpoint"]["path_from_project_root"],".venv-research/pyvenv.cfg"]
    ignored=subprocess.check_output(["git","check-ignore",*paths],text=True).splitlines()
    assert len(ignored)==len(paths)
    subprocess.run(["git","diff","--check"],check=True)
    result={"status":"passed","checked_at":datetime.now(timezone.utc).isoformat(),"checker_sha256":sha(__file__),
            "series_sha256":sha(run/"series.json"),"model_parameters":model_card["parameters"],
            "model_checkpoints_checked":len(s["trials"]),"model_card":(directory/"model-card.json").as_posix(),
            "report_and_archive_match":True,"audit_matches":True,"blog_image_files_checked":blog_image_count,
            "html_structure_and_resources":html_pages,"tests":tests,"data_and_models_ignored":True,
            "visual_review":["learning curve axes/legends inspected","test comparison labels and values inspected"],
            "limits":["HTML structure/resources verified; full browser layout was not tested",
                      "eight gating tests overlap across service and research suites"]}
    Path("docs/validation/ranker-artifacts-checks.json").write_text(json.dumps(result,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(result))


if __name__=="__main__":
    check()
