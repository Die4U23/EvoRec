"""Single-process catalog management with durable PostgreSQL publication records."""

import hashlib
import json
from pathlib import Path
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from evorec.domain.errors import ManagementError
from evorec.infrastructure.bundle import BundleValidationError, validate_bundle
from evorec.infrastructure.model_runtime import ControlledLoadError, RuntimeBundle, load_runtime_bundle
from evorec.infrastructure.postgres import PostgresDemoBackend


class CatalogManager:
    """One coordinator only; the database lock serializes publication/recovery."""

    LOCK_NAME = "evorec:publication"

    def __init__(self, backend: PostgresDemoBackend, managed_root: Path | None):
        self.backend = backend
        self.database_url = backend.database_url
        self.managed_root = managed_root

    def _connect(self, *, autocommit: bool = False):
        return psycopg.connect(self.database_url, row_factory=dict_row, autocommit=autocommit)

    def import_items(self, payload) -> dict[str, object]:
        canonical = json.dumps(payload.model_dump(mode="json"), sort_keys=True,
                               separators=(",", ":")).encode("utf-8")
        digest = hashlib.sha256(canonical).hexdigest()
        with self._connect() as connection:
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (str(payload.batch_id),),
            )
            prior = connection.execute(
                "SELECT payload_sha256, item_count FROM catalog_imports WHERE batch_id = %s FOR UPDATE",
                (payload.batch_id,),
            ).fetchone()
            if prior is not None:
                if prior["payload_sha256"].strip() != digest:
                    raise ManagementError("import_conflict", "batch ID was used for different items")
                return {"batch_id": payload.batch_id, "item_count": prior["item_count"],
                        "replayed": True}
            for item in payload.items:
                connection.execute(
                    """
                    INSERT INTO items (item_id, title, category, description, image_url)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (item_id) DO UPDATE SET
                        title = EXCLUDED.title, category = EXCLUDED.category,
                        description = EXCLUDED.description, image_url = EXCLUDED.image_url,
                        updated_at = now()
                    """,
                    (item.item_id, item.title, item.category, item.description, item.image_url),
                )
            connection.execute(
                "INSERT INTO catalog_imports (batch_id, payload_sha256, item_count) VALUES (%s, %s, %s)",
                (payload.batch_id, digest, len(payload.items)),
            )
        return {"batch_id": payload.batch_id, "item_count": len(payload.items),
                "replayed": False}

    def list_items(self, *, limit: int = 100) -> list[dict[str, object]]:
        with self._connect() as connection:
            return list(connection.execute(
                """
                SELECT item_id, title, category, description, image_url, is_active
                FROM items ORDER BY item_id LIMIT %s
                """, (limit,),
            ).fetchall())

    def get_item(self, item_id: str) -> dict[str, object]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT item_id, title, category, description, image_url, is_active
                FROM items WHERE item_id = %s
                """, (item_id,),
            ).fetchone()
        if row is None:
            raise ManagementError("item_not_found", "item does not exist", 404)
        return row

    def deactivate_item(self, item_id: str) -> dict[str, object]:
        with self._connect() as connection:
            connection.execute("SELECT singleton FROM catalog_control WHERE singleton = 1 FOR UPDATE")
            row = connection.execute(
                "UPDATE items SET is_active = false, updated_at = now() "
                "WHERE item_id = %s AND is_active RETURNING item_id", (item_id,),
            ).fetchone()
            if row is None:
                item = connection.execute(
                    "SELECT item_id FROM items WHERE item_id = %s", (item_id,),
                ).fetchone()
                if item is None:
                    raise ManagementError("item_not_found", "item does not exist", 404)
            else:
                connection.execute(
                    "UPDATE catalog_control SET exclusion_version = exclusion_version + 1, "
                    "updated_at = now() WHERE singleton = 1"
                )
            control = connection.execute(
                "SELECT exclusion_version FROM catalog_control WHERE singleton = 1"
            ).fetchone()
        return {"item_id": item_id, "is_active": False,
                "exclusion_version": control["exclusion_version"]}

    def _load(self, bundle_id: UUID) -> RuntimeBundle:
        if self.managed_root is None:
            raise ManagementError("bundle_root_not_configured", "managed bundle root is not configured", 503)
        try:
            bundle = validate_bundle(self.managed_root, self.managed_root / str(bundle_id))
            return load_runtime_bundle(bundle)
        except (BundleValidationError, ControlledLoadError) as exc:
            raise ManagementError(exc.code, "bundle validation or controlled loading failed", 422) from exc

    def register_bundle(self, bundle_id: UUID) -> dict[str, object]:
        runtime = self._load(bundle_id)
        with self._connect() as connection:
            prior = connection.execute(
                "SELECT manifest_sha256, artifact_path FROM bundle_versions "
                "WHERE bundle_id = %s FOR UPDATE", (bundle_id,),
            ).fetchone()
            path = f"managed/{bundle_id}"
            if prior is not None:
                if prior["manifest_sha256"].strip() != runtime.manifest_sha256 or prior["artifact_path"] != path:
                    raise ManagementError("bundle_conflict", "bundle ID was registered differently")
                self._verify_members(connection, runtime)
                return {"bundle_id": bundle_id, "manifest_sha256": runtime.manifest_sha256,
                        "item_count": len(runtime.item_ids), "replayed": True}
            existing = {row["item_id"] for row in connection.execute(
                "SELECT item_id FROM items WHERE item_id = ANY(%s)", (list(runtime.item_ids),)
            ).fetchall()}
            if existing != set(runtime.item_ids):
                raise ManagementError("bundle_items_missing", "import all bundle items before registration")
            connection.execute(
                "INSERT INTO bundle_versions (bundle_id, status, artifact_path, manifest_sha256) "
                "VALUES (%s, 'ready', %s, %s)",
                (bundle_id, path, runtime.manifest_sha256),
            )
            with connection.cursor() as cursor:
                cursor.executemany(
                    "INSERT INTO bundle_items (bundle_id, item_id, internal_item_id) VALUES (%s, %s, %s)",
                    [(bundle_id, item_id, index)
                     for index, item_id in enumerate(runtime.item_ids)],
                )
        return {"bundle_id": bundle_id, "manifest_sha256": runtime.manifest_sha256,
                "item_count": len(runtime.item_ids), "replayed": False}

    def _registered_runtime(self, connection, bundle_id: UUID) -> RuntimeBundle:
        row = connection.execute(
            "SELECT artifact_path, manifest_sha256 FROM bundle_versions WHERE bundle_id = %s",
            (bundle_id,),
        ).fetchone()
        if row is None or row["artifact_path"] != f"managed/{bundle_id}":
            raise ManagementError("bundle_not_registered", "controlled bundle is not registered", 404)
        runtime = self._load(bundle_id)
        if row["manifest_sha256"].strip() != runtime.manifest_sha256:
            raise ManagementError("bundle_changed", "registered manifest hash changed")
        self._verify_members(connection, runtime)
        return runtime

    @staticmethod
    def _verify_members(connection, runtime: RuntimeBundle) -> None:
        rows = connection.execute(
            "SELECT item_id FROM bundle_items WHERE bundle_id = %s ORDER BY internal_item_id",
            (runtime.bundle_id,),
        ).fetchall()
        if tuple(row["item_id"] for row in rows) != runtime.item_ids:
            raise ManagementError("bundle_members_changed", "registered bundle members changed")

    def publication_state(self) -> dict[str, object]:
        with self._connect() as connection:
            control = connection.execute(
                "SELECT active_bundle_id, exclusion_version, admission_open "
                "FROM catalog_control WHERE singleton = 1"
            ).fetchone()
            pending = connection.execute(
                "SELECT operation_id, status FROM publication_operations "
                "WHERE status IN ('preparing', 'switched') ORDER BY created_at LIMIT 1"
            ).fetchone()
        return {**control, "pending_operation": pending}

    def publish(self, operation_id: UUID, bundle_id: UUID,
                expected_active_bundle_id: UUID | None) -> dict[str, object]:
        with self._connect() as connection:
            completed = connection.execute(
                "SELECT target_bundle_id, expected_active_bundle_id, status "
                "FROM publication_operations WHERE operation_id = %s", (operation_id,),
            ).fetchone()
        if completed is not None:
            if (completed["target_bundle_id"] != bundle_id
                    or completed["expected_active_bundle_id"] != expected_active_bundle_id):
                raise ManagementError("publication_conflict", "operation ID was used differently")
            if completed["status"] == "completed":
                return {"operation_id": operation_id, "active_bundle_id": bundle_id,
                        "status": "completed", "replayed": True}
            if completed["status"] == "aborted":
                raise ManagementError("publication_aborted", "operation was aborted; use a new ID")
        self.recover()
        with self._connect(autocommit=True) as connection:
            connection.execute("SELECT pg_advisory_lock(hashtext(%s))", (self.LOCK_NAME,))
            try:
                prior = connection.execute(
                    "SELECT target_bundle_id, expected_active_bundle_id, status "
                    "FROM publication_operations WHERE operation_id = %s", (operation_id,),
                ).fetchone()
                if prior is not None:
                    if (prior["target_bundle_id"] != bundle_id
                            or prior["expected_active_bundle_id"] != expected_active_bundle_id):
                        raise ManagementError("publication_conflict", "operation ID was used differently")
                    if prior["status"] == "completed":
                        return {"operation_id": operation_id, "active_bundle_id": bundle_id,
                                "status": "completed", "replayed": True}
                    if prior["status"] == "aborted":
                        raise ManagementError("publication_aborted", "operation was aborted; use a new ID")
                    raise ManagementError("publication_incomplete", "operation requires recovery", 503)
                unfinished = connection.execute(
                    "SELECT operation_id FROM publication_operations "
                    "WHERE status IN ('preparing', 'switched') LIMIT 1"
                ).fetchone()
                if unfinished is not None:
                    raise ManagementError("publication_incomplete", "another operation requires recovery", 503)
                runtime = self._registered_runtime(connection, bundle_id)
                row = connection.execute(
                    "SELECT manifest_sha256, status FROM bundle_versions WHERE bundle_id = %s",
                    (bundle_id,),
                ).fetchone()
                if row is None or row["manifest_sha256"].strip() != runtime.manifest_sha256:
                    raise ManagementError("bundle_changed", "bundle is missing or changed")
                if row["status"] not in {"ready", "retired", "active"}:
                    raise ManagementError("bundle_not_ready", "bundle is not publishable")
                with connection.transaction():
                    control = connection.execute(
                        "SELECT active_bundle_id FROM catalog_control WHERE singleton = 1 FOR UPDATE"
                    ).fetchone()
                    if control["active_bundle_id"] != expected_active_bundle_id:
                        raise ManagementError("publication_version_conflict", "active bundle changed")
                    connection.execute(
                        "INSERT INTO publication_operations "
                        "(operation_id, target_bundle_id, expected_active_bundle_id, previous_bundle_id, status) "
                        "VALUES (%s, %s, %s, %s, 'preparing')",
                        (operation_id, bundle_id, expected_active_bundle_id,
                         control["active_bundle_id"]),
                    )
                    connection.execute(
                        "UPDATE catalog_control SET admission_open = false, updated_at = now() "
                        "WHERE singleton = 1"
                    )
                with connection.transaction():
                    control = connection.execute(
                        "SELECT active_bundle_id FROM catalog_control WHERE singleton = 1 FOR UPDATE"
                    ).fetchone()
                    if control["active_bundle_id"] != expected_active_bundle_id:
                        raise ManagementError("publication_version_conflict", "active bundle changed")
                    if expected_active_bundle_id != bundle_id:
                        connection.execute(
                            "UPDATE bundle_versions SET status = 'retired' "
                            "WHERE bundle_id = %s AND status = 'active'",
                            (expected_active_bundle_id,),
                        )
                    connection.execute(
                        "UPDATE bundle_versions SET status = 'active' WHERE bundle_id = %s",
                        (bundle_id,),
                    )
                    connection.execute(
                        "UPDATE catalog_control SET active_bundle_id = %s, updated_at = now() "
                        "WHERE singleton = 1", (bundle_id,),
                    )
                    connection.execute(
                        "UPDATE publication_operations SET status = 'switched', updated_at = now() "
                        "WHERE operation_id = %s", (operation_id,),
                    )
                self.backend.activate_runtime(runtime)
                with connection.transaction():
                    connection.execute(
                        "UPDATE catalog_control SET admission_open = true, updated_at = now() "
                        "WHERE singleton = 1 AND active_bundle_id = %s", (bundle_id,),
                    )
                    connection.execute(
                        "UPDATE publication_operations SET status = 'completed', "
                        "completed_at = now(), updated_at = now() WHERE operation_id = %s",
                        (operation_id,),
                    )
                return {"operation_id": operation_id, "active_bundle_id": bundle_id,
                        "status": "completed", "replayed": False}
            finally:
                connection.execute("SELECT pg_advisory_unlock(hashtext(%s))", (self.LOCK_NAME,))

    def ensure_ready(self) -> None:
        self.recover()

    def recover(self) -> None:
        """On restart, reconcile the durable pointer and unfinished operation."""
        with self._connect(autocommit=True) as connection:
            connection.execute("SELECT pg_advisory_lock(hashtext(%s))", (self.LOCK_NAME,))
            try:
                control = connection.execute(
                    "SELECT active_bundle_id, admission_open FROM catalog_control WHERE singleton = 1"
                ).fetchone()
                active = control["active_bundle_id"]
                pending = connection.execute(
                    "SELECT operation_id, target_bundle_id, previous_bundle_id, status "
                    "FROM publication_operations WHERE status IN ('preparing', 'switched') "
                    "ORDER BY created_at LIMIT 1"
                ).fetchone()
                if (pending is not None and pending["status"] == "preparing"
                        and active == pending["previous_bundle_id"]
                        and active != pending["target_bundle_id"]):
                    # No pointer commit: abort this attempt and restore the old version.
                    with connection.transaction():
                        connection.execute(
                            "UPDATE publication_operations SET status = 'aborted', "
                            "error_code = 'interrupted_before_switch', updated_at = now() "
                            "WHERE operation_id = %s", (pending["operation_id"],),
                        )
                    pending = None
                if pending is not None and active != pending["target_bundle_id"]:
                    connection.execute(
                        "UPDATE catalog_control SET admission_open = false WHERE singleton = 1"
                    )
                    return
                if active is None:
                    return
                row = connection.execute(
                    "SELECT artifact_path, manifest_sha256 FROM bundle_versions WHERE bundle_id = %s",
                    (active,),
                ).fetchone()
                if row is None or row["artifact_path"] != f"managed/{active}":
                    # Legacy local demo seed is not a controlled model runtime.
                    if pending is None and not control["admission_open"]:
                        connection.execute(
                            "UPDATE catalog_control SET admission_open = true WHERE singleton = 1"
                        )
                    return
                if (self.backend.runtime is None
                        or self.backend.runtime.bundle_id != str(active)
                        or self.backend.runtime.manifest_sha256 != row["manifest_sha256"].strip()):
                    try:
                        runtime = self._registered_runtime(connection, active)
                    except Exception:
                        connection.execute(
                            "UPDATE catalog_control SET admission_open = false WHERE singleton = 1"
                        )
                        return
                    self.backend.activate_runtime(runtime)
                if pending is not None and active == pending["target_bundle_id"]:
                    with connection.transaction():
                        connection.execute(
                            "UPDATE publication_operations SET status = 'completed', "
                            "completed_at = now(), updated_at = now() WHERE operation_id = %s",
                            (pending["operation_id"],),
                        )
                        connection.execute(
                            "UPDATE catalog_control SET admission_open = true, updated_at = now() "
                            "WHERE singleton = 1"
                        )
                elif pending is None and not control["admission_open"]:
                    connection.execute(
                        "UPDATE catalog_control SET admission_open = true WHERE singleton = 1"
                    )
            finally:
                connection.execute("SELECT pg_advisory_unlock(hashtext(%s))", (self.LOCK_NAME,))
