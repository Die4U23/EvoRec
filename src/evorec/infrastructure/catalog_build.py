"""Durable, explicitly retried local catalog builds with frozen input snapshots."""

from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from evorec.domain.errors import ManagementError
from evorec.infrastructure.catalog_builder import MAX_CATALOG_ITEMS, build_catalog_bundle


class CatalogBuildService:
    LOCK_NAME = "evorec:catalog-build"

    def __init__(self, manager):
        self.manager = manager

    def enqueue(self, batch_id: UUID, build_id: UUID) -> dict:
        """Freeze input before acknowledging a durable background build."""
        if self.manager.managed_root is None:
            raise ManagementError("bundle_root_not_configured", "managed bundle root is not configured", 503)
        with self.manager._connect() as connection:
            prior = connection.execute(
                "SELECT batch_id, auto_retry, status FROM catalog_builds WHERE build_id = %s FOR UPDATE",
                (build_id,),
            ).fetchone()
            if prior is not None:
                if prior["batch_id"] != batch_id or not prior["auto_retry"]:
                    raise ManagementError("build_conflict", "build ID belongs to another build request")
                if prior["status"] == "failed":
                    connection.execute(
                        "UPDATE catalog_builds SET status = 'queued', updated_at = now() "
                        "WHERE build_id = %s", (build_id,),
                    )
            else:
                base, snapshot = self._snapshot(connection, batch_id)
                inserted = connection.execute(
                    "INSERT INTO catalog_builds (build_id, batch_id, bundle_id, base_bundle_id, "
                    "status, snapshot, total_count, auto_retry) "
                    "VALUES (%s, %s, %s, %s, 'queued', %s, %s, true) "
                    "ON CONFLICT (build_id) DO NOTHING RETURNING build_id",
                    (build_id, batch_id, uuid4(), base, Jsonb(snapshot), len(snapshot)),
                ).fetchone()
                if inserted is None:
                    winner = connection.execute(
                        "SELECT batch_id, auto_retry FROM catalog_builds WHERE build_id = %s",
                        (build_id,),
                    ).fetchone()
                    if winner["batch_id"] != batch_id or not winner["auto_retry"]:
                        raise ManagementError("build_conflict", "build ID belongs to another build request")
        return self.get(build_id)

    def run_next(self) -> bool:
        """Run one queued job; publication stays an explicit admin decision."""
        with self.manager._connect() as connection:
            row = connection.execute(
                "SELECT batch_id, build_id FROM catalog_builds WHERE status = 'queued' "
                "ORDER BY created_at, build_id LIMIT 1"
            ).fetchone()
        if row is None:
            return False
        self.process(row["batch_id"], row["build_id"])
        return True

    def get(self, build_id: UUID) -> dict:
        with self.manager._connect() as connection:
            row = connection.execute(
                "SELECT b.build_id, b.batch_id, b.bundle_id, b.base_bundle_id, b.status, "
                "b.total_count, b.processed_count, b.failed_count, b.attempts, b.error_code, "
                "v.status AS publication_status "
                "FROM catalog_builds b LEFT JOIN bundle_versions v ON v.bundle_id = b.bundle_id "
                "WHERE b.build_id = %s", (build_id,),
            ).fetchone()
        if row is None:
            raise ManagementError("build_not_found", "catalog build does not exist", 404)
        return row

    def preview(self, build_id: UUID, offset: int, limit: int) -> dict:
        with self.manager._connect() as connection:
            row = connection.execute(
                "SELECT snapshot, total_count, status FROM catalog_builds WHERE build_id = %s",
                (build_id,),
            ).fetchone()
            if row is None:
                raise ManagementError("build_not_found", "catalog build does not exist", 404)
            items = row["snapshot"][offset:offset + limit]
            active = {item["item_id"]: item for item in connection.execute(
                "SELECT i.item_id, i.is_active, EXISTS (SELECT 1 FROM bundle_items bi "
                "JOIN catalog_control c ON bi.bundle_id = c.active_bundle_id "
                "WHERE bi.item_id = i.item_id) AS in_active_bundle "
                "FROM items i WHERE item_id = ANY(%s)",
                ([item["item_id"] for item in items],),
            ).fetchall()}
        return {"build_id": build_id, "total_count": row["total_count"], "offset": offset,
                "items": [{**item, "is_active": active[item["item_id"]]["is_active"],
                           "currently_recommendable": active[item["item_id"]]["is_active"]
                           and active[item["item_id"]]["in_active_bundle"],
                           "ready_for_publication": row["status"] == "ready"}
                          for item in items]}

    def _snapshot(self, connection, batch_id: UUID) -> tuple[UUID | None, list[dict]]:
        imported = connection.execute(
            "SELECT items_snapshot FROM catalog_imports WHERE batch_id = %s", (batch_id,),
        ).fetchone()
        if imported is None:
            raise ManagementError("import_not_found", "catalog import does not exist", 404)
        if imported["items_snapshot"] is None:
            raise ManagementError("legacy_import_without_snapshot", "this older import has no recoverable item snapshot")
        control = connection.execute(
            "SELECT active_bundle_id FROM catalog_control WHERE singleton = 1 FOR SHARE"
        ).fetchone()
        base = control["active_bundle_id"]
        imported_ids = {item["item_id"] for item in imported["items_snapshot"]}
        rows = connection.execute(
            "SELECT i.item_id, i.title, i.category, i.description, i.image_url FROM items i "
            "WHERE i.item_id = ANY(%s) OR EXISTS (SELECT 1 FROM bundle_items bi "
            "WHERE bi.bundle_id = %s AND bi.item_id = i.item_id) ORDER BY i.item_id LIMIT %s",
            (list(imported_ids), base, MAX_CATALOG_ITEMS + 1),
        ).fetchall()
        if not imported_ids <= {item["item_id"] for item in rows}:
            raise ManagementError("import_items_missing", "imported item is missing from the catalog")
        if not 1 <= len(rows) <= MAX_CATALOG_ITEMS:
            raise ManagementError("catalog_capacity_exceeded", "local content catalog supports 1 to 5000 items", 422)
        return base, list(rows)

    def process(self, batch_id: UUID, build_id: UUID) -> dict:
        if self.manager.managed_root is None:
            raise ManagementError("bundle_root_not_configured", "managed bundle root is not configured", 503)
        with self.manager._connect(autocommit=True) as connection:
            locked = connection.execute(
                "SELECT pg_try_advisory_lock(hashtext(%s)) AS acquired", (self.LOCK_NAME,),
            ).fetchone()["acquired"]
            if not locked:
                raise ManagementError("build_in_progress", "another catalog build is running; check status and retry", 503)
            try:
                with connection.transaction():
                    prior = connection.execute(
                        "SELECT * FROM catalog_builds WHERE build_id = %s FOR UPDATE", (build_id,),
                    ).fetchone()
                    if prior is not None and prior["batch_id"] != batch_id:
                        raise ManagementError("build_conflict", "build ID belongs to a different import")
                    if prior is not None and prior["status"] == "ready":
                        return self.get(build_id)
                    if prior is not None and prior["status"] == "processing":
                        raise ManagementError("build_in_progress", "build is still processing", 503)
                    bundle_id = uuid4()
                    if prior is None:
                        base, snapshot = self._snapshot(connection, batch_id)
                        connection.execute(
                            "INSERT INTO catalog_builds (build_id, batch_id, bundle_id, base_bundle_id, "
                            "status, snapshot, total_count) VALUES (%s, %s, %s, %s, 'processing', %s, %s)",
                            (build_id, batch_id, bundle_id, base, Jsonb(snapshot), len(snapshot)),
                        )
                    else:
                        snapshot = prior["snapshot"]
                        connection.execute(
                            "UPDATE bundle_versions SET status = 'failed' WHERE bundle_id = %s AND status = 'ready'",
                            (prior["bundle_id"],),
                        )
                        connection.execute(
                            "UPDATE catalog_builds SET bundle_id = %s, status = 'processing', "
                            "processed_count = 0, failed_count = 0, "
                            "attempts = attempts + CASE WHEN status = 'queued' AND error_code IS NULL "
                            "THEN 0 ELSE 1 END, "
                            "error_code = NULL, updated_at = now() WHERE build_id = %s",
                            (bundle_id, build_id),
                        )

                def progress(count):
                    connection.execute(
                        "UPDATE catalog_builds SET processed_count = %s, updated_at = now() WHERE build_id = %s",
                        (count, build_id),
                    )

                try:
                    build_catalog_bundle(self.manager.managed_root, bundle_id, build_id, snapshot, progress)
                    self.manager.register_bundle(bundle_id)
                    connection.execute(
                        "UPDATE catalog_builds SET status = 'ready', processed_count = total_count, "
                        "error_code = NULL, updated_at = now() WHERE build_id = %s", (build_id,),
                    )
                except Exception as exc:
                    code = getattr(exc, "code", "catalog_build_failed")
                    connection.execute(
                        "UPDATE catalog_builds SET status = 'failed', error_code = %s, "
                        "failed_count = total_count - processed_count, updated_at = now() WHERE build_id = %s",
                        (code, build_id),
                    )
                    raise ManagementError(code, "catalog build failed; inspect its status and retry", 422) from exc
                return self.get(build_id)
            finally:
                connection.execute("SELECT pg_advisory_unlock(hashtext(%s))", (self.LOCK_NAME,))

    def publish(self, build_id: UUID, operation_id: UUID) -> dict:
        build = self.get(build_id)
        if build["status"] != "ready":
            raise ManagementError("build_not_ready", "catalog build is not ready for publication")
        return self.manager.publish(operation_id, build["bundle_id"], build["base_bundle_id"])

    def recover_interrupted(self) -> None:
        """Queue interrupted background jobs; leave synchronous builds retryable."""
        with self.manager._connect(autocommit=True) as connection:
            if not connection.execute(
                "SELECT pg_try_advisory_lock(hashtext(%s)) AS acquired", (self.LOCK_NAME,),
            ).fetchone()["acquired"]:
                return
            try:
                connection.execute(
                    "UPDATE catalog_builds SET status = 'queued', error_code = 'build_interrupted', "
                    "failed_count = total_count - processed_count, updated_at = now() "
                    "WHERE status = 'processing' AND auto_retry"
                )
                connection.execute(
                    "UPDATE catalog_builds SET status = 'failed', error_code = 'build_interrupted', "
                    "failed_count = total_count - processed_count, updated_at = now() "
                    "WHERE status = 'processing' AND NOT auto_retry"
                )
            finally:
                connection.execute("SELECT pg_advisory_unlock(hashtext(%s))", (self.LOCK_NAME,))
