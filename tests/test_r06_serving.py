"""Real controlled synthetic files plus independent subset and snapshot expectations."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
import hashlib
import json
import math
import struct
from uuid import uuid4

import pytest

from evorec.domain.models import CatalogSnapshot, RequestContext, SessionSnapshot, Strategy
from evorec.domain.recommendation import select_results
from evorec.infrastructure import r06_serving as serving_module
from evorec.infrastructure.r06_features import load_r06_features
from evorec.infrastructure.r06_retrieval import load_r06_retrieval
from evorec.infrastructure.r06_serving import FrozenR06Request, R06SnapshotRanker, SERVING_POLICY
from evorec.infrastructure.residual_ranker import ControlledLoadError, PROVENANCE_HASHES, SCALAR_NAMES, load_residual_ranker
from test_r06_retrieval_runtime import _fixture, _record, _save, _samples


def _ranker(root, features):
    root.mkdir()
    weights = struct.pack("<21f", *([0.]*21))
    samples = [dict(context=context, candidates=[[1., 0.], [0., 1.]],
                    scalars=[[0., .5, .5, 0., 0., 0., 0., 0.], [0., .25, .25, 0., 0., 0., 0., 0.]],
                    expected_scores=[4., 2.], expected_top20=[0, 1]) for context in ([1., 0.], [0., 0.])]
    provenance = dict.fromkeys(PROVENANCE_HASHES, "a"*64)
    provenance.update({k: v for k, v in features.provenance.items() if k in PROVENANCE_HASHES})
    provenance.update(selected_method="A-frozen-s17", selection_protocol_id=features.provenance["selection_protocol_id"],
                      training_protocol_id=features.provenance["training_protocol_id"])
    manifest = dict(schema_version=1, kind="residual-list-mlp-v1", dtype="float32-le", dimension=2,
                    hidden=1, bottleneck=1, base_scale=8, residual_scale=4,
                    scalar_names=list(SCALAR_NAMES), provenance=provenance)
    for key, name, raw in (("weights", "weights.f32", weights),
                           ("validation", "validation.json", json.dumps(samples).encode())):
        (root / name).write_bytes(raw)
        manifest[key] = _record(name, raw)
    _save(root, manifest)
    return load_residual_ranker(root)


@pytest.fixture
def components(tmp_path):
    root, _, features = _fixture(tmp_path)
    retrieval = load_r06_retrieval(root, features)
    ranker = _ranker(tmp_path / "ranker", features)
    return R06SnapshotRanker(uuid4(), features, retrieval, ranker)


def _request(adapter, *, history=("a",), hidden=(), favorites=(), seen=None, eligible=None, timestamp=11):
    session = SessionSnapshot(uuid4(), 2, 3, history, frozenset(hidden), frozenset(favorites))
    context = RequestContext(uuid4(), session,
                             CatalogSnapshot(adapter.bundle_id, 4, frozenset(adapter.features.item_ids if eligible is None else eligible)))
    return FrozenR06Request(context, timestamp, frozenset(history) if seen is None else seen,
                             adapter.features.manifest_sha256, adapter.features.provenance["catalog_sha256"])


def test_independent_subset_order_scores_and_binding(components):
    request = _request(components, eligible={"c", "d", "e", "zero"})
    result = components.score(request)
    assert result.retrieval.collaborative == ("zero", "c", "d")
    assert result.retrieval.content == ("e", "c", "d", "zero")
    # The synthetic zero residual yields exactly f32(4*(CF-RRF+content-RRF)).
    cf = {item: struct.unpack("<f", struct.pack("<f", 61/(60+i)))[0]
          for i, item in enumerate(("zero", "c", "d"), 1)}
    content = {item: struct.unpack("<f", struct.pack("<f", 61/(60+i)))[0]
               for i, item in enumerate(("e", "c", "d", "zero"), 1)}
    expected = {item: struct.unpack("<f", struct.pack("<f", 4*(cf.get(item, 0.)+content[item])))[0] for item in content}
    assert {candidate.item_id: candidate.score for candidate in result.batch.candidates} == expected
    assert result.batch.binding == request.context.binding and result.batch.actual_strategy == Strategy.DENSE
    assert all(c.source == "r06-a-frozen-s17" for c in result.batch.candidates)
    assert result.model_version == components.model_version and result.serving_policy == SERVING_POLICY
    assert [c.item_id for c in select_results(result.batch.candidates, request.context, 20)] == sorted(expected, key=lambda i: (-expected[i], i))


def test_full_seen_survives_last50_truncation_and_unknown_positions(components):
    history = ("a",) + ("ghost",)*50
    request = _request(components, history=history, seen={"a", "ghost", "b"})
    result = components.score(request)
    assert (result.history_input_count, result.history_used_count, result.unknown_history_count) == (51, 50, 50)
    assert set(c.item_id for c in result.batch.candidates).isdisjoint({"a", "b", "ghost"})
    expected = components.retrieval.retrieve(("ghost",)*50, {"a", "ghost", "b"}, 11)
    assert result.retrieval == expected
    with pytest.raises(ControlledLoadError, match="entire session history"):
        _request(components, history=history, seen={"ghost"})


def test_unknown_history_not_removed_before_decay_and_hidden_favorite_exclusion(components):
    request = _request(components, history=("a", "ghost"), hidden={"b"}, favorites={"zero"})
    result = components.score(request)
    assert request.full_seen == frozenset({"a", "ghost", "b", "zero"})
    direct = components.retrieval.retrieve(("a", "ghost"), request.full_seen, 11)
    assert result.retrieval == direct and result.unknown_history_count == 1
    pool = components.features.build_pool(("a", "ghost"), request.full_seen, 11, direct.collaborative, direct.content)
    clean = components.features.build_pool(("a",), request.full_seen, 11, direct.collaborative, direct.content)
    assert pool.scalars[0][6] > clean.scalars[0][6]  # Unknown still counts in history length.


def test_empty_catalog_and_future_boundary_do_not_invent_results(components):
    assert components.score(_request(components, eligible=set())).batch.candidates == ()
    result = components.score(_request(components, eligible={"e"}, timestamp=10))
    assert result.batch.candidates == ()  # first_seen == request is not available
    with pytest.raises(ControlledLoadError):
        components.score(_request(components, history=("e",)+( "ghost",)*50, timestamp=10))


@pytest.mark.parametrize("field,value", [("features_manifest_sha256", "0"*64), ("catalog_sha256", "0"*64)])
def test_same_ids_do_not_approve_changed_catalog_or_features(components, field, value):
    with pytest.raises(ControlledLoadError) as error:
        components.score(replace(_request(components), **{field: value}))
    assert error.value.code == "catalog_changed"


def test_new_catalog_ids_and_wrong_bundle_are_rejected_before_scoring(components):
    with pytest.raises(ControlledLoadError) as error:
        components.score(_request(components, eligible={"new-product"}))
    assert error.value.code == "catalog_changed"
    request = _request(components)
    bad = replace(request.context, catalog=replace(request.context.catalog, bundle_id=uuid4()))
    with pytest.raises(ControlledLoadError) as error:
        components.score(replace(request, context=bad))
    assert error.value.code == "catalog_changed"


@pytest.mark.parametrize("timestamp", [True, -1, 1.5, 253402300800000])
def test_admitted_time_is_explicit_and_bounded(components, timestamp):
    with pytest.raises(ControlledLoadError):
        _request(components, timestamp=timestamp)


@pytest.mark.parametrize("target,field,value", [("session", "epoch", True), ("session", "history_version", 1.5),
    ("catalog", "exclusion_version", True), ("session", "session_id", "bad"), ("catalog", "bundle_id", "bad")])
def test_snapshot_version_and_uuid_types_are_strict(components, target, field, value):
    request = _request(components)
    nested = replace(getattr(request.context, target), **{field: value})
    with pytest.raises(ControlledLoadError):
        replace(request, context=replace(request.context, **{target: nested}))


def test_input_exclusion_limit_and_mutable_source_are_handled(components):
    seen = {"a"}
    request = _request(components, seen=seen)
    seen.add("b")
    assert request.full_seen == frozenset({"a"})
    with pytest.raises(FrozenInstanceError):
        request.timestamp_ms = 12
    with pytest.raises(ControlledLoadError):
        _request(components, seen={f"x{i}" for i in range(10001)})
    with pytest.raises(ControlledLoadError):
        _request(components, seen={"a"}|{f"x{i}" for i in range(9999)}, hidden={"b"})
    with pytest.raises(ControlledLoadError):
        _request(components, history=("a",)*10001)


def test_frozen_request_validates_every_eligible_string_without_ids_tuple(monkeypatch, components):
    visited = []

    class TrackedID(str):
        def strip(self, chars=None):
            visited.append(self)
            return super().strip(chars)

    eligible = tuple(TrackedID(item) for item in components.features.item_ids)
    original_ids = serving_module._ids

    def reject_catalog_materialization(values, limit, **kwargs):
        if limit == serving_module.MAX_ITEMS:
            pytest.fail("eligible catalog must be validated without an intermediate tuple")
        return original_ids(values, limit, **kwargs)

    monkeypatch.setattr(serving_module, "_ids", reject_catalog_materialization)
    request = _request(components, eligible=eligible)
    assert request.context.catalog.eligible_items == frozenset(eligible)
    assert len(visited) == len(eligible) and set(visited) == set(eligible)


@pytest.mark.parametrize("item", ["", "   ", "x" * 129, None, 7])
def test_frozen_request_keeps_eligible_item_shape_errors(components, item):
    with pytest.raises(ControlledLoadError) as error:
        _request(components, eligible={item})
    assert error.value.code == "input_shape"


def test_frozen_request_keeps_eligible_resource_limit(components, monkeypatch):
    monkeypatch.setattr(serving_module, "MAX_ITEMS", len(components.features.item_ids) - 1)
    with pytest.raises(ControlledLoadError) as error:
        _request(components)
    assert error.value.code == "resource_limit"


def test_component_pairing_model_identity_and_independent_requests(components):
    with pytest.raises(ControlledLoadError) as error:
        replace(components, features=replace(components.features))
    assert error.value.code == "component_changed"
    changed = replace(components.ranker, provenance={**components.ranker.provenance, "training_protocol_id": "f"*16})
    with pytest.raises(ControlledLoadError):
        replace(components, ranker=changed)
    assert replace(components, bundle_id=uuid4()).model_version != components.model_version
    requests = [_request(components), _request(components, eligible={"d", "e"}), _request(components, history=())]
    expected = [components.score(r) for r in requests]
    with ThreadPoolExecutor(max_workers=4) as executor:
        actual = list(executor.map(lambda i: components.score(requests[i%3]), range(30)))
    assert actual == [expected[i%3] for i in range(30)]


def _large_fixture(tmp_path, count=601):
    """Zero graph and tied content with descending known priors; full file loaders."""
    root, _, _ = _fixture(tmp_path)
    froot = tmp_path / "features"
    ids = tuple(f"item{i:04}" for i in range(count))
    counts = [float(count-i) for i in range(count)]
    vectors = struct.pack(f"<{count*2}f", *([1., 0.]*count))
    manifest = json.loads((froot / "manifest.json").read_text())
    fp = hashlib.sha256(vectors)
    fp.update(json.dumps(ids).encode())
    manifest["provenance"]["feature_fingerprint"] = fp.hexdigest()
    records = [dict(item_id=item, first_seen_ms=1, training_item=True,
                    prior_strength=math.log1p(value)/math.log1p(count)) for item, value in zip(ids, counts)]
    for key, name, raw in (("items", "items.json", json.dumps(records).encode()), ("vectors", "vectors.f32", vectors)):
        (froot / name).write_bytes(raw)
        manifest[key] = _record(name, raw)
    manifest["item_count"] = count
    _save(froot, manifest)
    features = load_r06_features(froot)
    retrieval_manifest = json.loads((root / "manifest.json").read_text())
    samples = [dict(history=[ids[0]], seen=[ids[0]], timestamp_ms=11, collaborative=list(ids[1:201]), content=list(ids[1:201])),
               dict(history=[], seen=[], timestamp_ms=11, collaborative=list(ids[:200]), content=list(ids[:200]))]
    for key, name, raw in (("statistics", "statistics.json", json.dumps(counts).encode()),
                           ("neighbors", "neighbors.bin", bytes(4*(count+1))),
                           ("validation", "validation.json", json.dumps(samples).encode())):
        (root / name).write_bytes(raw)
        retrieval_manifest[key] = _record(name, raw)
    retrieval_manifest.update(item_count=count, edge_count=0,
        provenance={**features.provenance, "features_manifest_sha256": features.manifest_sha256, "selected_method": "A-frozen-s17"},
        fit=dict(end_ms=10, training_rows=int(sum(counts)), positive_rating_min=4))
    _save(root, retrieval_manifest)
    return root, features


def test_filter_before_top200_refills_from_legal_catalog(tmp_path):
    root, features = _large_fixture(tmp_path)
    runtime = load_r06_retrieval(root, features)
    eligible = frozenset(features.item_ids[400:])
    original = runtime.retrieve([features.item_ids[0]], {features.item_ids[0]}, 11)
    assert not eligible.intersection(original.collaborative + original.content)
    actual = runtime.retrieve([features.item_ids[0]], {features.item_ids[0]}, 11, eligible_items=eligible)
    assert actual.collaborative == actual.content == features.item_ids[400:600]
    assert len(runtime.build_pool([], [], 11, eligible_items=eligible).item_ids) == 200


@pytest.mark.parametrize("eligible", [True, "abc", ["a", "a"], ["missing"], [None], range(200001)])
def test_retrieval_subset_validation_is_not_a_silent_intersection(components, eligible):
    with pytest.raises(ControlledLoadError):
        components.retrieval.retrieve([], [], 11, eligible_items=eligible)


def test_empty_subset_does_not_bypass_invalid_history(components):
    with pytest.raises(ControlledLoadError):
        components.retrieval.retrieve(["e"], {"e"}, 10, eligible_items=set())


@pytest.fixture
def replay_harness(tmp_path, monkeypatch, components):
    """Synthetic formula references; mocked Git records are never production evidence."""
    from scripts import verify_r06_serving as script
    project = tmp_path / "project"
    source = project / "scripts/verify_r06_serving.py"
    source.parent.mkdir(parents=True)
    source.write_text("synthetic source")
    monkeypatch.setattr(script, "__file__", str(source))
    monkeypatch.setattr(script, "SOURCE_FILES", ("scripts/verify_r06_serving.py",))
    monkeypatch.setattr(script.subprocess, "check_output", lambda argv, **kwargs: "a"*40 if "rev-parse" in argv else "")
    samples, references = [], []
    f32 = lambda v: struct.unpack("<f", struct.pack("<f", v))[0]
    for sample in _samples():
        context = [1., 0.] if sample["history"] else [0., 0.]
        ids = sorted(set(sample["collaborative"]) | set(sample["content"]))
        cf = {item: 61/(60+i) for i, item in enumerate(sample["collaborative"], 1)}
        content = {item: 61/(60+i) for i, item in enumerate(sample["content"], 1)}
        scalars = []
        for item in ids:
            meta = components.features._metadata[components.features._indices[item]]
            age = math.log1p((11-meta.first_seen_ms)/86400000)/math.log1p(3650)
            scalars.append([f32(v) for v in [context[0]*components.features.vector(item)[0], cf.get(item, 0.),
                content.get(item, 0.), meta.prior_strength, float(not meta.training_item), age,
                math.log1p(len(sample["history"]))/math.log1p(50), float(bool(sample["history"]))]])
        scores = [f32(4*(s[1]+s[2])) for s in scalars]
        samples.append({**sample, "expected_items": ids, "expected_context": context, "expected_scalars": scalars})
        references.append({"expected_scores": scores, "expected_top20": sorted(range(len(ids)), key=lambda i: -scores[i])})
    monkeypatch.setattr(script, "_validation", lambda root, digest: samples if root == tmp_path/"features" else references)
    output = project / "artifacts/replay"
    def run(**kwargs):
        return script.verify(output, tmp_path/"features", components.features.manifest_sha256,
                             tmp_path/"retrieval", components.retrieval.manifest_sha256,
                             tmp_path/"ranker", components.ranker.manifest_sha256, **kwargs)
    return script, output, samples, run


def test_recorded_replay_real_components_and_independent_formula(replay_harness):
    _, output, _, run = replay_harness
    result = run()
    assert result["status"] == "passed" and not result["activated"]
    assert result["request_bindings_exact"] and result["legal_top20_exact"]
    assert [row["candidate_count"] for row in result["rows"]] == [5, 5]
    assert json.loads((output/"verification.json").read_text()) == result
    assert (output/"source/scripts/verify_r06_serving.py").read_text() == "synthetic source"


def test_recorded_replay_existing_evidence_is_protected(replay_harness):
    _, output, _, run = replay_harness
    output.mkdir(parents=True)
    (output/"verification.json").write_text("older evidence")
    with pytest.raises(FileExistsError):
        run()
    assert (output/"verification.json").read_text() == "older evidence"


def test_recorded_replay_dirty_source_stops_before_output(replay_harness, monkeypatch):
    script, output, _, run = replay_harness
    monkeypatch.setattr(script.subprocess, "check_output", lambda argv, **kwargs: " M changed.py" if "status" in argv else "a"*40)
    with pytest.raises(ControlledLoadError) as error:
        run()
    assert error.value.code == "source_dirty" and not output.exists()


def test_recorded_replay_wrong_golden_order_does_not_publish_pass(replay_harness):
    _, output, samples, run = replay_harness
    samples[0]["content"][0:2] = samples[0]["content"][0:2][::-1]
    with pytest.raises(ValueError, match="order"):
        run()
    assert output.exists() and not (output/"verification.json").exists()


@pytest.mark.parametrize("fault", ["binding", "scores"])
def test_recorded_replay_checks_actual_batch_not_only_recomputed_pool(replay_harness, monkeypatch, fault):
    _, output, _, run = replay_harness
    original = R06SnapshotRanker.score
    def altered(self, request):
        result = original(self, request)
        if fault == "binding":
            batch = replace(result.batch, binding=replace(result.batch.binding, request_id=uuid4()))
        else:
            batch = replace(result.batch, candidates=tuple(replace(c, score=c.score+1.) for c in result.batch.candidates))
        return replace(result, batch=batch)
    monkeypatch.setattr(R06SnapshotRanker, "score", altered)
    with pytest.raises(ValueError, match="batch"):
        run()
    assert not (output/"verification.json").exists()


def test_recorded_replay_interruption_leaves_diagnostics_not_pass(replay_harness, monkeypatch):
    script, output, _, run = replay_harness
    def stop(*args):
        raise KeyboardInterrupt()
    monkeypatch.setattr(script, "_verify", stop)
    with pytest.raises(KeyboardInterrupt):
        run()
    assert output.exists() and not (output/"verification.json").exists()


def test_recorded_replay_write_failure_revokes_own_report(replay_harness, monkeypatch):
    from pathlib import Path
    _, output, _, run = replay_harness
    original = Path.open
    class FailedStream:
        def __init__(self, stream): self.stream = stream
        def __enter__(self): return self
        def __exit__(self, *args): self.stream.close()
        def write(self, raw):
            self.stream.write(b"partial")
            raise OSError("disk full")
    def opened(path, *args, **kwargs):
        stream = original(path, *args, **kwargs)
        return FailedStream(stream) if path.name == "verification.json" and args == ("xb",) else stream
    monkeypatch.setattr(Path, "open", opened)
    with pytest.raises(OSError):
        run()
    assert not (output/"verification.json").exists()


def test_recorded_replay_report_race_protects_other_writer(replay_harness, monkeypatch):
    from pathlib import Path
    _, output, _, run = replay_harness
    original = Path.open
    def opened(path, *args, **kwargs):
        if path.name == "verification.json" and args == ("xb",):
            with original(path, "w") as stream:
                stream.write("other writer")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", opened)
    with pytest.raises(FileExistsError):
        run()
    assert (output/"verification.json").read_text() == "other writer"
