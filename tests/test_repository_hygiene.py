"""Repository delivery checks must not rely on ignored working-tree files."""
from pathlib import Path

import pytest
from scripts.check_repository_hygiene import check


def test_valid_index_with_relative_and_external_links():
    result = check({"README.md": b"[guide](docs/guide.md) [web](https://example.org)",
                    "docs/guide.md": b"[root](../README.md)\n", "src/example.py": b"x = 1\n"})
    assert result["local_links_checked"] == 2
    assert result["python_files_parsed"] == 1


@pytest.mark.parametrize("name", ["docs/blog/article.md", "docs/reviews/internship.md",
    "datasets/events.csv", "artifacts/run/results.json", "tmp/log.txt", ".env", "models/best.pt"])
def test_local_and_raw_materials_are_rejected(name):
    with pytest.raises(ValueError):
        check({name: b"local material"})


def test_ignored_local_presence_does_not_satisfy_link(tmp_path):
    path = tmp_path / "private.md"
    path.write_text("present locally")
    with pytest.raises(ValueError, match="link missing from Git index"):
        check({"README.md": b"[private](private.md)"})


def test_common_token_signature_is_rejected():
    with pytest.raises(ValueError, match="possible credential"):
        check({"config.txt": ("ghp_"+"x"*36).encode()})


def test_oversized_file_and_invalid_python_are_rejected():
    with pytest.raises(ValueError, match="10 MiB"):
        check({"oversized.bin": b"x"*(10*1024*1024+1)})
    with pytest.raises(ValueError, match="invalid Python"):
        check({"src/broken.py": b"def x(:\n"})


def test_html_assets_must_be_in_index():
    with pytest.raises(ValueError, match="link missing"):
        check({"report.html": b'<img src="missing.png" alt="plot">'})


def test_service_ci_runs_durable_file_job_regressions():
    workflow = (Path(__file__).resolve().parents[1] / ".github" / "workflows"
                / "service-integration.yml").read_text(encoding="utf-8")
    service_step = workflow.split("- name: Run service tests against PostgreSQL", 1)[1].split(
        "- name: Run local page interaction tests", 1,
    )[0]
    assert "tests/test_catalog_file_jobs.py" in service_step
    assert "tests/test_backup_restore.py" in service_step
    assert service_step.count("tests/test_strategy_comparison.py") == 1
    assert service_step.count('tests/test_comparison_jobs.py') == 1
    assert service_step.count('tests/test_r06_features_runtime.py') == 1
    assert service_step.count('tests/test_content_encoder_runtime.py') == 1
    assert service_step.count('tests/test_r06_retrieval_runtime.py') == 1


def test_optional_retrieval_ci_remains_separate_from_pure_service():
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/service-integration.yml").read_text(encoding="utf-8")
    service, accelerated = workflow.split("  retrieval-accelerated:", 1)
    assert "requirements-retrieval.lock.txt" not in service
    assert "tests/test_r06_retrieval_numpy.py" not in service
    assert "requirements-retrieval.lock.txt" in accelerated
    assert "tests/test_r06_retrieval_numpy.py" in accelerated
    assert "tests/test_r06_retrieval_benchmark.py" in accelerated
    assert '"errors", "failures", "skipped"' in accelerated
    assert "numpy" not in (root / "requirements-dev.lock.txt").read_text(encoding="utf-8").lower()
