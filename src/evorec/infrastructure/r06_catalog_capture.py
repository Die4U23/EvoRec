"""Single-view actual catalog fingerprints; never stored or client digests.

Frames use a UTF-8 byte length followed by the ID and a signed big-endian
index. Active frames additionally contain the actual text SHA-256 and literal
time. Counts and NULL guards accompany hashes (SHA-256 collision resistance).
The unchanged persisted catalog seal is separate from these read fingerprints.
"""

from dataclasses import dataclass
import hashlib
import struct

from psycopg.rows import dict_row

from evorec.domain.errors import ManagementError


@dataclass(frozen=True)
class CatalogCapture:
    member_count: int
    member_sha256: bytes
    active_count: int
    active_sha256: bytes
    invalid_active_count: int
    inactive_ids: tuple[str, ...]


def member_frame(item_id, index):
    raw = item_id.encode("utf-8")
    return struct.pack("!I", len(raw)) + raw + struct.pack("!q", index)


def catalog_source_digests(ordered_ids, catalog_items, text_digests):
    members, active = hashlib.sha256(), hashlib.sha256()
    for index, item_id in enumerate(ordered_ids):
        frame = member_frame(item_id, index)
        members.update(frame)
        active.update(frame + text_digests[item_id]
                      + struct.pack("!q", catalog_items[item_id].first_seen_ms))
    return members.digest(), active.digest()


CATALOG_CAPTURE_SQL = """
SELECT count(*) AS member_count,
       pg_catalog.sha256(COALESCE(pg_catalog.string_agg(member_frame, ''::bytea
           ORDER BY internal_item_id), ''::bytea)) AS member_sha256,
       count(*) FILTER (WHERE is_active) AS active_count,
       pg_catalog.sha256(COALESCE(pg_catalog.string_agg(
           member_frame || pg_catalog.sha256(pg_catalog.convert_to(r06_model_text, 'UTF8'))
                        || pg_catalog.int8send(r06_first_seen_ms)
           , ''::bytea ORDER BY internal_item_id) FILTER (WHERE is_active), ''::bytea)) AS active_sha256,
       count(*) FILTER (WHERE is_active AND
           (r06_model_text IS NULL OR r06_first_seen_ms IS NULL)) AS invalid_active_count,
       COALESCE(pg_catalog.array_agg(item_id ORDER BY internal_item_id)
           FILTER (WHERE NOT is_active), ARRAY[]::text[]) AS inactive_ids
FROM (
    SELECT bi.item_id, bi.internal_item_id, i.is_active,
           i.r06_model_text, i.r06_first_seen_ms,
           pg_catalog.int4send(pg_catalog.octet_length(pg_catalog.convert_to(bi.item_id, 'UTF8')))
           || pg_catalog.convert_to(bi.item_id, 'UTF8')
           || pg_catalog.int8send(bi.internal_item_id::bigint) AS member_frame
    FROM bundle_items bi JOIN items i ON i.item_id=bi.item_id
    WHERE bi.bundle_id=%s
) AS actual_rows
"""


def read_catalog_capture(connection, bundle_id):
    # All members, active content and exclusions use this statement's snapshot.
    # Only one summary is transferred; NULL content cannot vanish unnoticed.
    with connection.cursor(row_factory=dict_row, binary=True) as cursor:
        row = cursor.execute(CATALOG_CAPTURE_SQL, (bundle_id,)).fetchone()
    row["inactive_ids"] = tuple(row["inactive_ids"])
    return CatalogCapture(**row)


def capture_eligible(runtime, capture):
    if (type(capture) is not CatalogCapture or type(capture.member_count) is not int
            or capture.member_count != len(runtime.item_ids)
            or type(capture.member_sha256) is not bytes
            or capture.member_sha256 != runtime.member_sha256):
        raise ManagementError("bundle_members_changed", "approved ordered membership changed", 409)
    inactive = capture.inactive_ids
    if (type(capture.active_count) is not int or not 0 <= capture.active_count <= capture.member_count
            or type(capture.invalid_active_count) is not int or capture.invalid_active_count != 0
            or type(inactive) is not tuple or any(type(item) is not str for item in inactive)
            or len(set(inactive)) != len(inactive)
            or not set(inactive) <= runtime.full_eligible_items
            or len(inactive) + capture.active_count != capture.member_count
            or type(capture.active_sha256) is not bytes):
        raise ManagementError("r06_catalog_changed", "actual active catalog differs", 409)
    if not inactive:
        eligible, expected = runtime.full_eligible_items, runtime.full_active_sha256
    else:
        eligible = runtime.full_eligible_items.difference(inactive)
        digest = hashlib.sha256()
        for index, item_id in enumerate(runtime.item_ids):
            if item_id in eligible:
                digest.update(member_frame(item_id, index) + runtime.catalog_text_sha256[item_id]
                              + struct.pack("!q", runtime.catalog_items[item_id].first_seen_ms))
        expected = digest.digest()
    if capture.active_sha256 != expected:
        raise ManagementError("r06_catalog_changed", "actual model text/time changed", 409)
    return eligible
