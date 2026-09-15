from uuid import uuid4

import pytest
from pydantic import ValidationError

from evorec.contracts import CatalogImportInput, CatalogItem, FeedbackInput, RecommendationInput


def feedback(**changes):
    value = {
        "event_id": str(uuid4()),
        "session_id": str(uuid4()),
        "request_id": str(uuid4()),
        "item_id": "game-001",
        "kind": "detail_view",
        "observed_at": "2026-09-14T08:00:00Z",
    }
    return value | changes


def item(item_id="game-001"):
    return {"item_id": item_id, "title": "合作游戏", "category": "Video_Games"}


def test_duplicate_item_ids_reject_entire_batch():
    with pytest.raises(ValidationError, match="duplicate item_id"):
        CatalogImportInput(batch_id=uuid4(), items=[item(), item()])


def test_batch_limit_accepts_1000_and_rejects_1001():
    items = [item(f"game-{i}") for i in range(1000)]
    assert len(CatalogImportInput(batch_id=uuid4(), items=items).items) == 1000
    with pytest.raises(ValidationError):
        CatalogImportInput(batch_id=uuid4(), items=items + [item("overflow")])


@pytest.mark.parametrize("items", [[], [item() | {"category": "  "}]])
def test_empty_or_invalid_batch_cannot_pass_validation(items):
    with pytest.raises(ValidationError):
        CatalogImportInput(batch_id=uuid4(), items=items)


@pytest.mark.parametrize("k", [0, 51, True, "10"])
def test_result_count_is_a_bounded_integer(k):
    with pytest.raises(ValidationError):
        RecommendationInput(session_id=uuid4(), expected_history_version=0, k=k)


def test_state_feedback_requires_explicit_boolean_including_false():
    assert FeedbackInput(**feedback(kind="hide_set", desired_state=False)).desired_state is False
    for value in [None, "false", 0]:
        with pytest.raises(ValidationError):
            FeedbackInput(**feedback(kind="hide_set", desired_state=value))


@pytest.mark.parametrize("ratio,duration", [(0.49, 1000), (0.5, 999), (None, 1000), (0.5, None)])
def test_exposure_requires_both_visibility_and_duration(ratio, duration):
    with pytest.raises(ValidationError):
        FeedbackInput(**feedback(kind="exposure", visible_ratio=ratio, visible_duration_ms=duration))


def test_exposure_boundary_and_click_before_exposure_are_distinct():
    assert FeedbackInput(**feedback(kind="exposure", visible_ratio=0.5, visible_duration_ms=1000)).kind == "exposure"
    # A detail click is valid without exposure evidence; persistence must later backfill exposure.
    assert FeedbackInput(**feedback()).kind == "detail_view"


@pytest.mark.parametrize("changes", [
    {"observed_at": "2026-09-14T08:00:00"},
    {"kind": "detail_view", "desired_state": True},
    {"kind": "hide_set", "desired_state": True, "visible_ratio": 1.0},
    {"kind": "unknown"},
    {"server_received_at": "2026-09-14T08:00:00Z"},
])
def test_ambiguous_or_client_invented_event_fields_are_rejected(changes):
    with pytest.raises(ValidationError):
        FeedbackInput(**feedback(**changes))


def test_catalog_ids_are_stable_and_titles_cannot_be_blank():
    assert CatalogItem(**item("A-001")).item_id == "A-001"
    for invalid in [item(" A-001"), item() | {"title": " \t "}, item() | {"is_published": True}]:
        with pytest.raises(ValidationError):
            CatalogItem(**invalid)
