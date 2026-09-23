"""Exercise M11 constraints against a real PostgreSQL database, then clean up."""

import hashlib
import json
import os
from uuid import uuid4

import psycopg
from psycopg.errors import CheckViolation, LockNotAvailable, UniqueViolation


def verify(database_url: str) -> dict[str, object]:
    bundle_id = uuid4()
    session_id = uuid4()
    request_id = uuid4()
    item_id = f"verify-{uuid4().hex}"
    report: dict[str, object] = {}

    with psycopg.connect(database_url) as connection:
        connection.execute(
            "INSERT INTO bundle_versions (bundle_id, status, artifact_path) VALUES (%s, 'building', %s)",
            (bundle_id, f"verification/{bundle_id}"),
        )
        connection.execute(
            "INSERT INTO items (item_id, title, category) VALUES (%s, 'M11 verification', 'test')",
            (item_id,),
        )
        connection.execute(
            "INSERT INTO bundle_items (bundle_id, item_id, internal_item_id) VALUES (%s, %s, 0)",
            (bundle_id, item_id),
        )
        connection.execute(
            """
            INSERT INTO sessions (session_id, owner_token_sha256)
            VALUES (%s, %s)
            """,
            (session_id, hashlib.sha256(session_id.bytes).hexdigest()),
        )
        reset = connection.execute(
            """
            UPDATE sessions
            SET epoch = epoch + 1, history_version = history_version + 1,
                history = '[]'::jsonb, hidden_items = '[]'::jsonb,
                favorite_items = '[]'::jsonb, updated_at = now()
            WHERE session_id = %s AND history_version = 0
            RETURNING epoch, history_version
            """,
            (session_id,),
        ).fetchone()
        stale = connection.execute(
            """
            UPDATE sessions SET history_version = history_version + 1
            WHERE session_id = %s AND history_version = 0
            RETURNING history_version
            """,
            (session_id,),
        ).fetchone()
        report["reset_versions"] = list(reset or ())
        report["stale_version_rejected"] = stale is None

        connection.execute(
            """
            INSERT INTO recommendation_requests (
                request_id, session_id, session_epoch, history_version,
                history_snapshot, hidden_snapshot, bundle_id, exclusion_version,
                requested_strategy, requested_k, status
            ) VALUES (%s, %s, 1, 1, '[]'::jsonb, '[]'::jsonb, %s, 0, 'popular', 1, 'accepted')
            """,
            (request_id, session_id, bundle_id),
        )
        try:
            with connection.transaction():
                connection.execute(
                    """
                    INSERT INTO recommendation_requests (
                        request_id, session_id, session_epoch, history_version,
                        history_snapshot, hidden_snapshot, bundle_id, exclusion_version,
                        requested_strategy, requested_k, status
                    ) VALUES (%s, %s, 1, 1, '[]'::jsonb, '[]'::jsonb, %s, 0, 'popular', 1, 'accepted')
                    """,
                    (request_id, session_id, bundle_id),
                )
        except UniqueViolation:
            report["duplicate_request_rejected"] = True
        else:
            raise AssertionError("duplicate request_id was accepted")

        try:
            with connection.transaction():
                connection.execute(
                    """
                    INSERT INTO request_items (request_id, item_id, position, score, source)
                    VALUES (%s, %s, 1, 'NaN'::double precision, 'verification')
                    """,
                    (request_id, item_id),
                )
        except CheckViolation:
            report["non_finite_score_rejected"] = True
        else:
            raise AssertionError("non-finite score was accepted")
        connection.commit()

    first = psycopg.connect(database_url)
    second = psycopg.connect(database_url)
    try:
        first.execute("SELECT session_id FROM sessions WHERE session_id = %s FOR UPDATE", (session_id,))
        try:
            with second.transaction():
                second.execute("SET LOCAL lock_timeout = '250ms'")
                second.execute(
                    "UPDATE sessions SET updated_at = now() WHERE session_id = %s",
                    (session_id,),
                )
        except LockNotAvailable:
            report["session_row_lock_verified"] = True
        else:
            raise AssertionError("concurrent session update did not wait for the row lock")
    finally:
        first.rollback()
        second.rollback()
        first.close()
        second.close()

    with psycopg.connect(database_url) as cleanup:
        cleanup.execute("DELETE FROM recommendation_requests WHERE request_id = %s", (request_id,))
        cleanup.execute("DELETE FROM sessions WHERE session_id = %s", (session_id,))
        cleanup.execute("DELETE FROM bundle_items WHERE bundle_id = %s", (bundle_id,))
        cleanup.execute("DELETE FROM items WHERE item_id = %s", (item_id,))
        cleanup.execute("DELETE FROM bundle_versions WHERE bundle_id = %s", (bundle_id,))

    report["temporary_rows_cleaned"] = True
    return report


def main() -> None:
    database_url = os.getenv("EVOREC_DATABASE_URL")
    if not database_url:
        raise SystemExit("EVOREC_DATABASE_URL is required")
    print(json.dumps(verify(database_url), ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
