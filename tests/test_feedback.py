from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from evorec.domain.models import FeedbackCommand


def command(observed_at: datetime) -> FeedbackCommand:
    return FeedbackCommand(
        event_id=uuid4(),
        session_id=uuid4(),
        session_token="token",
        request_id=uuid4(),
        item_id="demo-coop",
        kind="exposure",
        observed_at=observed_at,
        visible_ratio=0.5,
        visible_duration_ms=1000,
    )


def test_feedback_hash_normalizes_equivalent_timezone_representations():
    instant = datetime(2026, 9, 23, 8, 30, tzinfo=timezone.utc)
    original = command(instant)
    shifted = replace(
        original,
        observed_at=instant.astimezone(timezone(timedelta(hours=8))),
    )
    assert original.payload_sha256 == shifted.payload_sha256


def test_feedback_hash_changes_with_semantic_content():
    original = command(datetime(2026, 9, 23, 8, 30, tzinfo=timezone.utc))
    changed = replace(original, visible_duration_ms=1001)
    assert original.payload_sha256 != changed.payload_sha256
