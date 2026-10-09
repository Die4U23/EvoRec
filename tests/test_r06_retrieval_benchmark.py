"""Benchmark control-flow tests; the real pinned component run is the numerical oracle."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("numpy")

from scripts import benchmark_r06_retrieval as script


@pytest.fixture
def harness(tmp_path, monkeypatch):
    project = tmp_path / "project"
    source = project / "scripts" / "benchmark.py"
    source.parent.mkdir(parents=True)
    source.write_text("fixed source", encoding="utf-8")
    monkeypatch.setattr(script, "__file__", str(source))
    monkeypatch.setattr(script, "SOURCE_FILES", ("scripts/benchmark.py",))
    calls = []
    samples = [dict(history=["known"], seen=[], timestamp_ms=11, collaborative=["a"], content=["b"],
                    expected_items=["a", "b"], expected_context=[1., 0.], expected_scalars=[[0.]*6]*2),
               dict(history=[], seen=[], timestamp_ms=11, collaborative=["a"], content=["b"],
                    expected_items=["a", "b"], expected_context=[0., 0.], expected_scalars=[[0.]*6]*2)]
    references = [dict(expected_scores=[2., 1.], expected_top20=[0, 1]) for _ in samples]
    def pool(history, seen, timestamp, collaborative, content):
        calls.append("verify")
        sample = samples[0 if history else 1]
        return SimpleNamespace(item_ids=("a", "b"), context=sample["expected_context"],
                               scalars=sample["expected_scalars"], score=lambda ranker: [2., 1.])
    features = SimpleNamespace(build_pool=pool, manifest_sha256="f"*64, item_ids=("a", "b"), dimension=2)
    ranker = SimpleNamespace(manifest_sha256="k"*64)
    engines = {}
    for backend in ("stdlib", "numpy"):
        def retrieve(history, seen, timestamp, backend=backend):
            calls.append(backend)
            return SimpleNamespace(collaborative=("a",), content=("b",))
        engines[backend] = SimpleNamespace(retrieve=retrieve)
    monkeypatch.setattr(script, "load_r06_features", lambda *a, **k: features)
    monkeypatch.setattr(script, "load_residual_ranker", lambda *a, **k: ranker)
    monkeypatch.setattr(script, "load_r06_retrieval", lambda *a, **k: engines[k["content_backend"]])
    monkeypatch.setattr(script, "_validation", lambda root, digest: samples if root == "features" else references)
    monkeypatch.setattr(script.subprocess, "check_output", lambda argv, **k: "a"*40 if "rev-parse" in argv else "")
    ticks = iter(range(100))
    def clock():
        calls.append("clock")
        return next(ticks)
    monkeypatch.setattr(script.time, "perf_counter", clock)
    def run(**kwargs):
        return script.benchmark(project / "artifacts" / "comparison", "features", "f"*64,
                                "retrieval", "r"*64, "ranker", "k"*64, rounds=kwargs.get("rounds", 1),
                                compare_block_topk=kwargs.get("compare_block_topk", False),
                                compare_eligible_validation=kwargs.get("compare_eligible_validation", False))
    return SimpleNamespace(project=project, output=project / "artifacts" / "comparison", source=source,
                           calls=calls, samples=samples, references=references, features=features,
                           ranker=ranker, engines=engines, run=run)


def test_interleaved_timing_snapshot_and_report(harness):
    result = harness.run(rounds=2)
    assert result["status"] == "passed" and len(result["measurements"]) == 16
    order = [m["backend"] for m in result["measurements"]]
    abba, baab = ["stdlib", "numpy", "numpy", "stdlib"], ["numpy", "stdlib", "stdlib", "numpy"]
    assert order == abba + baab + baab + abba
    for i in range(0, len(harness.calls), 4):
        assert harness.calls[i:i+4] == ["clock", order[i//4], "clock", "verify"]
    assert result["effective_history_median_seconds"] == {"stdlib": 1., "numpy": 1.}
    assert result["source"]["working_tree_dirty"] is False
    assert result["source"]["source_sha256"]["scripts/benchmark.py"] == hashlib.sha256(b"fixed source").hexdigest()
    assert (harness.output / "source/scripts/benchmark.py").read_bytes() == b"fixed source"
    assert json.loads((harness.output / "verification.json").read_text()) == result
    assert not result["activated"] and not result["test_queries_evaluated"]


def test_block_topk_comparison_is_explicit_and_interleaved(harness, monkeypatch):
    replaced = []
    def reference(engine, **kwargs):
        assert engine is harness.engines["numpy"]
        replaced.append(kwargs)
        return harness.engines["stdlib"]
    monkeypatch.setattr(script, "replace", reference)
    result = harness.run(compare_block_topk=True)
    assert replaced == [{"_content_scanner": script._full_numpy_scan}]
    assert result["comparison"] == "numpy-full-stream-vs-block-top200"
    assert "not a historical process run" in result["reference_scope"]
    assert [m["backend"] for m in result["measurements"]] == [
        "numpy_full_scan", "numpy_block_topk", "numpy_block_topk", "numpy_full_scan",
        "numpy_block_topk", "numpy_full_scan", "numpy_full_scan", "numpy_block_topk"]
    assert result["effective_history_median_seconds"] == {"numpy_full_scan": 1., "numpy_block_topk": 1.}


@pytest.fixture
def eligible_harness(harness, monkeypatch):
    from evorec.infrastructure import r06_features
    harness.features._indices = {"a": 0, "b": 1}
    runtime = harness.engines["numpy"]
    runtime._features = harness.features
    runtime.manifest_sha256 = "r" * 64
    runtime._eligible = lambda values: values
    retrieve = runtime.retrieve
    runtime.retrieve = lambda *args, **kwargs: retrieve(*args)
    monkeypatch.setattr(r06_features, "_validate_id_values", lambda values: None, raising=False)
    return harness


def test_eligibility_comparison_is_separate_interleaved_and_full_replay_checked(eligible_harness):
    h = eligible_harness
    result = h.run(compare_eligible_validation=True)
    assert result["comparison"] == "converted-vs-immutable-eligibility-validation"
    assert "not admission" in result["scope"] and "not a historical" in result["reference_scope"]
    assert result["approved_reference_rows"] == 2 and result["all_full_provider_orders_exact"] and result["top20_exact"]
    assert len(result["measurements"]) == 24 and len(result["rows"]) == 6
    assert [m["mode"] for m in result["measurements"]] == ["converted", "immutable", "immutable", "converted"] * 6
    assert {row["stage"] for row in result["rows"]} == {"capture_id_validation", "retrieval_eligibility"}
    assert {row["case"] for row in result["rows"]} == {"full", "sparse", "empty"}
    assert all(row["median_seconds"] == {"converted": 1., "immutable": 1.} for row in result["rows"])
    assert all(set(row["traced_peak_bytes"]) == {"converted", "immutable"} for row in result["rows"])
    assert result["source"]["working_tree_dirty"] is False
    assert json.loads((h.output / "verification.json").read_text()) == result
    assert not result["activated"] and not result["retrained"] and not result["test_queries_evaluated"]
    assert not script.tracemalloc.is_tracing()


@pytest.mark.parametrize("failure", ["identity", "clock", "source", "revision"])
def test_eligibility_comparison_failure_never_issues_passed_report(eligible_harness, monkeypatch, failure):
    h = eligible_harness
    if failure == "identity":
        h.engines["numpy"]._eligible = lambda values: frozenset(tuple(values))
    elif failure == "clock":
        monkeypatch.setattr(script.time, "perf_counter", lambda: 1.)
    elif failure == "source":
        def changed(values):
            h.source.write_text("changed source")
            return values
        h.engines["numpy"]._eligible = changed
    else:
        revisions = iter(["a" * 40, "b" * 40])
        monkeypatch.setattr(script.subprocess, "check_output", lambda args, **kwargs: next(revisions) if "rev-parse" in args else "")
    with pytest.raises(ValueError):
        h.run(compare_eligible_validation=True)
    assert not (h.output / "verification.json").exists()
    assert not script.tracemalloc.is_tracing()


def test_eligibility_comparison_rejects_dirty_source_before_artifacts(eligible_harness, monkeypatch):
    monkeypatch.setattr(script.subprocess, "check_output", lambda *args, **kwargs: " M source.py")
    with pytest.raises(ValueError, match="clean source"):
        eligible_harness.run(compare_eligible_validation=True)
    assert not eligible_harness.output.exists()


def test_eligibility_comparison_preserves_external_allocation_observer(eligible_harness):
    script.tracemalloc.start()
    try:
        with pytest.raises(ValueError, match="inactive"):
            eligible_harness.run(compare_eligible_validation=True)
        assert script.tracemalloc.is_tracing() and not eligible_harness.output.exists()
    finally:
        script.tracemalloc.stop()


@pytest.mark.parametrize("mode", [1, None, "true"])
def test_eligibility_comparison_mode_is_boolean(harness, mode):
    with pytest.raises(ValueError, match="boolean"):
        harness.run(compare_eligible_validation=mode)
    assert not harness.output.exists()


def test_comparison_modes_are_mutually_exclusive(harness):
    with pytest.raises(ValueError, match="mutually exclusive"):
        harness.run(compare_block_topk=True, compare_eligible_validation=True)
    assert not harness.output.exists()


@pytest.mark.parametrize("mode", [1, None, "true"])
def test_comparison_mode_rejects_non_boolean_before_output(harness, mode):
    with pytest.raises(ValueError, match="boolean"):
        harness.run(compare_block_topk=mode)
    assert not harness.output.exists() and not harness.calls


@pytest.mark.parametrize("enabled", [False, True])
def test_cli_block_topk_mode_requires_explicit_flag(monkeypatch, capsys, enabled):
    calls = []
    def benchmark(*args, **kwargs):
        calls.append(kwargs)
        return dict(status="passed", effective_history_median_seconds=None,
                    effective_history_speedup=None)
    monkeypatch.setattr(script, "benchmark", benchmark)
    argv = ["output", "--features-component", "features", "--expected-features-manifest-sha256", "f"*64,
            "--retrieval-component", "retrieval", "--expected-retrieval-manifest-sha256", "r"*64,
            "--ranker-component", "ranker", "--expected-ranker-manifest-sha256", "k"*64]
    if enabled:
        argv.append("--compare-block-topk")
    assert script.main(argv) == 0
    assert calls == [dict(rounds=1, compare_block_topk=enabled, compare_eligible_validation=False)]
    assert json.loads(capsys.readouterr().out)["activated"] is False


def test_cli_eligibility_comparison_requires_explicit_flag(monkeypatch, capsys):
    calls = []
    def benchmark(*args, **kwargs):
        calls.append(kwargs)
        return dict(status="passed", comparison="converted-vs-immutable-eligibility-validation",
                    scope="not HTTP timing", rows=[], activated=False)
    monkeypatch.setattr(script, "benchmark", benchmark)
    argv = ["output", "--features-component", "features", "--expected-features-manifest-sha256", "f"*64,
            "--retrieval-component", "retrieval", "--expected-retrieval-manifest-sha256", "r"*64,
            "--ranker-component", "ranker", "--expected-ranker-manifest-sha256", "k"*64,
            "--compare-eligible-validation"]
    assert script.main(argv) == 0
    assert calls == [dict(rounds=1, compare_block_topk=False, compare_eligible_validation=True)]
    assert json.loads(capsys.readouterr().out)["comparison"] == "converted-vs-immutable-eligibility-validation"


@pytest.mark.parametrize("rounds", [True, 0, 4, 1.5])
def test_invalid_rounds_do_not_create_output(harness, rounds):
    with pytest.raises(ValueError, match="rounds"):
        harness.run(rounds=rounds)
    assert not harness.output.exists() and not harness.calls


def test_existing_output_and_outside_artifacts_are_protected(harness):
    harness.output.mkdir(parents=True)
    sentinel = harness.output / "verification.json"
    sentinel.write_text("previous evidence")
    with pytest.raises(FileExistsError):
        harness.run()
    assert sentinel.read_text() == "previous evidence" and not harness.calls
    with pytest.raises(ValueError, match="subdirectory"):
        script.benchmark(harness.project / "outside", "features", "f"*64, "retrieval", "r"*64, "ranker", "k"*64)


@pytest.mark.parametrize("failure", ["provider", "score", "top20", "clock", "source"])
def test_failed_check_never_issues_a_passed_report(harness, monkeypatch, failure):
    if failure == "provider":
        harness.engines["numpy"].retrieve = lambda *args: SimpleNamespace(collaborative=("a",), content=("wrong",))
    elif failure == "score":
        harness.references[0]["expected_scores"] = [1., 2.]
    elif failure == "top20":
        harness.references[0]["expected_top20"] = [1, 0]
    elif failure == "clock":
        monkeypatch.setattr(script.time, "perf_counter", lambda: 1.)
    else:
        original = harness.engines["numpy"].retrieve
        def mutate(*args):
            harness.source.write_text("changed source")
            return original(*args)
        harness.engines["numpy"].retrieve = mutate
    with pytest.raises(ValueError):
        harness.run()
    assert not (harness.output / "verification.json").exists()
    assert (harness.output / "source/scripts/benchmark.py").exists()


def test_nonfinite_score_is_rejected(harness):
    pool = SimpleNamespace(item_ids=("a", "b"), context=[1., 0.], scalars=[[0.]*6]*2,
                           score=lambda ranker: [float("nan"), 1.])
    harness.features.build_pool = lambda *args: pool
    with pytest.raises(ValueError, match="non-finite"):
        harness.run()


def test_cold_only_history_classification(harness):
    for sample in harness.samples:
        sample["expected_context"] = [0., 0.]
    result = harness.run()
    assert result["effective_history_median_seconds"] is None and result["effective_history_speedup"] is None


def test_history_classification_uses_norm_not_largest_coordinate(harness):
    harness.samples[0]["expected_context"] = [8e-9, 8e-9]
    assert harness.run()["rows"][0]["effective_history"] is True


def test_partial_report_is_revoked_on_write_failure(harness, monkeypatch):
    original = Path.open
    class BrokenStream:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.stream.close()
        def write(self, raw):
            self.stream.write(b"partial")
            self.stream.flush()
            raise OSError("disk full")
    def opened(path, *args, **kwargs):
        stream = original(path, *args, **kwargs)
        return BrokenStream(stream) if path.name == "verification.json" and args == ("xb",) else stream
    monkeypatch.setattr(Path, "open", opened)
    with pytest.raises(OSError, match="disk full"):
        harness.run()
    assert not (harness.output / "verification.json").exists()


def test_report_creation_race_preserves_other_evidence(harness, monkeypatch):
    original = Path.open
    def opened(path, *args, **kwargs):
        if path.name == "verification.json" and args == ("xb",):
            with original(path, "w") as stream:
                stream.write("other evidence")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", opened)
    with pytest.raises(FileExistsError):
        harness.run()
    assert (harness.output / "verification.json").read_text() == "other evidence"
