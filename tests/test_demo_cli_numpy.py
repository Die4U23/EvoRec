"""Actual controlled NumPy files; the DB probe is stubbed, not a TCP test."""
from uuid import UUID, uuid4

import numpy
import pytest

from scripts import demo
from test_r06_bundle import _build


@pytest.mark.parametrize("change", ["valid", "wrong_approval", "component_corrupt"])
def test_actual_numpy_bundle_preflight(tmp_path, monkeypatch, change):
    assert numpy.__version__ == demo.NUMPY_VERSION
    root, target, digest = _build(tmp_path)
    if change == "component_corrupt":
        (target / "ranker/weights.f32").write_bytes(b"corrupt")
    db_calls = []
    monkeypatch.setattr(demo, "check_database", lambda _: db_calls.append(True) or {"read_only": True})
    settings = demo.Settings("r06", demo.PROJECT / "artifacts/test-demo-cli-numpy" / uuid4().hex, 0, "host=127.0.0.1", "process",
        demo.Approval(root, UUID(target.name), "a"*64 if change == "wrong_approval" else digest))
    if change != "valid":
        from evorec.infrastructure.residual_ranker import ControlledLoadError
        with pytest.raises(ControlledLoadError):
            demo.preflight(settings)
        assert not db_calls
    else:
        report = demo.preflight(settings)
        assert report["model"]["backend"] == "numpy" and report["model"]["item_count"] == 6
        assert report["model"]["manifest_sha256"] == digest and db_calls == [True]
        assert report["runtime_started"] is report["acceptance"] is False
    assert not settings.output.exists()


def test_numpy_missing_binary_or_wrong_version_fail_closed(monkeypatch):
    def absent(_): raise ImportError("PRIVATE")
    monkeypatch.setattr(demo.importlib, "import_module", absent)
    with pytest.raises(demo.PreflightFailure) as error:
        demo.check_packages("r06")
    assert error.value.code == "dependencies_missing" and error.value.missing == ("numpy",)
    monkeypatch.setattr(demo.importlib, "import_module", lambda _: type("Numpy", (), {"__version__": "0.0"}))
    with pytest.raises(demo.PreflightFailure, match="numpy_version_unsupported"):
        demo.check_packages("r06")
