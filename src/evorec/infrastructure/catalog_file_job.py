"""Durable, bounded file validation before catalog import."""

import hashlib
from collections.abc import Callable
from uuid import UUID

from psycopg.types.json import Jsonb

from evorec.domain.catalog_file import CatalogFileError, MAX_FILE_BYTES
from evorec.domain.errors import ManagementError


class CatalogFileJobService:
    LOCK_NAME = "evorec:catalog-file-job"

    def __init__(self, manager):
        self.manager = manager

    def enqueue(self, batch_id: UUID, data: bytes, media_type: str) -> dict:
        if len(data) > MAX_FILE_BYTES:
            raise ManagementError("file_too_large", "catalog file exceeds 20 MB", 413)
        digest = hashlib.sha256(media_type.encode("utf-8") + b"\0" + data).hexdigest()
        with self.manager._connect() as connection:
            connection.execute(
                f"SELECT pg_advisory_xact_lock({self.manager.LOCK_KEY_SQL})", (self.manager.LOCK_NAME,),
            )
            prior = connection.execute(
                "SELECT payload_sha256 FROM catalog_file_jobs WHERE batch_id = %s FOR UPDATE",
                (batch_id,),
            ).fetchone()
            if prior is not None:
                if prior["payload_sha256"].strip() != digest:
                    raise ManagementError("import_conflict", "batch ID was used for different file content")
                replayed = True
            else:
                if connection.execute(
                    "SELECT 1 FROM catalog_imports WHERE batch_id = %s", (batch_id,),
                ).fetchone():
                    raise ManagementError("import_conflict", "batch ID already belongs to an import")
                inserted = connection.execute(
                    "INSERT INTO catalog_file_jobs (batch_id, payload_sha256, media_type, status, payload) "
                    "VALUES (%s, %s, %s, 'queued', %s) ON CONFLICT DO NOTHING RETURNING batch_id",
                    (batch_id, digest, media_type, data),
                ).fetchone()
                if inserted is None:
                    winner = connection.execute(
                        "SELECT payload_sha256 FROM catalog_file_jobs WHERE batch_id = %s",
                        (batch_id,),
                    ).fetchone()
                    if winner["payload_sha256"].strip() != digest:
                        raise ManagementError("import_conflict", "batch ID was used for different file content")
                    replayed = True
                else:
                    replayed = False
        return {**self.get(batch_id), "replayed": replayed}

    def get(self, batch_id: UUID) -> dict:
        with self.manager._connect() as connection:
            row = connection.execute(
                "SELECT batch_id, status, item_count, error_code, row_errors, attempts, "
                "created_at, updated_at FROM catalog_file_jobs WHERE batch_id = %s",
                (batch_id,),
            ).fetchone()
        if row is None:
            raise ManagementError("file_job_not_found", "catalog file job does not exist", 404)
        return row

    def run_next(self, parse_file: Callable) -> bool:
        with self.manager._connect() as connection:
            row = connection.execute(
                "SELECT batch_id FROM catalog_file_jobs WHERE status = 'queued' "
                "ORDER BY created_at, batch_id LIMIT 1"
            ).fetchone()
        if row is None:
            return False
        self.process(row["batch_id"], parse_file)
        return True

    def process(self, batch_id: UUID, parse_file: Callable) -> dict:
        with self.manager._connect(autocommit=True) as connection:
            if not connection.execute(
                "SELECT pg_try_advisory_lock(hashtext(%s)) AS acquired", (self.LOCK_NAME,),
            ).fetchone()["acquired"]:
                raise ManagementError("file_job_in_progress", "another file job is running", 503)
            try:
                with connection.transaction():
                    row = connection.execute(
                        "SELECT status, payload, media_type FROM catalog_file_jobs "
                        "WHERE batch_id = %s FOR UPDATE", (batch_id,),
                    ).fetchone()
                    if row is None:
                        raise ManagementError("file_job_not_found", "catalog file job does not exist", 404)
                    if row["status"] in {"imported", "failed"}:
                        return self.get(batch_id)
                    if row["status"] != "queued":
                        raise ManagementError("file_job_in_progress", "file job is validating", 503)
                    connection.execute(
                        "UPDATE catalog_file_jobs SET status = 'validating', attempts = attempts + 1, "
                        "error_code = NULL, updated_at = now() WHERE batch_id = %s", (batch_id,),
                    )
                try:
                    payload = parse_file(bytes(row["payload"]), row["media_type"], batch_id)
                    offset = 2 if row["media_type"] == "text/csv" else 1
                    numbers = {item.item_id: number for number, item in enumerate(payload.items, offset)}
                    result = self.manager.import_items(payload, row_numbers=numbers, from_job=True)
                except CatalogFileError as exc:
                    self._fail(connection, batch_id, exc.code,
                               [issue.__dict__ for issue in exc.rows])
                except ManagementError as exc:
                    self._fail(connection, batch_id, exc.code, exc.rows or [])
                else:
                    connection.execute(
                        "UPDATE catalog_file_jobs SET status = 'imported', payload = NULL, "
                        "item_count = %s, error_code = NULL, row_errors = '[]'::jsonb, "
                        "updated_at = now() WHERE batch_id = %s",
                        (result["item_count"], batch_id),
                    )
                return self.get(batch_id)
            finally:
                connection.execute("SELECT pg_advisory_unlock(hashtext(%s))", (self.LOCK_NAME,))

    def _fail(self, connection, batch_id: UUID, code: str, rows: list[dict]) -> None:
        connection.execute(
            "UPDATE catalog_file_jobs SET status = 'failed', payload = NULL, error_code = %s, "
            "row_errors = %s, updated_at = now() WHERE batch_id = %s",
            (code, Jsonb(rows), batch_id),
        )

    def recover_interrupted(self) -> None:
        with self.manager._connect(autocommit=True) as connection:
            if not connection.execute(
                "SELECT pg_try_advisory_lock(hashtext(%s)) AS acquired", (self.LOCK_NAME,),
            ).fetchone()["acquired"]:
                return
            try:
                connection.execute(
                    "UPDATE catalog_file_jobs SET status = 'queued', error_code = 'validation_interrupted', "
                    "updated_at = now() WHERE status = 'validating'"
                )
            finally:
                connection.execute("SELECT pg_advisory_unlock(hashtext(%s))", (self.LOCK_NAME,))
