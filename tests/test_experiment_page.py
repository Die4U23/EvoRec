"""Public research display is read-only and never exposes arbitrary files."""
import hashlib
from pathlib import Path

import pytest
from test_api import request

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / "docs/experiments/r06-multi-interest"


def test_results_entry_is_linked_and_served_without_model_or_database():
    response = request("GET", "/app/results")
    assert response.status_code == 200
    assert response.content == (ROOT / "web/results.html").read_bytes()
    assert 'href="/app/results"' in request("GET", "/app").text
    assert "/app/results" not in request("GET", "/openapi.json").json()["paths"]


@pytest.mark.parametrize("asset", ["results.json", "uncertainty.json", "report.md", "report.html",
    "figures/manifest.json", "figures/coverage-and-ranking.png", "figures/learning-curves.png",
    "figures/paired-intervals.png", "protocol.md"])
def test_public_evidence_exact_bytes(asset):
    response = request("GET", "/app/evidence/r06/" + asset)
    expected = ROOT / "docs/experiments/r06-multi-interest-protocol.md" if asset == "protocol.md" else PUBLIC / asset
    assert response.status_code == 200
    assert response.content == expected.read_bytes()
    assert response.headers["cache-control"] == "no-cache"
    if asset.endswith(".json"):
        assert response.headers["content-type"].startswith("application/json")
        assert response.json()


@pytest.mark.parametrize("asset", [".env", "manifest.json", "../../../../.env",
    "%2e%2e%2f%2e%2e%2f.env", "report.md/../../LICENSE", "figures/other.png", "C:%5CWindows%5Cwin.ini"])
def test_non_allowlisted_files_are_not_exposed(asset):
    response = request("GET", "/app/evidence/r06/" + asset)
    assert response.status_code == 404


def test_page_pins_archived_public_bytes_not_new_experiment_metrics():
    page = (ROOT / "web/results.html").read_text(encoding="utf-8")
    for name in ("results.json", "uncertainty.json"):
        assert hashlib.sha256((PUBLIC / name).read_bytes()).hexdigest() in page
    assert "不是新的实验" in page
    assert "不包含训练随机性" in page
    assert "未做多重比较校正" in page
    assert "工作区 dirty" in page
