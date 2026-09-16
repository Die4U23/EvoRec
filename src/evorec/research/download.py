"""Download a bounded, explicitly biased prefix; never treat it as the full dataset."""

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

SOURCE = "https://mcauleylab.ucsd.edu/public_datasets/data/amazon_2023/benchmark/0core/rating_only/Video_Games.csv.gz"


class LimitedReader:
    def __init__(self, stream, limit: int):
        self.stream, self.limit, self.consumed = stream, limit, 0

    def read(self, size=-1):
        remaining = self.limit - self.consumed
        if remaining <= 0:
            raise ValueError("compressed download byte limit reached before row limit")
        chunk = self.stream.read(min(size if size >= 0 else remaining, remaining))
        self.consumed += len(chunk)
        return chunk


def collect_prefix(stream, output, limit: int) -> int:
    if type(limit) is not int or limit <= 0:
        raise ValueError("row limit must be positive")
    header = stream.readline(65537)
    if len(header) > 65536:
        raise ValueError("CSV line too long")
    if next(csv.reader([header.decode("utf-8-sig").strip()])) != list(FIELDS):
        raise ValueError("unexpected remote CSV header")
    writer = csv.DictWriter(output, fieldnames=FIELDS, lineterminator="\n")
    writer.writeheader()
    count = 0
    while count < limit:
        raw = stream.readline(65537)
        if not raw:
            break
        if len(raw) > 65536:
            raise ValueError("CSV line too long")
        values = next(csv.reader([raw.decode("utf-8").strip()]))
        if len(values) != len(FIELDS):
            raise ValueError("unexpected remote row shape")
        row = dict(zip(FIELDS, values))
        parse_row(row)
        writer.writerow(row)
        count += 1
    if count != limit:
        raise ValueError(f"source ended after {count} rows, expected {limit}")
    return count


def download(output: Path, limit=50000, max_bytes=16 * 1024 * 1024) -> dict:
    if output.exists() or output.with_suffix(".manifest.json").exists():
        raise FileExistsError("sample already exists; use its manifest or choose a new output")
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_suffix(output.suffix + ".part")
    started = time.perf_counter()
    request = Request(SOURCE, headers={"User-Agent": "EvoRec-R01/0.1", "Accept-Encoding": "identity"})
    with urlopen(request, timeout=30) as response:
        reader = LimitedReader(response, max_bytes)
        with gzip.GzipFile(fileobj=reader) as decompressed, partial.open("x", encoding="utf-8", newline="") as target:
            rows = collect_prefix(decompressed, target, limit)
        headers = {key: response.headers.get(key) for key in ("ETag", "Last-Modified", "Content-Length")}
        resolved_url = response.geturl()
    partial.replace(output)
    with output.open("rb") as sample:
        checksum = hashlib.file_digest(sample, "sha256").hexdigest()
    manifest = {
        "status": "completed_prefix_sample",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_url": SOURCE, "resolved_url": resolved_url,
        "source_documentation": "https://amazon-reviews-2023.github.io/data_processing/0core.html",
        "source_headers": headers,
        "sampling": "first_N_rows_in_source_order_not_random_not_representative",
        "requested_rows": limit, "rows": rows,
        "compressed_bytes_read": reader.consumed,
        "max_compressed_bytes": max_bytes,
        "elapsed_seconds": time.perf_counter() - started,
        "sample_sha256": checksum,
        "full_source_downloaded": False,
        "full_gzip_crc_verified": False,
        "limitations": [
            "prefix order may bias users, items and times",
            "sample can truncate a user's history",
            "first observation in this sample is not real item listing time",
            "partial gzip reading cannot verify the full archive CRC",
            "source terms and redistribution permissions require review before redistributing data",
        ],
    }
    output.with_suffix(".manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("datasets/video_games_r01.csv"))
    parser.add_argument("--limit", type=int, default=50000)
    args = parser.parse_args()
    print(json.dumps(download(args.output, args.limit), indent=2))


if __name__ == "__main__":
    main()
