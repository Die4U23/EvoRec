"""Repository delivery checks must not rely on ignored working-tree files."""
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
