"""Trusted PostgreSQL capture and pinned restoration, never HTTP input hashes.

The content seal proves actual text/time matched the approved immutable package
at capture. Restoration uses that stored seal, not mutable current item rows.
Database ownership is the trust boundary, not a cryptographic signature.
"""

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from evorec.domain.errors import ManagementError
from evorec.domain.models import ModelSnapshot
from evorec.infrastructure.r06_bundle import KIND, FrozenCatalogItem, FrozenR06Bundle, _item_digest, catalog_seal
from evorec.infrastructure.r06_serving import FrozenR06Request
from evorec.infrastructure.residual_ranker import ControlledLoadError


@dataclass(frozen=True)
class ManagedR06Runtime:
    bundle: FrozenR06Bundle
    catalog_items: Mapping[str, FrozenCatalogItem]

    def __post_init__(self):
        # Pay canonical text hashing once at load. Requests still compare actual
        # DB text/time, not a cached client claim, before using these identities.
        records = dict(self.catalog_items)
        if (type(self.bundle) is not FrozenR06Bundle
                or set(records) != set(self.bundle.adapter.features.item_ids)
                or any(type(item) is not FrozenCatalogItem or item.item_id != key
                       or _item_digest(item) != self.bundle.catalog_item_sha256[key]
                       for key, item in records.items())):
            raise ManagementError("r06_catalog_changed", "approved runtime source table differs", 422)
        object.__setattr__(self, "catalog_items", MappingProxyType(records))

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
    return catalog_seal(bundle.manifest_sha256, identities)


def restore_request(bundle, context):
    """Only for server-owned persisted snapshots; not a client authorization API."""
    model = context.model
    if (model is None or context.catalog.bundle_id != bundle.bundle_id
            or model.manifest_sha256 != bundle.manifest_sha256
            or model.model_version != bundle.model_version
            or not context.catalog.eligible_items <= bundle.catalog_item_sha256.keys()):
        raise ManagementError("r06_snapshot_changed", "frozen model input identity differs", 503)
    # Subset + equal cardinality proves exact full coverage. A same-size set
    # containing a new ID was already rejected, never approved by size alone.
    eligible = context.catalog.eligible_items
    seal = (bundle.full_catalog_seal if len(eligible) == len(bundle.catalog_item_sha256)
            else _seal(bundle, [(item, bundle.catalog_item_sha256[item]) for item in eligible]))
    if model.catalog_sha256 != seal:
        raise ManagementError("r06_snapshot_changed", "frozen model input identity differs", 503)
    return validate_captured_context(bundle, context)


def validate_captured_context(bundle, context):
    """Shape/time guard only; does not authenticate a caller-provided content seal.

    Used after capture_model has verified actual rows, or after restore_request
    has checked the persisted seal. Never sufficient for restoring by itself.
    """
    model = context.model
    if (model is None or context.catalog.bundle_id != bundle.bundle_id
            or model.manifest_sha256 != bundle.manifest_sha256 or model.model_version != bundle.model_version):
        raise ManagementError("r06_snapshot_changed", "captured model identity differs", 503)
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


def capture_model(connection, runtime, session, catalog, rows):
    bundle = runtime.bundle
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
    for row in rows:
        if not row["is_active"]:
            continue
        item_id = row["item_id"]
        approved = runtime.catalog_items[item_id]
        if row["r06_model_text"] != approved.text or row["r06_first_seen_ms"] != approved.first_seen_ms:
            raise ManagementError("r06_catalog_changed", "actual model text/time changed", 409)
        identities.append((item_id, bundle.catalog_item_sha256[item_id]))
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
    # Actual ordered membership, text/time and eligible coverage were checked
    # above. Only their immutable canonical full-set digest is reused.
    seal = (bundle.full_catalog_seal if len(identities) == len(bundle.catalog_item_sha256)
            else _seal(bundle, identities))
    try:
        return ModelSnapshot(bundle.manifest_sha256, bundle.model_version, timestamp, seen, seal)
    except ValueError as error:
        raise ManagementError("r06_input_limit", "full seen input exceeds the serving limits", 422) from error
