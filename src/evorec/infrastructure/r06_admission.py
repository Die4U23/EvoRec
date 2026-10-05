"""Trusted PostgreSQL capture and pinned restoration, never HTTP input hashes.

The content seal proves actual text/time matched the approved immutable package
at capture. Restoration uses that stored seal, not mutable current item rows.
Database ownership is the trust boundary, not a cryptographic signature.
"""

from dataclasses import dataclass
import hashlib
import json

from evorec.domain.errors import ManagementError
from evorec.domain.models import ModelSnapshot
from evorec.infrastructure.r06_bundle import KIND, FrozenCatalogItem, FrozenR06Bundle, _item_digest
from evorec.infrastructure.r06_serving import FrozenR06Request
from evorec.infrastructure.residual_ranker import ControlledLoadError


@dataclass(frozen=True)
class ManagedR06Runtime:
    bundle: FrozenR06Bundle

    @property
    def bundle_id(self):
        return str(self.bundle.bundle_id)

    @property
    def manifest_sha256(self):
        return self.bundle.manifest_sha256

    @property
    def item_ids(self):
        return self.bundle.adapter.features.item_ids


def encode_model(model):
    if model is None:
        return None
    return dict(manifest_sha256=model.manifest_sha256, model_version=model.model_version,
                timestamp_ms=model.timestamp_ms, full_seen=sorted(model.full_seen),
                catalog_sha256=model.catalog_sha256)


def decode_model(data):
    return ModelSnapshot(**data) if data is not None else None


def _seal(bundle, identities):
    raw = json.dumps([KIND, bundle.manifest_sha256, sorted(identities)],
                     ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def restore_request(bundle, context):
    """Only for server-owned persisted snapshots; not a client authorization API."""
    model = context.model
    if (model is None or context.catalog.bundle_id != bundle.bundle_id
            or model.manifest_sha256 != bundle.manifest_sha256
            or model.model_version != bundle.model_version
            or not context.catalog.eligible_items <= bundle.catalog_item_sha256.keys()
            or model.catalog_sha256 != _seal(bundle, [(item, bundle.catalog_item_sha256[item])
                                                    for item in context.catalog.eligible_items])):
        raise ManagementError("r06_snapshot_changed", "frozen model input identity differs", 503)
    try:
        request = FrozenR06Request(context, model.timestamp_ms, model.full_seen,
                                   bundle.adapter.features.manifest_sha256,
                                   bundle.adapter.features.provenance["catalog_sha256"])
        features = bundle.adapter.features
        if any(features._metadata[features._indices[item]].first_seen_ms >= model.timestamp_ms
               for item in context.session.history if item in features._indices):
            raise ManagementError("r06_snapshot_time", "known history does not predate capture", 422)
        return request
    except ControlledLoadError as error:
        raise ManagementError(error.code, "frozen R06 input validation failed", 422) from error


def capture_model(connection, bundle, session, catalog, rows):
    row = connection.execute(
        "SELECT runtime_kind, manifest_sha256 FROM bundle_versions WHERE bundle_id = %s",
        (catalog.bundle_id,),
    ).fetchone()
    if (row is None or row["runtime_kind"] != KIND
            or (row["manifest_sha256"] or "").strip() != bundle.manifest_sha256):
        raise ManagementError("r06_snapshot_changed", "registered model identity differs", 503)
    if (tuple(row["item_id"] for row in rows) != bundle.adapter.features.item_ids
            or any(row["internal_item_id"] != index for index, row in enumerate(rows))):
        raise ManagementError("bundle_members_changed", "approved ordered membership changed", 409)
    identities = []
    try:
        for row in rows:
            if not row["is_active"]:
                continue
            item = FrozenCatalogItem(row["item_id"], row["r06_model_text"], row["r06_first_seen_ms"])
            digest = _item_digest(item)
            if bundle.catalog_item_sha256.get(item.item_id) != digest:
                raise ManagementError("r06_catalog_changed", "actual model text/time changed", 409)
            identities.append((item.item_id, digest))
    except ControlledLoadError as error:
        raise ManagementError("r06_catalog_changed", "actual model text/time is invalid", 409) from error
    if frozenset(item for item, _ in identities) != catalog.eligible_items or len(identities) != len(catalog.eligible_items):
        raise ManagementError("r06_catalog_changed", "eligible catalog changed during admission", 409)
    seen = set(session.history) | session.hidden_items | session.favorite_items
    seen.update(row["item_id"] for row in connection.execute(
        "SELECT DISTINCT item_id FROM feedback_events WHERE session_id = %s AND session_epoch = %s LIMIT 10001",
        (session.session_id, session.epoch),
    ).fetchall())
    timestamp = connection.execute(
        "SELECT floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint AS timestamp_ms",
    ).fetchone()["timestamp_ms"]
    try:
        return ModelSnapshot(bundle.manifest_sha256, bundle.model_version, timestamp, seen, _seal(bundle, identities))
    except ValueError as error:
        raise ManagementError("r06_input_limit", "full seen input exceeds the serving limits", 422) from error
