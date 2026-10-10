"""Independent framing and validation tests for R06 catalog fingerprints."""

from dataclasses import FrozenInstanceError
import hashlib
from types import SimpleNamespace

import pytest

from evorec.domain.errors import ManagementError
from evorec.infrastructure.r06_bundle import FrozenCatalogItem
from evorec.infrastructure.r06_catalog_capture import (
    CatalogCapture,
    catalog_source_digests,
    capture_eligible,
    member_frame,
)


def test_member_and_active_digest_frames_match_independent_utf8_encoding():
    ids = ("a", "中")
    records = {
        "a": FrozenCatalogItem("a", "alpha", 7),
        "中": FrozenCatalogItem("中", "中文", 258),
    }
    text_digests = {item: hashlib.sha256(record.text.encode("utf-8")).digest()
                    for item, record in records.items()}

    # Spell the wire bytes independently: UTF-8 byte lengths, then signed
    # eight-byte big-endian indices and timestamps.
    frame_a = b"\x00\x00\x00\x01a" + bytes(8)
    raw_chinese_id = "中".encode("utf-8")
    frame_chinese = len(raw_chinese_id).to_bytes(4, "big") + raw_chinese_id + (1).to_bytes(8, "big")
    expected_members = hashlib.sha256(frame_a + frame_chinese).digest()
    expected_active = hashlib.sha256(
        frame_a + text_digests["a"] + (7).to_bytes(8, "big", signed=True)
        + frame_chinese + text_digests["中"] + (258).to_bytes(8, "big", signed=True)
    ).digest()

    assert member_frame("中", 1) == frame_chinese
    assert catalog_source_digests(ids, records, text_digests) == (expected_members, expected_active)


def test_capture_is_frozen_and_validates_members_before_active_content():
    item = FrozenCatalogItem("a", "中文 alpha", 1)
    text_digest = hashlib.sha256(item.text.encode("utf-8")).digest()
    member = b"\x00\x00\x00\x01a" + bytes(8)
    active_digest = hashlib.sha256(member + text_digest + (1).to_bytes(8, "big", signed=True)).digest()
    runtime = SimpleNamespace(
        item_ids=("a",),
        catalog_items={"a": item},
        catalog_text_sha256={"a": text_digest},
        member_sha256=hashlib.sha256(member).digest(),
        full_active_sha256=active_digest,
        full_eligible_items=frozenset({"a"}),
    )
    capture = CatalogCapture(1, runtime.member_sha256, 1, active_digest, 0, ())

    assert capture_eligible(runtime, capture) == frozenset({"a"})
    with pytest.raises(FrozenInstanceError):
        capture.member_count = 0

    hostile = CatalogCapture(0, b"wrong", 0, b"wrong", 1, ())
    with pytest.raises(ManagementError) as error:
        capture_eligible(runtime, hostile)
    assert error.value.code == "bundle_members_changed"

    same_count_replacement = CatalogCapture(1, b"wrong-member-digest", 1, active_digest, 0, ())
    with pytest.raises(ManagementError) as error:
        capture_eligible(runtime, same_count_replacement)
    assert error.value.code == "bundle_members_changed"


@pytest.mark.parametrize("damage", ["active-digest", "invalid-count", "unknown-inactive", "duplicate-inactive"])
def test_capture_rejects_bad_active_digest_counts_and_inactive_partition(damage):
    item = FrozenCatalogItem("a", "alpha", 7)
    text_digest = hashlib.sha256(b"alpha").digest()
    member = b"\x00\x00\x00\x01a" + bytes(8)
    runtime = SimpleNamespace(
        item_ids=("a",),
        catalog_items={"a": item},
        catalog_text_sha256={"a": text_digest},
        member_sha256=hashlib.sha256(member).digest(),
        full_active_sha256=hashlib.sha256(member + text_digest + (7).to_bytes(8, "big", signed=True)).digest(),
        full_eligible_items=frozenset({"a"}),
    )
    valid = CatalogCapture(1, runtime.member_sha256, 1, runtime.full_active_sha256, 0, ())
    assert capture_eligible(runtime, valid) == frozenset({"a"})
    all_inactive = CatalogCapture(1, runtime.member_sha256, 0, hashlib.sha256(b"").digest(), 0, ("a",))
    assert capture_eligible(runtime, all_inactive) == frozenset()

    if damage == "active-digest":
        capture = CatalogCapture(1, runtime.member_sha256, 1, b"wrong-active", 0, ())
    elif damage == "invalid-count":
        capture = CatalogCapture(1, runtime.member_sha256, 1, runtime.full_active_sha256, 1, ())
    elif damage == "unknown-inactive":
        capture = CatalogCapture(1, runtime.member_sha256, 0, hashlib.sha256(b"").digest(), 0, ("unknown",))
    else:
        capture = CatalogCapture(1, runtime.member_sha256, 0, hashlib.sha256(b"").digest(), 0, ("a", "a"))
    with pytest.raises(ManagementError) as error:
        capture_eligible(runtime, capture)
    assert error.value.code == "r06_catalog_changed"
