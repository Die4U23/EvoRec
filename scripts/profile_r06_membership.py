"""Synthetic ordered-membership microbenchmark; no DB/model/API or SLA proof."""

import argparse
from collections import namedtuple
import json
from pathlib import Path
from statistics import median
from time import perf_counter
import tracemalloc

from evorec.infrastructure.r06_admission import _ordered_membership_matches
from scripts.assemble_r06_bundle import _source
from scripts.verify_r06_reliability import subprocess_sources


def reference(rows, expected):
    """The pre-change capture predicate, for already materialized trusted rows."""
    return (tuple(row.item_id for row in rows) == expected
            and not any(row.internal_item_id != index for index, row in enumerate(rows)))


def profile(*, items=137249, blocks=6):
    if (type(items) is not int or not 1 <= items <= 150000
            or type(blocks) is not int or not 1 <= blocks <= 16):
        raise ValueError("items 1..150000 and ABBA blocks 1..16 required")
    if tracemalloc.is_tracing():
        raise ValueError("existing tracemalloc observer is not permitted")
    project = Path(__file__).resolve().parents[1]
    commit, hashes = _source(project), subprocess_sources(project)
    # Shape matches the compact DB namedtuple, but every value is synthetic.
    row_type = namedtuple("MembershipRow", "item_id internal_item_id is_active digest timestamp")
    expected = tuple(f"synthetic-{index}" for index in range(items))
    rows = [row_type(item, index, True, b"x" * 32, 1) for index, item in enumerate(expected)]
    functions = {"reference": reference, "candidate": _ordered_membership_matches}
    for function in functions.values():
        if function(rows, expected) is not True:
            raise ValueError("positive membership control failed")
    records = []
    for block in range(blocks):
        for name in ("reference", "candidate", "candidate", "reference"):
            started = perf_counter()
            matches = functions[name](rows, expected)
            elapsed = perf_counter() - started
            if matches is not True:
                raise ValueError("timed membership control failed")
            records.append(dict(block=block, implementation=name, elapsed_seconds=elapsed))
    peaks = {}
    # Independent observer pass: never include tracemalloc overhead in timings.
    for name, function in functions.items():
        tracemalloc.start()
        try:
            if function(rows, expected) is not True:
                raise ValueError("allocation membership control failed")
            peaks[name] = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
    if _source(project) != commit or subprocess_sources(project) != hashes:
        raise ValueError("source changed during measurement")
    return dict(kind="synthetic_membership_predicate_not_database_or_api",
                source_commit=commit, source_sha256=hashes, items=items, abba_blocks=blocks,
                untimed_warmups_per_implementation=1, automatic_retries=0,
                gc_policy_changed=False, timings=records, traced_peak_bytes=peaks,
                median_seconds={name: median(record["elapsed_seconds"] for record in records
                                            if record["implementation"] == name)
                                for name in functions},
                limits="Valid full rows only; no SQL, decode, content checks, ranking, TCP, RSS or SLA")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--items", type=int, default=137249)
    parser.add_argument("--blocks", type=int, default=6)
    args = parser.parse_args(argv)
    output = args.output.resolve()
    artifacts = Path(__file__).resolve().parents[1] / "artifacts"
    if not output.is_relative_to(artifacts) or output == artifacts or output.exists():
        raise ValueError("output must be a new file inside project artifacts")
    report = profile(items=args.items, blocks=args.blocks)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2)
    print(json.dumps({key: report[key] for key in ("kind", "items", "median_seconds", "traced_peak_bytes")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
