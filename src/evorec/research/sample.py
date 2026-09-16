"""Full-stream user sampling with independent catalog-time provenance."""

import argparse
import csv
import gzip
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

from evorec.research.data import FIELDS, parse_row
from evorec.research.download import SOURCE
from evorec.research.runner import file_sha

SEED = "evorec-r02-v1"


def selected_user(user_id, excluded, denominator=20):
    if denominator < 1:
        raise ValueError("sampling denominator must be positive")
    return user_id not in excluded and int.from_bytes(
        hashlib.sha256((SEED + "|" + user_id).encode()).digest()[:8], "big"
    ) % denominator == 0


def download_full(path: Path, max_bytes=128 * 1024 * 1024):
    manifest_path = path.with_suffix(path.suffix + ".manifest.json")
    if path.exists():
        manifest = json.loads(manifest_path.read_text())
        if file_sha(path) != manifest["sha256"]:
            raise ValueError("cached full source checksum differs")
        return manifest
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    digest, count, last_report = hashlib.sha256(), 0, 0
    with urlopen(Request(SOURCE, headers={"User-Agent": "EvoRec-R02/0.1", "Accept-Encoding": "identity"}), timeout=30) as response:
        length = response.headers.get("Content-Length")
        if length is not None and int(length) > max_bytes:
            raise ValueError("remote archive exceeds configured download limit")
        with temporary.open("xb") as output:
            while chunk := response.read(1024 * 1024):
                count += len(chunk)
                if count > max_bytes:
                    raise ValueError("remote archive exceeds configured download limit")
                output.write(chunk)
                digest.update(chunk)
                if count - last_report >= 10 * 1024 * 1024:
                    print(json.dumps({"phase": "download", "bytes": count}), flush=True)
                    last_report = count
        if length is not None and count != int(length):
            raise ValueError("incomplete HTTP response")
        manifest = {
            "source_url": SOURCE, "bytes": count, "sha256": digest.hexdigest(),
            "downloaded_at": datetime.now(timezone.utc).isoformat(),
            "etag": response.headers.get("ETag"), "last_modified": response.headers.get("Last-Modified"),
        }
    temporary.replace(path)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def sample(source: Path, output: Path, development_sample: Path, denominator=20):
    manifest_path = output.with_suffix(".manifest.json")
    catalog_path = output.with_suffix(".catalog.json")
    if output.exists() or manifest_path.exists() or catalog_path.exists():
        raise FileExistsError("choose a new sample output; existing evidence is immutable")
    with development_sample.open(newline="", encoding="utf-8") as stream:
        excluded = {row["user_id"] for row in csv.DictReader(stream)}
    source_manifest = download_full(source)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".part")
    catalog, users = {}, set()
    total, kept = 0, 0
    started = time.perf_counter()
    with gzip.open(source, "rt", encoding="utf-8", newline="") as stream, temporary.open("x", encoding="utf-8", newline="") as target:
        reader = csv.DictReader(stream)
        if reader.fieldnames != list(FIELDS):
            raise ValueError("full source schema mismatch")
        writer = csv.DictWriter(target, fieldnames=FIELDS, lineterminator="\n")
        writer.writeheader()
        for row in reader:
            event = parse_row(row)
            total += 1
            catalog[event.item_id] = min(catalog.get(event.item_id, event.timestamp_ms), event.timestamp_ms)
            if selected_user(event.user_id, excluded, denominator):
                writer.writerow(row)
                users.add(event.user_id)
                kept += 1
            if total % 500000 == 0:
                print(json.dumps({"phase": "sample", "source_rows": total, "sample_rows": kept}), flush=True)
    # Reaching gzip EOF above verifies the archive checksum, unlike R01's prefix.
    temporary.replace(output)
    catalog_path.write_text(json.dumps(catalog, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    manifest = {
        "schema_version": "0.2", "status": "completed_user_hash_sample",
        "source": source_manifest, "full_gzip_crc_verified": True,
        "source_rows": total, "rows": kept, "users": len(users), "full_catalog_items": len(catalog),
        "selection": {"seed": SEED, "denominator": denominator, "rule": "sha256(seed|user) first 8 bytes modulo denominator == 0; exclude development users"},
        "development_users_excluded": len(excluded),
        "development_user_set_sha256": hashlib.sha256("\n".join(sorted(excluded)).encode()).hexdigest(),
        "development_sample_sha256": file_sha(development_sample),
        "sample_sha256": file_sha(output), "catalog_sha256": file_sha(catalog_path),
        "catalog_path": catalog_path.as_posix(),
        "availability": "first interaction in the full category strictly before request; proxy, not listing time",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "sample_scan_seconds": time.perf_counter() - started,
        "limitations": ["rating-only upstream deduplication policy not independently reproduced", "review rating is an implicit-interest proxy", "item availability is inferred from interactions, not real listings"],
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("datasets/amazon2023/Video_Games.csv.gz"))
    parser.add_argument("--output", type=Path, default=Path("datasets/video_games_r02.csv"))
    parser.add_argument("--development-sample", type=Path, default=Path("datasets/video_games_r01.csv"))
    parser.add_argument("--denominator", type=int, default=20)
    args = parser.parse_args()
    print(json.dumps(sample(args.source, args.output, args.development_sample, args.denominator), indent=2), flush=True)


if __name__ == "__main__":
    main()
