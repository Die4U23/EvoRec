"""Transactional frozen corpus preparation, not activation or online admission."""

import hashlib
from pathlib import Path
from uuid import UUID

from evorec.domain.errors import ManagementError
from evorec.infrastructure.r06_bundle import KIND, FrozenCatalogItem, _item_digest, load_r06_bundle
from evorec.infrastructure.r06_features import MAX_JSON_BYTES, _json
from evorec.infrastructure.residual_ranker import ControlledLoadError, _digest, _read, _verified

CATEGORY = "R06 frozen corpus"


def display_title(item_id, text):
    return text.strip()[:300] or item_id


def source_records(root, runtime):
    """Read approved raw bytes again; never trust a newly computed input hash."""
    root = Path(root).resolve()
    raw = _read(root, "manifest.json", 16*1024)
    if hashlib.sha256(raw).hexdigest() != runtime.manifest_sha256:
        raise ControlledLoadError("component_changed", "bundle changed during preparation")
    manifest = _json(raw)
    catalog = _json(_verified(root, manifest["catalog"], "catalog.json", MAX_JSON_BYTES))
    metadata = _json(_verified(root, manifest["metadata"], "metadata.json", MAX_JSON_BYTES))
    records = tuple(FrozenCatalogItem(item, metadata.get(item, ""), catalog[item])
                    for item in runtime.adapter.features.item_ids)
    if any(_item_digest(item) != runtime.catalog_item_sha256[item.item_id] for item in records):
        raise ControlledLoadError("catalog_changed", "source content differs from the loaded package")
    return records


def verify_database_sources(connection, runtime):
    """Validate actual stored model text/time, derived display and membership."""
    rows = connection.execute(
        "SELECT bi.item_id, bi.internal_item_id, i.title, i.category, i.description, i.image_url, "
        "i.r06_model_text, i.r06_first_seen_ms FROM bundle_items bi "
        "JOIN items i ON i.item_id = bi.item_id WHERE bi.bundle_id = %s ORDER BY bi.internal_item_id",
        (runtime.bundle_id,),
    ).fetchall()
    if tuple(row["item_id"] for row in rows) != runtime.adapter.features.item_ids:
        raise ManagementError("bundle_members_changed", "frozen bundle members changed")
    for index, row in enumerate(rows):
        text = row["r06_model_text"]
        try:
            valid = (_item_digest(FrozenCatalogItem(row["item_id"], text, row["r06_first_seen_ms"]))
                     == runtime.catalog_item_sha256[row["item_id"]])
        except ControlledLoadError:
            valid = False
        if (not valid or row["internal_item_id"] != index
                or row["title"] != display_title(row["item_id"], text)
                or row["category"] != CATEGORY or row["description"] != text
                or row["image_url"] is not None):
            raise ManagementError("r06_catalog_changed", "stored R06 item representation changed")
    return rows


class R06CatalogPreparation:
    def __init__(self, manager):
        self.manager = manager

    def load_registered(self, connection, bundle_id, *, content_backend="stdlib", ranker_backend="stdlib", verify_sources=True):
        row = connection.execute(
            "SELECT artifact_path, manifest_sha256, runtime_kind, status FROM bundle_versions WHERE bundle_id = %s",
            (bundle_id,),
        ).fetchone()
        if (row is None or row["artifact_path"] != f"managed/{bundle_id}" or row["runtime_kind"] != KIND
                or row["status"] not in {"ready", "retired", "active"} or not row["manifest_sha256"]):
            raise ManagementError("bundle_not_registered", "approved R06 bundle is not registered", 404)
        runtime = self._load(bundle_id, row["manifest_sha256"].strip(),
                             content_backend=content_backend, ranker_backend=ranker_backend)
        if verify_sources:
            verify_database_sources(connection, runtime)
        return runtime

    def _load(self, bundle_id, digest, *, content_backend="stdlib", ranker_backend="stdlib"):
        if self.manager.managed_root is None:
            raise ManagementError("bundle_root_not_configured", "managed bundle root is not configured", 503)
        try:
            return load_r06_bundle(self.manager.managed_root, self.manager.managed_root / str(bundle_id),
                                   expected_manifest_sha256=digest, content_backend=content_backend, ranker_backend=ranker_backend)
        except ControlledLoadError as error:
            raise ManagementError(error.code, "approved R06 package validation failed", 422) from error

    def prepare(self, bundle_id: UUID, expected_manifest_sha256: str):
        """One atomic preparation keyed by bundle ID/hash; never overwrite items."""
        if type(bundle_id) is not UUID:
            raise ManagementError("invalid_bundle_id", "bundle ID must be a UUID", 422)
        try:
            _digest(expected_manifest_sha256)
        except ControlledLoadError as error:
            raise ManagementError(error.code, "approval must be a lowercase SHA-256", 422) from error
        runtime = self._load(bundle_id, expected_manifest_sha256)
        try:
            records = source_records(self.manager.managed_root / str(bundle_id), runtime)
        except ControlledLoadError as error:
            raise ManagementError(error.code, "R06 source changed during preparation", 422) from error
        with self.manager._connect() as connection:
            connection.execute(f"SELECT pg_advisory_xact_lock({self.manager.LOCK_KEY_SQL})", (self.manager.LOCK_NAME,))
            prior = connection.execute(
                "SELECT manifest_sha256, artifact_path, runtime_kind, status FROM bundle_versions "
                "WHERE bundle_id = %s FOR UPDATE", (bundle_id,),
            ).fetchone()
            if prior is not None:
                if ((prior["manifest_sha256"] or "").strip() != expected_manifest_sha256
                        or prior["artifact_path"] != f"managed/{bundle_id}" or prior["runtime_kind"] != KIND
                        or prior["status"] not in {"ready", "retired", "active"}):
                    raise ManagementError("bundle_conflict", "bundle ID was registered differently")
                verify_database_sources(connection, runtime)
                return dict(bundle_id=bundle_id, manifest_sha256=expected_manifest_sha256,
                            item_count=len(records), replayed=True)
            # Lock existing rows to freeze conflict checks through the transaction.
            existing = {row["item_id"]: row for row in connection.execute(
                "SELECT item_id, title, category, description, image_url, r06_first_seen_ms, r06_model_text "
                "FROM items WHERE item_id = ANY(%s) FOR UPDATE", (list(runtime.adapter.features.item_ids),),
            ).fetchall()}
            new = []
            for item in records:
                row = existing.get(item.item_id)
                values = (item.item_id, display_title(item.item_id, item.text), CATEGORY, item.text,
                          item.first_seen_ms, item.text)
                if row is None:
                    new.append(values)
                elif (tuple(row[k] for k in ("item_id", "title", "category", "description", "r06_first_seen_ms", "r06_model_text"))
                      != values or row["image_url"] is not None):
                    raise ManagementError("r06_item_conflict", "an existing item has a different source; nothing was imported")
            with connection.cursor() as cursor:
                cursor.executemany(
                    "INSERT INTO items(item_id, title, category, description, r06_first_seen_ms, r06_model_text) "
                    "VALUES (%s, %s, %s, %s, %s, %s)", new,
                )
            connection.execute(
                "INSERT INTO bundle_versions(bundle_id, status, artifact_path, manifest_sha256, runtime_kind) "
                "VALUES (%s, 'ready', %s, %s, %s)",
                (bundle_id, f"managed/{bundle_id}", expected_manifest_sha256, KIND),
            )
            with connection.cursor() as cursor:
                cursor.executemany("INSERT INTO bundle_items(bundle_id, item_id, internal_item_id) VALUES (%s, %s, %s)",
                                   [(bundle_id, item.item_id, index) for index, item in enumerate(records)])
            verify_database_sources(connection, runtime)
            # A fresh bulk load can otherwise be served before autovacuum has
            # collected statistics, badly underestimating the full corpus.
            # Scope maintenance to these tables, once after actual verification;
            # it is neither a cached content proof nor a request-time operation.
            connection.execute("ANALYZE items, bundle_items")
        return dict(bundle_id=bundle_id, manifest_sha256=expected_manifest_sha256,
                    item_count=len(records), replayed=False)
