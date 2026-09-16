import pytest
from evorec.research.content_data import metadata_text, selected_bucket
from evorec.research.sample import selected_user


def test_metadata_allowlist():
    row = {"title": "<b>Space &amp; Race</b>", "categories": ["Games", ["Racing"]],
           "average_rating": 5, "rating_number": 99999, "description": "leaking",
           "text": "future", "parent_asin": "secret", "bought_together": ["secret"]}
    assert metadata_text(row) == "Space & Race Games Racing"
    assert metadata_text({"parent_asin": "secret", "average_rating": 5}) == ""


def test_metadata_missing_and_bounded_fields():
    assert metadata_text({"title": None, "categories": {"unexpected": "value"}}) == ""
    assert len(metadata_text({"title": "a" * 5000})) == 4096


def test_user_buckets_are_disjoint():
    for i in range(5000):
        user = f"u-{i}"
        assert not (selected_bucket(user, set()) and selected_user(user, set()))
        assert not selected_bucket(user, {user})
    with pytest.raises(ValueError):
        selected_bucket("u", set(), bucket=20)
