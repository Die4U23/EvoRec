"""Owned full-catalog SQL observations, not an HTTP performance acceptance."""

import argparse
import ast
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
from time import perf_counter
from types import SimpleNamespace
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from evorec.domain.errors import ManagementError
from evorec.infrastructure.r06_catalog_capture import (
    CATALOG_CAPTURE_SQL, CatalogCapture, capture_eligible, catalog_source_digests,
)
from scripts.assemble_r06_bundle import _source
from scripts.r06_service_lab import R06ServiceLab
from scripts.r06_database_observer import DatabaseCallObserver
from scripts.run_r06_demo import marker
from scripts.verify_r06_reliability import subprocess_sources


LEGACY_SQL = """SELECT bi.item_id, bi.internal_item_id, i.is_active,
    i.r06_model_text, i.r06_first_seen_ms
    FROM bundle_items bi JOIN items i ON i.item_id=bi.item_id
    WHERE bi.bundle_id=%s ORDER BY bi.internal_item_id"""


def _sql_literal(source):
    """Read only the SQL literal, never import/execute historical Python code."""
    values = [node.value for node in ast.parse(source).body
              if isinstance(node, ast.Assign)
              and any(isinstance(target, ast.Name) and target.id == "CATALOG_CAPTURE_SQL"
                      for target in node.targets)]
    if (len(values) != 1 or not isinstance(values[0], ast.Constant)
            or type(values[0].value) is not str):
        raise ValueError("baseline must contain exactly one literal catalog SQL")
    return values[0].value


def _baseline_sql(project, commit):
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("baseline must be a full lowercase commit SHA")
    kind = subprocess.run(["git", "cat-file", "-t", commit], cwd=project, check=True,
                          capture_output=True).stdout
    if kind.strip() != b"commit":
        raise ValueError("baseline identifier must name a commit object")
    source = subprocess.run(
        ["git", "show", f"{commit}:src/evorec/infrastructure/r06_catalog_capture.py"],
        cwd=project, check=True, capture_output=True,
    ).stdout
    query = _sql_literal(source.decode("utf-8"))
    return query, dict(commit=commit, sql_sha256=hashlib.sha256(query.encode("utf-8")).hexdigest(),
                       module_sha256=hashlib.sha256(source).hexdigest())


def _capture(row):
    return CatalogCapture(**{**row, "inactive_ids": tuple(row["inactive_ids"])})


def plan_nodes(plan):
    """Retain measured plan facts without treating LIMIT as a scan guarantee."""
    result = []

    def visit(node):
        fields = ("Node Type", "Relation Name", "Index Name", "Actual Rows", "Actual Loops",
                  "Rows Removed by Filter", "Rows Removed by Join Filter", "Sort Method",
                  "Sort Space Used", "Sort Space Type", "Shared Hit Blocks", "Shared Read Blocks",
                  "Temp Read Blocks", "Temp Written Blocks")
        result.append({key: node[key] for key in fields if key in node})
        for child in node.get("Plans", []):
            visit(child)

    visit(plan[0]["Plan"])
    return result


def _reference(rows):
    # Only a cost reference from this owned, published initial catalog. Approval
    # predicates are independently tested; this object is not a production runtime.
    ordered = tuple(row["item_id"] for row in rows)
    if any(row["internal_item_id"] != index or not row["is_active"]
           or row["r06_model_text"] is None or row["r06_first_seen_ms"] is None
           for index, row in enumerate(rows)):
        raise ValueError("published initial directory is not canonical")
    records = {row["item_id"]: SimpleNamespace(first_seen_ms=row["r06_first_seen_ms"])
               for row in rows}
    digests = {row["item_id"]: hashlib.sha256(row["r06_model_text"].encode("utf-8")).digest()
               for row in rows}
    member, active = catalog_source_digests(ordered, records, digests)
    return SimpleNamespace(item_ids=ordered, catalog_items=records, catalog_text_sha256=digests,
                           member_sha256=member, full_active_sha256=active,
                           full_eligible_items=frozenset(ordered))


def _execute_observed(connection, database_url, query, parameters, *, many, enabled, observation):
    """Retain the failed SQL observation too; do not retry or replace its error."""
    observer = None
    started = executed = fetched = None
    try:
        observer = DatabaseCallObserver(connection, database_url) if enabled else None
        with observer if observer is not None else nullcontext():
            with connection.cursor(row_factory=dict_row, binary=True) as cursor:
                started = perf_counter()
                cursor.execute(query, parameters)
                executed = perf_counter()
                rows = cursor.fetchall() if many else cursor.fetchone()
                fetched = perf_counter()
        return rows
    except Exception as error:
        observation["operation_error_type"] = type(error).__name__
        raise
    finally:
        observation.update(execute_ms=(executed-started)*1000 if executed is not None else None,
                           fetch_decode_ms=(fetched-executed)*1000 if fetched is not None else None,
                           database_observation=observer.report() if observer is not None and observer.joined else None)


def profile(output, database_url, root, identity, digest, *, baseline_commit=None, observe_waits=False):
    if type(observe_waits) is not bool:
        raise ValueError("observe_waits must be a bool")
    project = Path(__file__).resolve().parents[1]
    commit, hashes = _source(project), subprocess_sources(project)
    baseline_query, baseline_source = (_baseline_sql(project, baseline_commit)
                                       if baseline_commit is not None else (LEGACY_SQL, None))
    baseline_name = "baseline" if baseline_commit is not None else "old"
    lab = R06ServiceLab(output, database_url, root, identity, digest,
                        gc_events=False, profile_samples=0)
    observations, plans, result = [], [], None
    try:
        with lab:
            ready = dict(lab.ready)
            # Stop the idle API to avoid its background activity in SQL timings.
            lab.stop()
            with psycopg.connect(lab.isolated_url, autocommit=True) as connection:
                with connection.cursor(row_factory=dict_row, binary=True) as cursor:
                    version = cursor.execute("SELECT version() AS version").fetchone()["version"]
                    settings = cursor.execute("SELECT current_setting('work_mem') AS work_mem, "
                                              "current_setting('plan_cache_mode') AS plan_cache_mode, "
                                              "current_setting('jit') AS jit").fetchone()
                    initial = cursor.execute(LEGACY_SQL, (identity,)).fetchall()
                if len(initial) != ready["item_count"]:
                    raise ValueError("full published model directory count changed")
                runtime = _reference(initial)
                del initial
                count = len(runtime.item_ids)
                queries = {baseline_name: (baseline_query, (identity, count+1, count)
                                           if baseline_commit is not None else (identity,)),
                           "new": (CATALOG_CAPTURE_SQL, (identity, count+1, count))}
                down_ids = list(runtime.item_ids[:3])
                for state in ("all_active", "three_inactive"):
                    if state == "three_inactive":
                        connection.execute("UPDATE items SET is_active=false WHERE item_id=ANY(%s)",
                                           (down_ids,))
                    expected = runtime.full_eligible_items.difference(down_ids if state != "all_active" else ())
                    reference_capture = None
                    for implementation in (baseline_name, "new", "new", baseline_name):
                        query, parameters = queries[implementation]
                        observation = dict(state=state, implementation=implementation, operation_error_type=None)
                        observations.append(observation)
                        rows = _execute_observed(connection, lab.isolated_url, query, parameters,
                            many=implementation == "old", enabled=observe_waits, observation=observation)
                        validated = None
                        if implementation != "old":
                            capture = _capture(rows)
                            validation_start = perf_counter()
                            actual = capture_eligible(runtime, capture)
                            validated = (perf_counter()-validation_start)*1000
                            if baseline_commit is not None:
                                if reference_capture is None:
                                    reference_capture = capture
                                elif capture != reference_capture:
                                    raise ValueError("baseline/candidate catalog summaries differ")
                        else:
                            actual = frozenset(row["item_id"] for row in rows if row["is_active"])
                        if actual != expected:
                            raise ValueError("SQL diagnostic eligibility changed")
                        observation.update(eligibility_validation_ms=validated,
                            candidate_eligibility_validation_ms=validated if implementation == "new" else None,
                            returned_rows=count if implementation == "old" else 1,
                            eligible_count=len(actual))
                        del rows, actual
                    for implementation in (baseline_name, "new"):
                        query, parameters = queries[implementation]
                        _explain(connection, query, parameters, state, implementation, plans)
                # Extra actual member is deliberately outside approval and has NULL
                # text/time: the N+1 count must reject it before text work.
                connection.execute("INSERT INTO items(item_id,title,category,is_active) "
                                   "VALUES ('capture_probe_extra','Owned extra','test',true)")
                connection.execute("INSERT INTO bundle_items(bundle_id,item_id,internal_item_id) "
                                   "VALUES (%s,'capture_probe_extra',%s)", (identity, count))
                for implementation in ((baseline_name, "new") if baseline_commit is not None else ("new",)):
                    query, parameters = queries[implementation]
                    with connection.cursor(row_factory=dict_row, binary=True) as cursor:
                        capture = _capture(cursor.execute(query, parameters).fetchone())
                    if (capture.member_count != count+1 or capture.active_count != 0
                            or capture.invalid_active_count != 0):
                        raise ValueError("overflow did not skip active catalog work")
                    try:
                        capture_eligible(runtime, capture)
                    except ManagementError as error:
                        if error.code != "bundle_members_changed":
                            raise
                    else:
                        raise ValueError("overflow was accepted")
                    _explain(connection, query, parameters, "overflow_one", implementation, plans)
                result = dict(source_commit=commit, source_sha256=hashes,
                    baseline_source=baseline_source,
                    wait_observer_enabled=observe_waits,
                    bundle_id=str(identity), manifest_sha256=digest, model_version=ready["model_version"],
                    item_count=count, database_version=version, driver_version=psycopg.__version__,
                    database_settings=settings, prepare_threshold=connection.prepare_threshold,
                    environment=dict(python=platform.python_version(), platform=platform.platform(),
                        processor=platform.processor(), logical_cpus=os.cpu_count(),
                        load_average=None, load_average_unavailable_on_windows=True),
                    measurements=observations, plans=plans, overflow_rejected=True,
                    timing_scope="driver execute and fetch/decode; not pure SQL or HTTP latency",
                    reference_scope="owned published initial directory; not independent approval proof",
                    explain_observation_cost_separate=True, production_acceptance=False)
    finally:
        if lab.output.exists():
            marker(lab.output, "catalog-observations", dict(source_commit=commit,
                source_sha256=hashes, baseline_source=baseline_source,
                wait_observer_enabled=observe_waits,
                measurements=observations, plans=plans, owned_schema_removed=not lab.created))
    if _source(project) != commit or subprocess_sources(project) != hashes:
        raise ValueError("source changed during catalog diagnostic")
    result.update(owned_schema_removed=not lab.created, normal_cpu_drain=lab.stopped[-1]["normal_cpu_drain"])
    marker(lab.output, "catalog-profile", result)
    return result


def _explain(connection, query, parameters, state, implementation, plans):
    started = perf_counter()
    raw = connection.execute("EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, FORMAT JSON) " + query,
                             parameters).fetchone()[0]
    plans.append(dict(state=state, implementation=implementation, raw=raw, nodes=plan_nodes(raw),
                      observation_elapsed_ms=(perf_counter()-started)*1000))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("managed_root", type=Path)
    parser.add_argument("bundle_id", type=UUID)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--baseline-commit", help="full commit SHA of the previous aggregate SQL")
    parser.add_argument("--observe-waits", action="store_true", help="owned PID sampling; changes observation cost")
    args = parser.parse_args()
    try:
        result = profile(args.output, os.environ["EVOREC_DATABASE_URL"], args.managed_root,
                         args.bundle_id, args.expected_manifest_sha256, baseline_commit=args.baseline_commit,
                         observe_waits=args.observe_waits)
    except Exception as error:
        print(json.dumps(dict(status="failed", error_type=type(error).__name__)))
        return 1
    complete = not args.observe_waits or (len(result["measurements"]) == 8 and all(
        row["database_observation"] is not None and row["database_observation"]["observer_complete"]
        for row in result["measurements"]))
    print(json.dumps(dict(status="observations_only" if complete else "incomplete_observations",
                          output=str(args.output), item_count=result["item_count"])))
    return 0 if complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
