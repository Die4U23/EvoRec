"""Inspect the Git index, not ignored local files, before a research delivery."""
import argparse
import ast
import hashlib
import json
import posixpath
import re
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.targets = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "a" and values.get("href"):
            self.targets.append(values["href"])
        if tag in {"img", "script"} and values.get("src"):
            self.targets.append(values["src"])
        if tag == "link" and values.get("href"):
            self.targets.append(values["href"])


def indexed_files():
    rows = subprocess.check_output(["git", "ls-files", "--stage", "-z"]).decode("utf-8").split("\0")
    entries = {}
    for row in filter(None, rows):
        meta, path = row.split("\t", 1)
        _, digest, stage = meta.split()
        if stage != "0":
            raise ValueError("unmerged index entry: " + path)
        entries[path] = digest
    objects = list(dict.fromkeys(entries.values()))
    raw = subprocess.run(["git", "cat-file", "--batch"], input=("\n".join(objects)+"\n").encode(),
                         check=True, capture_output=True).stdout
    blobs, position = {}, 0
    for expected in objects:
        end = raw.index(b"\n", position)
        digest, kind, size = raw[position:end].decode().split()
        if digest != expected or kind != "blob":
            raise ValueError("unexpected Git object")
        position = end+1
        blobs[digest] = raw[position:position+int(size)]
        position += int(size)+1
    return {path: blobs[digest] for path, digest in entries.items()}


def check(files):
    errors, link_count, python_count = [], 0, 0
    private_extensions = {".csv", ".pt", ".pth", ".npy", ".npz", ".joblib", ".jsonl", ".gz", ".zip"}
    secret_pattern = re.compile(rb"gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----")
    for name, content in files.items():
        lower = name.lower()
        if ("blog" in lower or "internship" in lower or "实习" in name or "求职" in name
                or name.startswith("docs/career/")):
            # The checker and policy may discuss local materials; their filenames remain generic.
            errors.append("local-only publishing/career path tracked: "+name)
        if name.startswith(("tmp/", ".venv/", ".venv-research/", "node_modules/")):
            errors.append("temporary/environment path tracked: "+name)
        if name.startswith(("datasets/", "artifacts/")) and name not in {"datasets/README.md", "artifacts/README.md"}:
            errors.append("raw research artifact tracked: "+name)
        if Path(name).suffix.lower() in private_extensions or (Path(name).name.startswith(".env") and name != ".env.example"):
            errors.append("private data or configuration extension: "+name)
        if len(content) > 10*1024*1024:
            errors.append("unexpected file above 10 MiB: "+name)
        if Path(name).suffix.lower() in {".py", ".md", ".json", ".html", ".toml", ".yml", ".yaml", ".txt", ".ps1"}:
            if secret_pattern.search(content):
                errors.append("possible credential; inspect privately: "+name)
        if name.endswith(".py"):
            try:
                ast.parse(content.decode("utf-8-sig"), filename=name)
                python_count += 1
            except (SyntaxError, UnicodeError):
                errors.append("invalid Python source: "+name)
        targets = []
        if name.endswith(".md"):
            targets = re.findall(r"!?\[[^\]\n]*\]\(([^)\n]+)\)", content.decode("utf-8-sig"))
        elif name.endswith(".html"):
            parser = Links()
            parser.feed(content.decode("utf-8-sig"))
            targets = parser.targets
        for target in targets:
            target = target.strip().strip("<>")
            if urlsplit(target).scheme or target.startswith(("#", "//")):
                continue
            target = unquote(target.split("#",1)[0].split("?",1)[0])
            if not target:
                continue
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), target))
            if resolved not in files and not any(path.startswith(resolved.rstrip("/")+"/") for path in files):
                errors.append("link missing from Git index: "+name+" -> "+target)
            link_count += 1
    if errors:
        raise ValueError("\n".join(errors))
    return {"status": "passed", "scope": "Git index only", "tracked_files": len(files),
            "local_links_checked": link_count, "python_files_parsed": python_count,
            "largest_file_bytes": max(map(len, files.values()), default=0),
            "no_tracked_local_materials_or_raw_artifacts": True,
            "credential_scan": "common token and private-key signatures only; not an exhaustive secret audit"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("tmp/repository-hygiene.json"))
    args = parser.parse_args()
    result = check(indexed_files())
    result["checker_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    subprocess.run(["git", "diff", "--cached", "--check"], check=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2)+"\n", encoding="utf-8")
    print(json.dumps(result))
