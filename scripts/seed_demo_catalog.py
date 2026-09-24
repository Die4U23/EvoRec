"""Idempotently seed the fictional catalog used by the local demo."""

import hashlib
import json
import os
from uuid import NAMESPACE_URL, uuid5

import psycopg
from evorec.infrastructure.demo_catalog import DEMO_ITEMS


BUNDLE_ID = uuid5(NAMESPACE_URL, "https://evorec.local/bundles/m1-memory-demo")
def main() -> None:
    database_url = os.getenv("EVOREC_DATABASE_URL")
    if not database_url:
        raise SystemExit("EVOREC_DATABASE_URL is required")
    manifest = hashlib.sha256(json.dumps(DEMO_ITEMS, ensure_ascii=False).encode("utf-8")).hexdigest()
    with psycopg.connect(database_url) as connection:
        for item_id, title, category, description in DEMO_ITEMS:
            connection.execute(
                """
                INSERT INTO items (item_id, title, category, description)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (item_id) DO UPDATE
                SET title = EXCLUDED.title, category = EXCLUDED.category,
                    description = EXCLUDED.description, is_active = true,
                    updated_at = now()
                """,
                (item_id, title, category, description),
            )
        connection.execute(
            """
            INSERT INTO bundle_versions (bundle_id, status, artifact_path, manifest_sha256)
            VALUES (%s, 'ready', 'demo/m1', %s)
            ON CONFLICT (bundle_id) DO UPDATE
            SET artifact_path = EXCLUDED.artifact_path,
                manifest_sha256 = EXCLUDED.manifest_sha256
            """,
            (BUNDLE_ID, manifest),
        )
        for internal_id, (item_id, _, _, _) in enumerate(DEMO_ITEMS):
            connection.execute(
                """
                INSERT INTO bundle_items (bundle_id, item_id, internal_item_id)
                VALUES (%s, %s, %s)
                ON CONFLICT (bundle_id, item_id) DO UPDATE
                SET internal_item_id = EXCLUDED.internal_item_id
                """,
                (BUNDLE_ID, item_id, internal_id),
            )
        connection.execute(
            "UPDATE bundle_versions SET status = 'retired' WHERE status = 'active' AND bundle_id <> %s",
            (BUNDLE_ID,),
        )
        connection.execute(
            "UPDATE bundle_versions SET status = 'active' WHERE bundle_id = %s",
            (BUNDLE_ID,),
        )
        connection.execute(
            """
            UPDATE catalog_control
            SET active_bundle_id = %s, admission_open = true, updated_at = now()
            WHERE singleton = 1
            """,
            (BUNDLE_ID,),
        )
    print(f"seeded demo catalog bundle {BUNDLE_ID}")


if __name__ == "__main__":
    main()
