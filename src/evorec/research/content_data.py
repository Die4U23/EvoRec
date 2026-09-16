"""Bounded metadata ingestion and an independently selected R03 user bucket."""
import argparse
import csv
import gzip
import hashlib
import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

from evorec.research.data import FIELDS, parse_row
from evorec.research.runner import file_sha
from evorec.research.sample import SEED

META_SOURCE = "https://mcauleylab.ucsd.edu/public_datasets/data/amazon_2023/raw/meta_categories/meta_Video_Games.jsonl.gz"


def selected_bucket(user, excluded, denominator=20, bucket=1):
    if denominator < 1 or not 0 <= bucket < denominator:
        raise ValueError("invalid hash bucket")
    return user not in excluded and int.from_bytes(
        hashlib.sha256((SEED + "|" + user).encode()).digest()[:8], "big"
    ) % denominator == bucket


def metadata_text(row):
    # This allowlist intentionally excludes rating aggregates and review text.
    def strings(value):
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            return [s for part in value for s in strings(part)]
        return []
    title = row.get("title", "")
    if not isinstance(title, str):
        title = ""
    text = " ".join([title, *strings(row.get("categories", []))])
    text = html.unescape(re.sub(r"<[^>]*>", " ", text))
    return re.sub(r"\s+", " ", text).strip()[:4096]


def download_metadata(path, max_bytes=128 * 1024 * 1024):
    manifest_path = path.with_suffix(path.suffix + ".manifest.json")
    if path.exists():
        manifest = json.loads(manifest_path.read_text())
        if manifest["source_url"] != META_SOURCE or file_sha(path) != manifest["sha256"]:
            raise ValueError("cached metadata integrity failure")
        return manifest
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    resumed_bytes = temporary.stat().st_size if temporary.exists() else 0
    last_error = None
    # A resumed gzip is still read to EOF by prepare_metadata; CRC and full SHA
    # establish the bytes actually used. The upstream does not publish a SHA.
    for attempt in range(1, 4):
        count = temporary.stat().st_size if temporary.exists() else 0
        headers = {"User-Agent": "EvoRec-R03/0.1", "Accept-Encoding": "identity"}
        if count:
            headers["Range"] = f"bytes={count}-"
        try:
            with urlopen(Request(META_SOURCE, headers=headers), timeout=45) as response:
                if count:
                    content_range = response.headers.get("Content-Range", "")
                    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", content_range)
                    if response.status != 206 or not match or int(match[1]) != count:
                        raise ValueError("server did not honor exact resume offset")
                    expected = int(match[3])
                else:
                    length = response.headers.get("Content-Length")
                    if length is None:
                        raise ValueError("metadata length is required")
                    expected = int(length)
                if expected > max_bytes or count > expected:
                    raise ValueError("metadata exceeds download budget")
                last_modified = response.headers.get("Last-Modified")
                etag = response.headers.get("ETag")
                with temporary.open("ab" if count else "xb") as stream:
                    while chunk := response.read(1024 * 1024):
                        count += len(chunk)
                        if count > expected or count > max_bytes:
                            raise ValueError("metadata response exceeds declared size")
                        stream.write(chunk)
                        if count // (10*1024*1024) != (count-len(chunk)) // (10*1024*1024):
                            print(json.dumps({"phase": "metadata_download", "bytes": count}), flush=True)
                if count != expected:
                    raise OSError("incomplete metadata response")
            # Validate EOF before promoting the downloaded file to a complete artifact.
            with gzip.open(temporary, "rb") as check:
                total_uncompressed = 0
                while block := check.read(1024*1024):
                    total_uncompressed += len(block)
                    if total_uncompressed > 2*1024**3:
                        raise ValueError("decompressed metadata exceeds budget")
            manifest = {"source_url": META_SOURCE, "bytes": count, "sha256": file_sha(temporary),
                        "last_modified": last_modified, "etag": etag, "resumed_bytes": resumed_bytes,
                        "full_gzip_crc_verified": True, "downloaded_at": datetime.now(timezone.utc).isoformat()}
            temporary.replace(path)
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
            return manifest
        except (OSError, TimeoutError) as error:
            last_error = error
            print(json.dumps({"phase": "metadata_download_retry", "attempt": attempt, "error": str(error)}), flush=True)
    raise RuntimeError("metadata download failed after three bounded attempts") from last_error

def prepare(config):
    output = Path(config["dataset_path"])
    manifest_path = output.with_suffix(".manifest.json")
    if output.exists() or manifest_path.exists():
        raise FileExistsError("choose a new R03 output; sample evidence is immutable")
    with Path("datasets/video_games_r01.csv").open(newline="", encoding="utf-8") as stream:
        excluded = {r["user_id"] for r in csv.DictReader(stream)}
    prior = json.loads(Path("datasets/video_games_r02.manifest.json").read_text())
    source = Path("datasets/amazon2023/Video_Games.csv.gz")
    if file_sha(source) != prior["source"]["sha256"]:
        raise ValueError("source integrity failure")
    temporary = output.with_suffix(".csv.part")
    users, kept, total = set(), 0, 0
    with gzip.open(source, "rt", encoding="utf-8", newline="") as stream, temporary.open("x", encoding="utf-8", newline="") as target:
        reader = csv.DictReader(stream)
        if reader.fieldnames != list(FIELDS):
            raise ValueError("source field mismatch")
        writer = csv.DictWriter(target, fieldnames=FIELDS, lineterminator="\n")
        writer.writeheader()
        for row in reader:
            event = parse_row(row)
            total += 1
            if selected_bucket(event.user_id, excluded, config["sample"]["denominator"], config["sample"]["bucket"]):
                writer.writerow(row)
                users.add(event.user_id)
                kept += 1
    temporary.replace(output)
    with Path("datasets/video_games_r02.csv").open(newline="", encoding="utf-8") as stream:
        previous_users = {r["user_id"] for r in csv.DictReader(stream)}
    if users.intersection(previous_users | excluded):
        raise ValueError("development user overlap")
    manifest = {
        "status": "completed_user_hash_sample", "rows": kept, "users": len(users), "source_rows": total,
        "source": prior["source"], "sample_sha256": file_sha(output),
        "catalog_path": prior["catalog_path"], "catalog_sha256": prior["catalog_sha256"],
        "selection": config["sample"], "full_gzip_crc_verified": True,
        "r01_users_excluded": len(excluded), "r02_user_overlap": 0,
        "r01_sample_sha256": file_sha(Path("datasets/video_games_r01.csv")),
        "r02_sample_sha256": file_sha(Path("datasets/video_games_r02.csv")),
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({"phase": "sample_completed", "rows": kept, "users": len(users)}), flush=True)
    return manifest


def prepare_metadata(config, sample_manifest):
    path = Path(config["metadata_path"])
    manifest_path = path.with_suffix(".manifest.json")
    if path.exists() or manifest_path.exists():
        raise FileExistsError("metadata evidence already exists")
    archive = Path("datasets/amazon2023/meta_Video_Games.jsonl.gz")
    source_manifest = download_metadata(archive)
    catalog = json.loads(Path(sample_manifest["catalog_path"]).read_text())
    texts, total, duplicate, decompressed = {}, 0, 0, 0
    with gzip.open(archive, "rt", encoding="utf-8") as stream:
        for line in stream:
            decompressed += len(line.encode("utf-8"))
            if decompressed > 2 * 1024**3:
                raise ValueError("decompressed metadata exceeds budget")
            row = json.loads(line)
            total += 1
            item = row.get("parent_asin")
            if item not in catalog:
                continue
            if item in texts:
                duplicate += 1
                continue
            texts[item] = metadata_text(row)
    path.write_text(json.dumps(texts, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    manifest = {
        "source": source_manifest, "full_gzip_crc_verified": True, "source_rows": total,
        "catalog_items": len(catalog), "metadata_items": len(texts),
        "nonempty_text_items": sum(bool(t) for t in texts.values()),
        "missing_metadata_items": len(catalog) - len(texts), "duplicate_items_first_record_kept": duplicate,
        "fields": config["content"]["fields"], "access_assumption": config["content"]["access_assumption"],
        "metadata_sha256": file_sha(path), "catalog_sha256": sample_manifest["catalog_sha256"],
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({"phase": "metadata_completed", **manifest}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("research/configs/r03-content.json"))
    parser.add_argument("--metadata-only", action="store_true")
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    manifest = (json.loads(Path(config["dataset_path"]).with_suffix(".manifest.json").read_text())
                if args.metadata_only else prepare(config))
    prepare_metadata(config, manifest)


if __name__ == "__main__":
    main()
