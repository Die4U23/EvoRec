"""Verify the completed R04 report, blog image sources and local documentation."""
import ast
import hashlib
import json
import re
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

from PIL import Image


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack, self.links, self.images = [], [], []
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "img":
            assert attrs.get("alt"), "missing alternative text"
            self.images.append(attrs["src"])
        if tag == "a":
            self.links.append(attrs["href"])
        if tag not in {"meta","img","br","hr","link","input","source","wbr"}:
            self.stack.append(tag)
    def handle_endtag(self, tag):
        assert self.stack and self.stack.pop() == tag, ("HTML nesting",tag)


def check():
    paths = subprocess.check_output(["git","ls-files","--cached","--others","--exclude-standard","-z"]).decode("utf-8").split("\0")
    documents, link_count, python_files = 0, 0, 0
    for path in dict.fromkeys(paths):
        p=Path(path)
        if not path or not p.is_file():
            continue
        if p.suffix == ".py":
            ast.parse(p.read_text(encoding="utf-8"),filename=str(p))
            python_files += 1
        if p.suffix != ".md":
            continue
        text=p.read_text(encoding="utf-8")
        assert "\ufffd" not in text, p
        assert len(re.findall(r"^\s*"+chr(96)*3,text,re.M))%2 == 0,p
        documents += 1
        for target in re.findall(r"\[[^\]\n]+\]\(([^)\n]+)\)",text):
            target=target.strip("<>")
            if urlsplit(target).scheme or target.startswith("#"):
                continue
            target=unquote(target.split("#",1)[0])
            if target:
                assert (p.parent/target).exists(),(str(p),target)
                link_count += 1
    run=Path("artifacts/runs/r04-gating-20260915")
    report=Path("docs/experiments/r04-gating")
    series=json.loads((run/"series.json").read_text(encoding="utf-8"))
    published=json.loads((report/"results.json").read_text(encoding="utf-8"))
    assert series["status"]=="completed" and series["selected_policy"]["name"]=="fixed"
    assert {k:published[k] for k in series}==series
    assert published["report_renderer_sha256"]==sha("src/evorec/research/gating_report.py")
    assert (Path("docs/experiments/archive")/(run.name+".json")).read_bytes()==(run/"series.json").read_bytes()
    audit=json.loads(Path("docs/validation/gating-checks.json").read_text(encoding="utf-8"))
    assert audit["status"]=="passed" and audit["series_sha256"]==sha(run/"series.json")
    assert audit["audit_script_sha256"]==sha("scripts/audit_gating.py")
    diagnosis=json.loads((report/"error-analysis.json").read_text(encoding="utf-8"))
    assert diagnosis["series_sha256"]==sha(run/"series.json")
    assert diagnosis["script_sha256"]==sha("scripts/analyze_gating_errors.py")
    assert sum(diagnosis["mutually_exclusive_raw_rank_groups"].values())==diagnosis["counts"]["cold_targets"]
    for trace in diagnosis["source_traces"].values():
        assert sha(trace["path_from_project_root"])==trace["sha256"]
    assets=Path("docs/blog/assets/evorec")
    manifest=json.loads((assets/"manifest.json").read_text(encoding="utf-8"))
    assert manifest["generator_sha256"]==sha(manifest["generator"])
    assert len(manifest["assets"])==20
    for asset in manifest["assets"]:
        file=assets/asset["file"]
        assert sha(file)==asset["sha256"]
        assert asset["caption"] and asset["alt"]
        if file.suffix==".png":
            with Image.open(file) as im:
                im.verify()
            with Image.open(file) as im:
                assert (im.width,im.height)==(asset["width"],asset["height"])
                assert im.width>=800 and im.height>=300
        else:
            root=ET.parse(file).getroot()
            assert root.tag.endswith("svg")
        if asset["kind"]=="measured":
            assert sha(asset["source"])==asset["source_sha256"]
            assert asset["sha256"]==asset["source_image_sha256"]
    pages=[]
    for page,count in ((report/"report.html",3),(assets/"index.html",10)):
        parser=Page()
        parser.feed(page.read_text(encoding="utf-8"))
        assert not parser.stack and len(parser.images)==count
        for target in parser.images+parser.links:
            assert not urlsplit(target).scheme,"expected offline page"
            assert (page.parent/target).exists(),(page,target)
        pages.append(page.as_posix())
    blog=Path("docs/blog/evorec-project-log.md").read_text(encoding="utf-8")
    embedded=re.findall(r"!\[[^\]]+\]\((assets/evorec/[^)]+)\)",blog)
    assert len(embedded)==len(set(embedded)) and len(embedded)>=10
    assert all((Path("docs/blog")/path).exists() for path in embedded)
    checks={}
    for name,expected in (("gating-core-tests.xml",96),("gating-research-tests.xml",19)):
        root=ET.parse(Path("docs/validation")/name).getroot()
        cases=root.findall(".//testcase")
        assert not root.findall(".//failure") and not root.findall(".//error")
        passed=sum(c.find("skipped") is None for c in cases)
        assert passed==expected
        checks[name]={"passed":passed,"skipped":len(cases)-passed}
    subprocess.run(["git","diff","--check"],check=True)
    result={"status":"passed","checked_at":datetime.now(timezone.utc).isoformat(),
            "checker_sha256":sha(__file__),"markdown_documents":documents,"local_links_checked":link_count,
            "python_files_parsed":python_files,"blog_image_embeds":len(embedded),"image_files_checked":20,
            "offline_html_pages":pages,"completed_series_archive_and_report_match":True,
            "image_sources_and_generator_fingerprints":"passed","independent_audit_matches_series":True,
            "tests":checks,"git_diff_whitespace":"passed",
            "limits":["HTML structure/resources checked; no full browser rendering asserted",
                      "research and service tests overlap on eight gating cases"]}
    Path("docs/validation/gating-artifacts-checks.json").write_text(json.dumps(result,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(result))


if __name__=="__main__":
    check()
