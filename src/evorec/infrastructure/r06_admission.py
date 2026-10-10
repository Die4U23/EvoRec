"""Trusted PostgreSQL capture and pinned restoration, never HTTP input hashes.

The content seal binds actual UTF-8 text hashes and literal time to the approved
immutable package at capture, assuming SHA-256 collision resistance. SQL hashes
actual current text, never a mutable stored digest. Restoration uses the stored
seal, not mutable current item rows.
Database ownership is the trust boundary, not a cryptographic signature.
"""

from dataclasses import dataclass, field
import hashlib
from types import MappingProxyType
from typing import Mapping

from evorec.domain.errors import ManagementError
from evorec.domain.models import ModelSnapshot
from evorec.infrastructure.r06_bundle import KIND, FrozenCatalogItem, FrozenR06Bundle, _item_digest, catalog_seal
from evorec.infrastructure.r06_serving import FrozenR06Request
from evorec.infrastructure.residual_ranker import ControlledLoadError
from evorec.infrastructure.r06_catalog_capture import catalog_source_digests, capture_eligible


@dataclass(frozen=True)
class ManagedR06Runtime:
    bundle: FrozenR06Bundle
    catalog_items: Mapping[str, FrozenCatalogItem]
    catalog_text_sha256: Mapping[str, bytes] = field(init=False, repr=False, compare=False)
    member_sha256: bytes = field(init=False, repr=False, compare=False)
    full_active_sha256: bytes = field(init=False, repr=False, compare=False)
    full_eligible_items: frozenset[str] = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        # Pay approved-source hashing once at load. Requests hash actual current
        # DB UTF-8 text, not a stored digest column or a cached client claim.
        records = dict(self.catalog_items)
        if (type(self.bundle) is not FrozenR06Bundle
                or set(records) != set(self.bundle.adapter.features.item_ids)
                or any(type(item) is not FrozenCatalogItem or item.item_id != key
                       or _item_digest(item) != self.bundle.catalog_item_sha256[key]
                       for key, item in records.items())):
            raise ManagementError("r06_catalog_changed", "approved runtime source table differs", 422)
        object.__setattr__(self, "catalog_items", MappingProxyType(records))
        object.__setattr__(self, "catalog_text_sha256", MappingProxyType({
            key: hashlib.sha256(item.text.encode("utf-8")).digest() for key, item in records.items()
        }))
        members, active = catalog_source_digests(self.item_ids, records, self.catalog_text_sha256)
        object.__setattr__(self, "member_sha256", members)
        object.__setattr__(self, "full_active_sha256", active)
        object.__setattr__(self, "full_eligible_items", frozenset(records))

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


def _ordered_membership_matches(rows, expected_ids):
    """Check trusted materialized row/ID sequences; inactive members still count."""
    if len(rows) != len(expected_ids):
        return False
    return all(row.item_id == expected_ids[index] and row.internal_item_id == index
               for index, row in enumerate(rows))


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


def capture_model(connection, runtime, session, catalog, capture):
    bundle = runtime.bundle
    row = connection.execute(
        "SELECT runtime_kind, manifest_sha256 FROM bundle_versions WHERE bundle_id = %s",
        (catalog.bundle_id,),
    ).fetchone()
    if (row is None or row["runtime_kind"] != KIND
            or (row["manifest_sha256"] or "").strip() != bundle.manifest_sha256):
        raise ManagementError("r06_snapshot_changed", "registered model identity differs", 503)
    eligible = capture_eligible(runtime, capture)
    if eligible != catalog.eligible_items:
        raise ManagementError("r06_catalog_changed", "eligible catalog changed during admission", 409)
    seen = set(session.history) | session.hidden_items | session.favorite_items
    seen.update(row["item_id"] for row in connection.execute(
        "SELECT DISTINCT item_id FROM feedback_events WHERE session_id = %s AND session_epoch = %s LIMIT 10001",
        (session.session_id, session.epoch),
    ).fetchall())
    timestamp = connection.execute(
        "SELECT floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint AS timestamp_ms",
    ).fetchone()["timestamp_ms"]
    # Actual ordered membership and active text/time were checked in one SQL
    # view, against immutable approved source fingerprints, not stored hashes.
    seal = (bundle.full_catalog_seal if capture.active_count == len(bundle.catalog_item_sha256)
            else _seal(bundle, [(item, bundle.catalog_item_sha256[item]) for item in catalog.eligible_items]))
    try:
        return ModelSnapshot(bundle.manifest_sha256, bundle.model_version, timestamp, seen, seal)
    except ValueError as error:
        raise ManagementError("r06_input_limit", "full seen input exceeds the serving limits", 422) from error
