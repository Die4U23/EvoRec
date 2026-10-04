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
                                "retrieval", "r"*64, "ranker", "k"*64, rounds=kwargs.get("rounds", 1))
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
