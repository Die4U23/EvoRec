"""Real synthetic controlled files, independent identities and hostile packaging."""

from dataclasses import FrozenInstanceError, replace
import hashlib
import json
import math
from pathlib import Path
import struct
import subprocess
import sys
from uuid import uuid4

import pytest

from evorec.infrastructure import r06_bundle as module
from evorec.infrastructure.content_encoder import BINDING_KEYS
from evorec.infrastructure.r06_features import load_r06_features
from evorec.infrastructure.r06_bundle import FrozenCatalogItem, assemble_r06_bundle, load_r06_bundle
from evorec.infrastructure.residual_ranker import ControlledLoadError
from test_content_encoder_runtime import _fixture as encoder_fixture
from test_r06_retrieval_runtime import COUNTS, IDS, _fixture, _save, _samples
from test_r06_serving import _ranker, _request


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _inputs(tmp_path):
    retrieval, rm, features = _fixture(tmp_path)
    catalog = tmp_path / "original-catalog.json"
    metadata = tmp_path / "original-metadata.json"
    catalog.write_text(json.dumps({item: row.first_seen_ms for item, row in
                                  zip(features.item_ids, features._metadata, strict=True)}), encoding="utf-8")
    # Missing original text is explicitly the empty string, not a made-up title.
    metadata.write_text(json.dumps({"a": "中文 alpha", "b": "beta"}, ensure_ascii=False), encoding="utf-8")
    froot = tmp_path / "features"
    fm = json.loads((froot / "manifest.json").read_bytes())
    fm["provenance"]["catalog_sha256"] = _sha(catalog)
    _save(froot, fm)
    features = load_r06_features(froot)
    rm["provenance"].update(features.provenance)
    rm["provenance"]["features_manifest_sha256"] = features.manifest_sha256
    _save(retrieval, rm)
    _ranker(tmp_path / "ranker", features)
    encoder, em, _ = encoder_fixture(tmp_path)
    em["provenance"].update({k: features.provenance[k] for k in BINDING_KEYS})
    em["provenance"]["metadata_sha256"] = _sha(metadata)
    _save(encoder, em)
    dirs = {role: tmp_path / role for role in module.FILES}
    digests = {role: _sha(root / "manifest.json") for role, root in dirs.items()}
    return dirs, digests, catalog, metadata


def _build(tmp_path):
    inputs = _inputs(tmp_path)
    root = tmp_path / "managed"
    root.mkdir()
    bundle_id = uuid4()
    digest = assemble_r06_bundle(root, bundle_id, *inputs, assembly_revision="a"*40)
    return root, root / str(bundle_id), digest


@pytest.fixture
def package(tmp_path):
    return _build(tmp_path)


def _load(package, **kwargs):
    root, target, digest = package
    return load_r06_bundle(root, target, expected_manifest_sha256=digest, **kwargs)


def _records(bundle, eligible):
    return tuple(FrozenCatalogItem(item, {"a": "中文 alpha", "b": "beta"}.get(item, ""),
                                   10 if item == "e" else 1) for item in sorted(eligible))


def _outer_change(package, change):
    root, target, _ = package
    path = target / "manifest.json"
    manifest = json.loads(path.read_bytes())
    change(manifest)
    _save(target, manifest)
    return root, target, _sha(path)


def test_self_contained_copy_identity_and_real_scoring(package, tmp_path):
    bundle = _load(package)
    request = _request(bundle.adapter, eligible={"c", "d", "e", "zero"})
    items = _records(bundle, request.context.catalog.eligible_items)
    actual = bundle.score(request.context, 11, request.full_seen, items)
    expected = bundle.adapter.score(request)
    assert actual.batch == expected.batch and actual.retrieval == expected.retrieval
    assert actual.model_version == bundle.model_version != expected.model_version
    independent = hashlib.sha256(json.dumps(["a", 1, "中文 alpha"], ensure_ascii=False,
                                            separators=(",", ":")).encode()).hexdigest()
    assert bundle.catalog_item_sha256["a"] == independent
    # Originals are not referenced by the assembled package after loading.
    (tmp_path / "features/vectors.f32").write_bytes(b"destroy source")
    assert _load(package).adapter.features._vectors == bundle.adapter.features._vectors
    with pytest.raises(TypeError):
        bundle.catalog_item_sha256["a"] = "x"
    with pytest.raises(FrozenInstanceError):
        bundle.manifest_sha256 = "a"*64


def test_full_catalog_seal_preserves_old_canonical_bytes_and_owns_mapping(package):
    bundle = _load(package)
    identities = dict(bundle.catalog_item_sha256)
    independent = hashlib.sha256(json.dumps(
        [module.KIND, bundle.manifest_sha256, sorted(identities.items())],
        ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
    assert bundle.full_catalog_seal == independent
    copied = replace(bundle, catalog_item_sha256=identities)
    identities["a"] = "0"*64
    assert copied.full_catalog_seal == independent
    assert copied.catalog_item_sha256["a"] == bundle.catalog_item_sha256["a"]
    assert replace(bundle, manifest_sha256="b"*64).full_catalog_seal != independent
    with pytest.raises(FrozenInstanceError):
        bundle.full_catalog_seal = "0"*64


@pytest.mark.parametrize("identities", [[], [("中文", "a"*64), ("alpha", "b"*64)],
                                       [("a", "1"*64), ("a", "1"*64)]])
def test_catalog_seal_canonical_encoding_matches_independent_legacy_formula(identities):
    # Duplicate identities must not silently become a full-set cached seal.
    independent = hashlib.sha256(json.dumps([module.KIND, "c"*64, sorted(identities)],
        ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
    assert module.catalog_seal("c"*64, identities[::-1]) == independent


@pytest.mark.parametrize("change", [
    lambda rows: rows[:-1], lambda rows: rows + rows[:1],
    lambda rows: (replace(rows[0], text="changed"), *rows[1:]),
    lambda rows: (replace(rows[0], first_seen_ms=2), *rows[1:]),
    lambda rows: (*rows, FrozenCatalogItem("new", "", 1)),
])
def test_actual_catalog_rejects_drift_duplicates_and_incomplete_coverage(package, change):
    bundle = _load(package)
    context = _request(bundle.adapter).context
    with pytest.raises(ControlledLoadError) as error:
        bundle.score(context, 11, {"a"}, change(_records(bundle, context.catalog.eligible_items)))
    assert error.value.code == "catalog_changed"


@pytest.mark.parametrize("items", [None, "a", ("a",), (FrozenCatalogItem("a", "\ud800", 1),),
    (FrozenCatalogItem("a", "x"*32769, 1),), (FrozenCatalogItem("a", "", True),),
    (FrozenCatalogItem("a", "", 1.),), (FrozenCatalogItem("", "", 1),)])
def test_actual_catalog_input_shape(package, items):
    bundle = _load(package)
    with pytest.raises(ControlledLoadError):
        bundle.request(_request(bundle.adapter).context, 11, {"a"}, items)


def test_subset_empty_reordering_and_bundle_binding(package):
    bundle = _load(package)
    for eligible in (set(), {"b", "c"}):
        request = _request(bundle.adapter, eligible=eligible)
        rows = _records(bundle, eligible)
        assert bundle.score(request.context, 11, {"a"}, rows) == bundle.score(request.context, 11, {"a"}, rows[::-1])
    context = _request(bundle.adapter).context
    context = replace(context, catalog=replace(context.catalog, bundle_id=uuid4()))
    with pytest.raises(ControlledLoadError) as error:
        bundle.request(context, 11, {"a"}, _records(bundle, context.catalog.eligible_items))
    assert error.value.code == "catalog_changed"


@pytest.mark.parametrize("key,value,code", [
    ("schema_version", True, "unsupported_format"), ("schema_version", 2, "unsupported_format"),
    ("kind", "cpu-demo", "unsupported_format"), ("serving_policy", "other", "unsupported_format"),
    ("catalog_policy", "id-only", "unsupported_format"), ("assembly_revision", "short", "component_schema"),
    ("components", {}, "component_schema"), ("components", {**dict.fromkeys(module.FILES, "a"*64), "extra": "a"*64}, "component_schema"),
    ("bundle_id", str(uuid4()), "bundle_identity_mismatch"), ("bundle_id", None, "component_schema"),
    ("extra", 1, "component_schema"),
])
def test_outer_schema(package, key, value, code):
    package = _outer_change(package, lambda m: m.update({key: value}))
    with pytest.raises(ControlledLoadError) as error:
        _load(package)
    assert error.value.code == code


@pytest.mark.parametrize("role", module.FILES)
def test_every_component_approval_is_required(package, role):
    changed = _outer_change(package, lambda m: m["components"].update({role: "a"*64}))
    with pytest.raises(ControlledLoadError) as error:
        _load(changed)
    assert error.value.code == "component_changed"


@pytest.mark.parametrize("path", ["catalog.json", "metadata.json", "features/vectors.f32", "retrieval/neighbors.bin", "ranker/weights.f32", "encoder/weights.f32"])
def test_raw_payload_tamper_rejected(package, path):
    (package[1] / path).write_bytes(b"tamper")
    with pytest.raises(ControlledLoadError):
        _load(package)


@pytest.mark.parametrize("path", ["extra.json", "features/extra.json", "encoder/extra.json"])
def test_unlisted_files_rejected(package, path):
    (package[1] / path).write_text("{}")
    with pytest.raises(ControlledLoadError) as error:
        _load(package)
    assert error.value.code == "unsupported_format"


def test_root_and_resource_limits_before_deserialization(package, monkeypatch):
    root, target, digest = package
    with pytest.raises(ControlledLoadError):
        load_r06_bundle(target, target, expected_manifest_sha256=digest)
    monkeypatch.setattr(module, "MAX_TOTAL_BYTES", 1)
    monkeypatch.setattr(module, "load_r06_features", lambda *a, **k: pytest.fail("must reject before loading"))
    with pytest.raises(ControlledLoadError) as error:
        _load(package)
    assert error.value.code == "resource_limit"


def test_source_record_rehashed_but_not_original_provenance(package):
    root, target, _ = package
    path = target / "metadata.json"
    path.write_text('{"a":"forged"}')
    package = _outer_change(package, lambda m: m["metadata"].update(size_bytes=path.stat().st_size, sha256=_sha(path)))
    with pytest.raises(ControlledLoadError) as error:
        _load(package)
    assert error.value.code == "component_changed"


def test_no_approval_and_wrong_approval(package):
    with pytest.raises(TypeError):
        load_r06_bundle(*package[:2])
    for digest in (None, True, "A"*64, "a"*64):
        with pytest.raises(ControlledLoadError):
            load_r06_bundle(*package[:2], expected_manifest_sha256=digest)


def test_source_extra_file_not_silently_sanitized(tmp_path):
    inputs = _inputs(tmp_path)
    (inputs[0]["features"] / "surprise.pkl").write_bytes(b"not allowed")
    with pytest.raises(ControlledLoadError) as error:
        assemble_r06_bundle(tmp_path, uuid4(), *inputs, assembly_revision="a"*40)
    assert error.value.code == "unsupported_format"


def test_existing_destination_is_never_overwritten(tmp_path):
    inputs = _inputs(tmp_path)
    identity = uuid4()
    target = tmp_path / str(identity)
    target.mkdir()
    (target / "manifest.json").write_bytes(b"old proof")
    with pytest.raises(FileExistsError):
        assemble_r06_bundle(tmp_path, identity, *inputs, assembly_revision="a"*40)
    assert (target / "manifest.json").read_bytes() == b"old proof"


def test_failed_commit_marker_is_removed_but_diagnostics_remain(tmp_path, monkeypatch):
    inputs = _inputs(tmp_path)
    identity = uuid4()
    original = Path.open
    class FailedWrite:
        def __init__(self, stream): self.stream = stream
        def __enter__(self): return self
        def write(self, raw):
            self.stream.write(raw[:5])
            raise OSError("synthetic disk failure")
        def __exit__(self, *args): self.stream.close()
    def fail(path, mode="r", *a, **k):
        stream = original(path, mode, *a, **k)
        if path == tmp_path / str(identity) / "manifest.json" and mode == "xb":
            return FailedWrite(stream)
        return stream
    monkeypatch.setattr(Path, "open", fail)
    with pytest.raises(OSError):
        assemble_r06_bundle(tmp_path, identity, *inputs, assembly_revision="a"*40)
    assert not (tmp_path / str(identity) / "manifest.json").exists()
    assert (tmp_path / str(identity) / "catalog.json").is_file()


def test_pure_runtime_imports_no_research_or_numerical_packages():
    code = "from evorec.infrastructure.r06_bundle import load_r06_bundle; import sys; assert not any(n.split('.')[0] in {'numpy','torch','sklearn','scipy','joblib'} for n in sys.modules)"
    subprocess.run([sys.executable, "-c", code], check=True)


def test_load_cli_success_and_failure(package, capsys):
    from scripts.load_r06_bundle import main
    args = [str(package[0]), str(package[1]), "--expected-manifest-sha256", package[2]]
    assert main(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["activated"] is False and report["item_count"] == 6
    args[-1] = "a"*64
    assert main(args) == 1
    assert json.loads(capsys.readouterr().out)["code"] == "component_changed"


@pytest.fixture
def package_replay(package, tmp_path, monkeypatch):
    from scripts import verify_r06_bundle as script
    project = tmp_path / "synthetic-project"
    source = project / "scripts/replay.py"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"synthetic replay source")
    monkeypatch.setattr(script, "__file__", str(source))
    monkeypatch.setattr(script, "SOURCE_FILES", ("scripts/replay.py",))
    monkeypatch.setattr(script, "_source", lambda project: "a"*40)
    samples, references = [], []
    f32 = lambda v: struct.unpack("<f", struct.pack("<f", v))[0]
    vectors = dict(zip(IDS, ([1., 0.], [0., 1.], [.6, .8], [-1., 0.], [1., 0.], [0., 0.]), strict=True))
    for sample in _samples():
        ids = sorted(set(sample["collaborative"]) | set(sample["content"]))
        context = [1., 0.] if sample["history"] else [0., 0.]
        cf = {item: 61/(60+i) for i, item in enumerate(sample["collaborative"], 1)}
        content = {item: 61/(60+i) for i, item in enumerate(sample["content"], 1)}
        scalars = [[f32(v) for v in (context[0]*f32(vectors[item][0]), cf.get(item, 0.), content.get(item, 0.),
                    math.log1p(COUNTS[IDS.index(item)])/math.log(5), float(item == "e"),
                    math.log1p((11-(10 if item == "e" else 1))/86400000)/math.log1p(3650),
                    math.log1p(len(sample["history"]))/math.log1p(50), float(bool(sample["history"])))] for item in ids]
        scores = [f32(4*(s[1]+s[2])) for s in scalars]
        samples.append({**sample, "expected_items": ids, "expected_context": context, "expected_scalars": scalars})
        references.append(dict(expected_scores=scores, expected_top20=sorted(range(len(ids)), key=lambda i: -scores[i])))
    monkeypatch.setattr(script, "_validation", lambda root, digest: samples if root.name == "features" else references)
    output = project / "artifacts/replay"
    def run():
        return script.verify(output, *package)
    return script, output, source, samples, run


def test_bundle_replay_independent_formulas_and_actual_catalog(package_replay):
    _, output, _, _, run = package_replay
    result = run()
    assert result["actual_catalog_items_checked"] == 6
    assert result["request_bindings_exact"] and result["legal_top20_exact"] and not result["activated"]
    assert [row["candidate_count"] for row in result["rows"]] == [5, 5]
    assert json.loads((output / "verification.json").read_bytes()) == result


def test_bundle_replay_old_evidence_and_dirty_source_protected(package_replay, monkeypatch):
    script, output, _, _, run = package_replay
    def dirty(project):
        raise ControlledLoadError("source_dirty", "synthetic dirty source")
    monkeypatch.setattr(script, "_source", dirty)
    with pytest.raises(ControlledLoadError): run()
    assert not output.exists()
    output.mkdir(parents=True)
    (output / "verification.json").write_bytes(b"old proof")
    with pytest.raises(FileExistsError): run()
    assert (output / "verification.json").read_bytes() == b"old proof"


@pytest.mark.parametrize("fault", ["provider_order", "batch_score", "version", "binding", "source"])
def test_bundle_replay_rejects_actual_output_faults(package_replay, monkeypatch, fault):
    script, output, source, samples, run = package_replay
    if fault == "provider_order":
        samples[0]["content"] = samples[0]["content"][::-1]
    else:
        original = module.FrozenR06Bundle.score
        def corrupt(self, *a, **k):
            result = original(self, *a, **k)
            if fault == "source": source.write_bytes(b"changed source")
            if fault == "version": return replace(result, model_version="a"*64)
            if fault == "binding": return replace(result, batch=replace(result.batch, binding=replace(result.batch.binding, request_id=uuid4())))
            if fault == "batch_score":
                rows = (replace(result.batch.candidates[0], score=999.), *result.batch.candidates[1:])
                return replace(result, batch=replace(result.batch, candidates=rows))
            return result
        monkeypatch.setattr(module.FrozenR06Bundle, "score", corrupt)
    with pytest.raises(ValueError): run()
    assert not (output / "verification.json").exists()


@pytest.mark.parametrize("error", [OSError, KeyboardInterrupt])
def test_bundle_replay_report_write_failure_withdraws_only_own_marker(package_replay, monkeypatch, error):
    _, output, _, _, run = package_replay
    original = Path.open
    class FailedWrite:
        def __init__(self, stream): self.stream = stream
        def __enter__(self): return self
        def write(self, raw):
            self.stream.write(raw[:8])
            raise error("synthetic interruption")
        def __exit__(self, *args): self.stream.close()
    def fail(path, mode="r", *a, **k):
        stream = original(path, mode, *a, **k)
        return FailedWrite(stream) if path == output / "verification.json" and mode == "xb" else stream
    monkeypatch.setattr(Path, "open", fail)
    with pytest.raises(error): run()
    assert not (output / "verification.json").exists()
    assert (output / "source/scripts/replay.py").is_file()


def test_bundle_replay_competing_report_is_not_deleted(package_replay, monkeypatch):
    _, output, _, _, run = package_replay
    original = Path.open
    def race(path, mode="r", *a, **k):
        if path == output / "verification.json" and mode == "xb":
            with original(path, "wb") as stream: stream.write(b"rival proof")
        return original(path, mode, *a, **k)
    monkeypatch.setattr(Path, "open", race)
    with pytest.raises(FileExistsError): run()
    assert (output / "verification.json").read_bytes() == b"rival proof"


def test_assembly_competing_destination_is_not_replaced(tmp_path, monkeypatch):
    inputs = _inputs(tmp_path)
    identity = uuid4()
    target = tmp_path / str(identity)
    original = Path.mkdir
    def race(path, *a, **k):
        if path == target:
            original(path)
            (path / "proof.json").write_bytes(b"rival package")
        return original(path, *a, **k)
    monkeypatch.setattr(Path, "mkdir", race)
    with pytest.raises(FileExistsError):
        assemble_r06_bundle(tmp_path, identity, *inputs, assembly_revision="a"*40)
    assert (target / "proof.json").read_bytes() == b"rival package"


@pytest.mark.parametrize("collection", ["history", "hidden_items", "favorite_items", "eligible_items"])
def test_domain_freezes_mutable_inputs_before_bundle(package, collection):
    bundle = _load(package)
    context = _request(bundle.adapter).context
    before = bundle.score(context, 11, {"a"}, _records(bundle, context.catalog.eligible_items))
    if collection == "eligible_items":
        mutable = set(context.catalog.eligible_items)
        context = replace(context, catalog=replace(context.catalog, eligible_items=mutable))
        mutable.clear()
    else:
        mutable = list(getattr(context.session, collection))
        context = replace(context, session=replace(context.session, **{collection: mutable}))
        mutable.append("changed-input")
    assert bundle.score(context, 11, {"a"}, _records(bundle, context.catalog.eligible_items)) == before


@pytest.mark.parametrize("area", ["catalog", "metadata"])
def test_semantic_source_mismatch_even_with_consistent_declared_hashes(package, area):
    root, target, _ = package
    path = target / (area + ".json")
    doc = json.loads(path.read_bytes())
    if area == "catalog": doc["a"] = 2
    else: doc["new-item"] = "not in frozen mapping"
    path.write_text(json.dumps(doc), encoding="utf-8")
    # Repin both outer record and component provenance: semantic checks still run.
    if area == "catalog":
        fp = target / "features/manifest.json"
        fm = json.loads(fp.read_bytes())
        fm["provenance"]["catalog_sha256"] = _sha(path)
        _save(fp.parent, fm)
        rp = target / "retrieval/manifest.json"
        rm = json.loads(rp.read_bytes())
        rm["provenance"].update(catalog_sha256=_sha(path), features_manifest_sha256=_sha(fp))
        _save(rp.parent, rm)
    else:
        ep = target / "encoder/manifest.json"
        em = json.loads(ep.read_bytes())
        em["provenance"]["metadata_sha256"] = _sha(path)
        _save(ep.parent, em)
    def change(m):
        m[area].update(size_bytes=path.stat().st_size, sha256=_sha(path))
        m["components"] = {role: _sha(target / role / "manifest.json") for role in module.FILES}
    with pytest.raises(ControlledLoadError) as error:
        _load(_outer_change(package, change))
    assert error.value.code == "catalog_changed"


@pytest.mark.parametrize("boundary", ["directory", "file"])
def test_link_guards_before_component_load(package, monkeypatch, boundary):
    original = module._link
    path = package[1] / ("features" if boundary == "directory" else "features/vectors.f32")
    monkeypatch.setattr(module, "_link", lambda p: p == path or original(p))
    with pytest.raises(ControlledLoadError) as error:
        _load(package)
    assert error.value.code == "unsafe_path"


def test_assemble_cli_refuses_dirty_before_writing(tmp_path, monkeypatch, capsys):
    from scripts import assemble_r06_bundle as script
    source = tmp_path / "project/scripts/assemble.py"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"fixture")
    monkeypatch.setattr(script, "__file__", str(source))
    def dirty(project): raise ControlledLoadError("source_dirty", "dirty")
    monkeypatch.setattr(script, "_source", dirty)
    root = tmp_path / "project/artifacts/bundles"
    args = [str(root), "--bundle-id", str(uuid4()), "--catalog", "unused", "--metadata", "unused"]
    for role in module.FILES: args.extend([f"--{role}-component", "unused", f"--expected-{role}-manifest-sha256", "a"*64])
    assert script.main(args) == 1
    assert not root.exists()
    assert json.loads(capsys.readouterr().out)["code"] == "source_dirty"


@pytest.mark.parametrize("fault", ["dirty_after", "commit_changed", "interrupt_after", "rival_manifest"])
def test_assembly_cli_withdraws_only_own_marker_on_postcheck_failure(tmp_path, monkeypatch, capsys, fault):
    from scripts import assemble_r06_bundle as script
    project = tmp_path / "project"
    source = project / "scripts/assemble.py"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"fixture")
    monkeypatch.setattr(script, "__file__", str(source))
    inputs_root = project / "inputs"
    inputs_root.mkdir()
    dirs, hashes, catalog, metadata = _inputs(inputs_root)
    root, identity = project / "artifacts/bundles", uuid4()
    target = root / str(identity) / "manifest.json"
    calls = []
    def provenance(project):
        calls.append(1)
        if len(calls) == 1: return "a"*40
        if fault == "interrupt_after": raise KeyboardInterrupt("synthetic")
        if fault == "rival_manifest": target.write_bytes(b"rival proof")
        if fault == "commit_changed": return "b"*40
        raise ControlledLoadError("source_dirty", "synthetic dirty after")
    monkeypatch.setattr(script, "_source", provenance)
    args = [str(root), "--bundle-id", str(identity), "--catalog", str(catalog), "--metadata", str(metadata)]
    for role in module.FILES:
        args.extend([f"--{role}-component", str(dirs[role]), f"--expected-{role}-manifest-sha256", hashes[role]])
    if fault == "interrupt_after":
        with pytest.raises(KeyboardInterrupt): script.main(args)
    else:
        assert script.main(args) == 1
        assert json.loads(capsys.readouterr().out)["status"] == "failed"
    assert (target.read_bytes() == b"rival proof") if fault == "rival_manifest" else not target.exists()
