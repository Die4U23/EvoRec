"""Clean-source, real loopback TCP acceptance in an owned ephemeral R06 demo.

Does not evaluate historical targets, browser UI, process restart or a load SLA.
The HTTP client's 10s timeout does not change the server's 2s request deadline.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
from time import perf_counter
from uuid import UUID, uuid4

import httpx
import psycopg

from evorec.domain.errors import ManagementError
from evorec.infrastructure.residual_ranker import ControlledLoadError
from scripts.assemble_r06_bundle import _source
from scripts.profile_r06_online import _schemas
from scripts.run_r06_demo import DemoProcess, marker
from scripts.verify_r06_online import SOURCE_FILES as ONLINE_SOURCES, _expected_popular

SOURCE_FILES = tuple(dict.fromkeys((*ONLINE_SOURCES, "scripts/run_r06_demo.py",
    "scripts/verify_r06_http.py", "tests/test_r06_demo.py", "web/index.html",
    "src/evorec/domain/session_profiles.py")))


def _sample(directory):
    """Independent file-order oracle; never calls the runtime's seed selector."""
    rows = json.loads((directory / "features/items.json").read_bytes())
    manifest = json.loads((directory / "features/manifest.json").read_bytes())
    dimension = manifest["dimension"]
    with (directory / "features/vectors.f32").open("rb") as stream:
        for row in rows:
            values = struct.unpack("<" + "f"*dimension, stream.read(4*dimension))
            if row["training_item"] and any(values):
                return row["item_id"]
    raise ValueError("approved package contains no represented training sample")


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def exercise(client, ready, directory, *, k=10):
    """Observable HTTP contracts; no DB edits, injected transport or instrumentation."""
    def ok(response, status=200):
        if response.status_code != status:
            code = response.json().get("error", {}).get("code", "unexpected_http_status")
            raise ManagementError(code, "actual loopback HTTP acceptance failed", response.status_code)
        return response.json()

    _require(ok(client.get("/health/live"))["status"] == "alive", "liveness differs")
    _require(ok(client.get("/health/ready"))["ready"] is True, "readiness differs")
    capabilities = ok(client.get("/api/v1/system"))["capabilities"]
    _require(capabilities["persistence"] and capabilities["r06_serving"]
             and not capabilities["catalog_publication"], "isolated capability flags differ")
    page = client.get("/app")
    _require(page.status_code == 200 and "固定演示历史" in page.text, "real page missing honest sample label")
    fresh = ok(client.post("/api/v1/sessions", json={"profile_id": "new"}), 201)
    _require(fresh["history"] == [], "new user is not empty")
    sample = ok(client.post("/api/v1/sessions", json={"profile_id": "sample"}), 201)
    seed = _sample(directory)
    _require(sample["history"] == [seed], "sample differs from independent approved-file oracle")
    path = "/api/v1/sessions/" + sample["session_id"]
    auth = {"X-Session-Token": sample["access_token"]}
    body = dict(session_id=sample["session_id"], expected_history_version=0, strategy="dense", k=k)
    headers = {**auth, "Idempotency-Key": str(uuid4())}
    started = perf_counter()
    dense = ok(client.post("/api/v1/recommendations", json=body, headers=headers))
    elapsed = perf_counter() - started

    def legal(result, excluded):
        items = result["items"]
        _require(result["actual_strategy"] == "dense" and result["fallback_reason"] is None
                 and result["bundle_id"] == ready["bundle_id"]
                 and result["model_version"] == ready["model_version"]
                 and len(items) == len({i["item_id"] for i in items}) == k
                 and all(i["source"] == "r06-a-frozen-s17" and i["item_id"] not in excluded for i in items),
                 "dense identity, legal uniqueness or requested Top-K differs")
    legal(dense, {seed})
    _require(ok(client.post("/api/v1/recommendations", json=body, headers=headers)) == dense,
             "completed recommendation replay differs")
    changed = client.post("/api/v1/recommendations", json={**body, "k": 1}, headers=headers)
    _require(changed.status_code == 409 and changed.json()["error"]["code"] == "recommendation_idempotency_conflict",
             "changed original key was not rejected")
    _require(client.post("/api/v1/recommendations", json=body,
                        headers={**headers, "X-Session-Token": "wrong"}).status_code == 401,
             "wrong recommendation owner accepted")
    popular = ok(client.post("/api/v1/recommendations", json={**body, "strategy": "popular"},
                             headers={**auth, "Idempotency-Key": str(uuid4())}))
    _require(popular["items"] == _expected_popular(directory, {seed}, popular["captured_at_ms"], k)
             and popular["actual_strategy"] == "popular" and popular["fallback_reason"] is None,
             "popular differs from approved raw training statistics")

    item, other = (i["item_id"] for i in dense["items"][:2])
    _require(ok(client.get("/api/v1/items/" + item))["item_id"] == item, "item detail differs")
    def event(kind, target=item, desired=None):
        payload = dict(event_id=str(uuid4()), session_id=sample["session_id"], request_id=dense["request_id"],
                       item_id=target, kind=kind, observed_at=datetime.now(timezone.utc).isoformat())
        if desired is not None:
            payload["desired_state"] = desired
        result = ok(client.post("/api/v1/feedback", json=payload, headers=auth))
        replay = ok(client.post("/api/v1/feedback", json=payload, headers=auth))
        _require(not result["replayed"] and replay["replayed"]
                 and result["history_version"] == replay["history_version"], "feedback replay mutated state")
        return payload
    event("detail_view")
    event("favorite_set", desired=True)
    state = ok(client.get(path, headers=auth))
    _require(state["favorite_items"] == [item] and item in state["history"], "favorite did not persist")
    event("favorite_set", desired=False)
    event("hide_set", other, True)
    _require(ok(client.get(path, headers=auth))["hidden_items"] == [other], "hide did not persist")
    stale_event = event("hide_set", other, False)
    state = ok(client.get(path, headers=auth))
    _require(state["hidden_items"] == state["favorite_items"] == [], "undo state did not persist")
    after = ok(client.post("/api/v1/recommendations", json={**body, "expected_history_version": state["history_version"]},
                           headers={**auth, "Idempotency-Key": str(uuid4())}))
    # R06 full_seen retains previous feedback, including undone states, until reset.
    legal(after, {seed, item, other})
    reset = ok(client.post(path + "/reset", headers=auth))
    _require(reset["history"] == [seed] and reset["epoch"] == 1
             and reset["hidden_items"] == reset["favorite_items"] == [], "reset differs from approved sample")
    rejected = client.post("/api/v1/feedback", json={**stale_event, "event_id": str(uuid4())}, headers=auth)
    _require(rejected.status_code == 409 and rejected.json()["error"]["code"] == "session_epoch_conflict",
             "old epoch feedback accepted")
    _require(ok(client.post("/api/v1/recommendations", json=body, headers=headers)) == dense,
             "stored original result differs after reset")
    empty = ok(client.post("/api/v1/recommendations", json={**body, "session_id": fresh["session_id"], "strategy": "popular"},
                           headers={"X-Session-Token": fresh["access_token"], "Idempotency-Key": str(uuid4())}))
    _require(empty["items"] == _expected_popular(directory, set(), empty["captured_at_ms"], k),
             "new-user popular differs from raw statistics")
    return dict(sample_item_id=seed, returned_items=k, single_tcp_dense_seconds=elapsed,
                actual_tcp_http=True, sample_matches_raw_package=True, new_user_empty=True,
                recommendation_replay_exact=True, changed_key_and_wrong_owner_rejected=True,
                training_popular_matches_raw_statistics=True, feedback_and_replay=True,
                detail_favorite_hide_undo_reset=True, full_seen_retains_undone_feedback=True,
                old_epoch_feedback_rejected=True, stored_recommendation_replays_after_reset=True)


def verify(output, database_url, root, bundle_id, digest, *, backend="numpy"):
    project = Path(__file__).resolve().parents[1]
    output = Path(output).resolve()
    if not output.is_relative_to(project / "artifacts") or output == project / "artifacts" or output.exists():
        raise ValueError("verification requires a fresh artifacts subdirectory")
    base = _source(project)
    before = _schemas(database_url)
    output.mkdir(parents=True, exist_ok=False)
    hashes = {}
    for name in SOURCE_FILES:
        raw = (project / name).read_bytes()
        if raw != subprocess.check_output(["git", "show", f"{base}:{name}"], cwd=project):
            raise ValueError("source differs from its Git blob")
        target = output / "source" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        hashes[name] = hashlib.sha256(raw).hexdigest()
    with DemoProcess(output / "server", database_url, root, bundle_id, digest, backend=backend) as process:
        with httpx.Client(base_url=process.ready["url"], timeout=10, trust_env=False) as client:
            result = exercise(client, process.ready, Path(root) / str(bundle_id))
        identity = {k: process.ready[k] for k in ("item_count", "model_version", "bundle_id", "manifest_sha256")}
    _require(_schemas(database_url) == before, "schema namespace did not return to original state")
    _require(_source(project) == base and all(hashlib.sha256((project/n).read_bytes()).hexdigest() == h
                                           for n,h in hashes.items()), "source changed during TCP acceptance")
    result.update(identity, status="passed", backend=backend, api_deadline_seconds=2.0,
                  cpu_jobs_drained=process.stopped["cpu_jobs_drained"], owned_schema_removed=True,
                  business_schema_untouched=True, browser_tested=False, process_restart_tested=False,
                  sustained_load_tested=False, sla_proven=False, test_targets_evaluated=False, retrained=False,
                  source=dict(base_commit=base, working_tree_dirty=False, source_sha256=hashes))
    marker(output, "verification", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("managed_root", type=Path)
    parser.add_argument("bundle_id", type=UUID)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--backend", choices=("numpy", "stdlib"), default="numpy")
    args = parser.parse_args(argv)
    database_url = os.getenv("EVOREC_DATABASE_URL")
    if not database_url:
        print(json.dumps(dict(status="failed", code="database_not_configured")))
        return 1
    try:
        result = verify(args.output, database_url, args.managed_root, args.bundle_id,
                        args.expected_manifest_sha256, backend=args.backend)
    except (ValueError, OSError, RuntimeError, psycopg.Error, httpx.HTTPError, ManagementError, ControlledLoadError) as error:
        print(json.dumps(dict(status="failed", code=getattr(error, "code", "verification_failed"),
                              error_type=type(error).__name__)))
        return 1
    print(json.dumps(dict(status=result["status"], output=str(args.output))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
