"""Strict readers for the official rating-only CSV and global time splits."""

import csv
from dataclasses import dataclass
from math import isfinite
from pathlib import Path

FIELDS = ("user_id", "parent_asin", "rating", "timestamp")


@dataclass(frozen=True, slots=True)
class Event:
    user_id: str
    item_id: str
    rating: float
    timestamp_ms: int


def parse_row(row: dict[str, str]) -> Event:
    if set(row) != set(FIELDS):
        raise ValueError("expected exactly user_id,parent_asin,rating,timestamp")
    user, item = row["user_id"].strip(), row["parent_asin"].strip()
    rating = float(row["rating"])
    timestamp = int(row["timestamp"])
    if not user or not item or len(user) > 128 or len(item) > 128:
        raise ValueError("invalid user or item ID")
    if not isfinite(rating) or not 1 <= rating <= 5:
        raise ValueError("rating must be finite and between 1 and 5")
    # Fail on seconds accidentally interpreted as milliseconds.
    if not 788918400000 <= timestamp < 4102444800000:
        raise ValueError("timestamp must be epoch milliseconds between 1995 and 2100")
    return Event(user, item, rating, timestamp)


def load_events(path: Path) -> tuple[list[Event], dict]:
    earliest: dict[tuple[str, str], Event] = {}
    input_rows = 0
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != list(FIELDS):
            raise ValueError("CSV header differs from the official rating-only schema")
        for line, row in enumerate(reader, 2):
            try:
                event = parse_row(row)
            except (ValueError, TypeError, AttributeError) as error:
                raise ValueError(f"invalid CSV row {line}: {error}") from error
            input_rows += 1
            key = (event.user_id, event.item_id)
            previous = earliest.get(key)
            # Preserve the first observation, never replace it with a future rating.
            if previous is None or event.timestamp_ms < previous.timestamp_ms:
                earliest[key] = event
    events = sorted(earliest.values(), key=lambda e: (e.timestamp_ms, e.user_id, e.item_id))
    if not events:
        raise ValueError("dataset is empty")
    return events, {
        "input_rows": input_rows, "deduplicated_rows": len(events),
        "duplicates_removed": input_rows - len(events),
        "users": len({e.user_id for e in events}),
        "items": len({e.item_id for e in events}),
        "first_timestamp_ms": events[0].timestamp_ms,
        "last_timestamp_ms": events[-1].timestamp_ms,
    }


def split_name(timestamp_ms: int, train_end_ms: int, validation_end_ms: int) -> str:
    if train_end_ms >= validation_end_ms:
        raise ValueError("train end must precede validation end")
    if timestamp_ms < train_end_ms:
        return "train"
    return "validation" if timestamp_ms < validation_end_ms else "test"
