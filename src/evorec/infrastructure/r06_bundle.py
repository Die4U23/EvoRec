"""Self-contained, hash-pinned frozen R06 packages; no database or activation.

Content identity is the original research text, not a merchant title or a claim
from a client. A future trusted admission layer must supply the actual snapshot.
"""

from dataclasses import dataclass, field, replace
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType
from typing import Mapping
from uuid import UUID

from evorec.infrastructure.content_encoder import ContentEncoder, load_content_encoder
from evorec.infrastructure.r06_features import MAX_ITEMS, MAX_JSON_BYTES, _ids, _json, _timestamp, load_r06_features
from evorec.infrastructure.r06_retrieval import load_r06_retrieval
from evorec.infrastructure.r06_serving import FrozenR06Request, R06SnapshotRanker, SERVING_POLICY
from evorec.infrastructure.residual_ranker import ControlledLoadError, _digest, _object, _read, _verified, load_residual_ranker

KIND = "r06-frozen-bundle-v1"
FILES = MappingProxyType({
    "features": ("manifest.json", "items.json", "vectors.f32", "validation.json"),
    "retrieval": ("manifest.json", "statistics.json", "neighbors.bin", "validation.json"),
    "ranker": ("manifest.json", "weights.f32", "validation.json"),
    "encoder": ("manifest.json", "vocabulary.json", "weights.f32", "validation.json"),
})
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_FILE_BYTES = 128 * 1024 * 1024
MAX_MANIFEST_BYTES = 16 * 1024
CATALOG_POLICY = "original-text-and-first-seen-v1"


def _fail(code, message):
    raise ControlledLoadError(code, message)


def _root(managed_root, bundle_dir):
    supplied = Path(bundle_dir)
    try:
        root, candidate = Path(managed_root).resolve(strict=True), supplied.resolve(strict=True)
        if not root.is_dir() or not candidate.is_dir() or supplied.absolute().parent != root:
            _fail("unsafe_path", "bundle must be a direct child of the managed root")
        if _link(supplied) or candidate.parent != root:
            _fail("unsafe_path", "bundle directory cannot be a link")
        return candidate
    except OSError as error:
        raise ControlledLoadError("component_unreadable", "cannot resolve bundle root") from error


def _link(path):
    return path.is_symlink() or path.is_junction()


def _entries(root, expected):
    entries = []
    for path in root.iterdir():
        entries.append(path)
        if len(entries) > len(expected):
            break
    if {p.name for p in entries} != set(expected) or len(entries) != len(expected):
        _fail("unsupported_format", "unlisted or missing frozen files")
    return entries


def _inventory(root):
    # Bounded enumeration, no arbitrary recursive traversal or path from JSON.
    expected = {"manifest.json", "catalog.json", "metadata.json", *FILES}
    try:
        children = _entries(root, expected)
        total = 0
        for child in children:
            if _link(child):
                _fail("unsafe_path", "bundle cannot contain links")
            entries = [child]
            if child.name in FILES:
                if not child.is_dir():
                    _fail("unsafe_path", "component must be a directory")
                entries = _entries(child, FILES[child.name])
            for entry in entries:
                if _link(entry) or not entry.is_file():
                    _fail("unsafe_path", "bundle files must be regular local files")
                size = entry.stat().st_size
                if size > MAX_FILE_BYTES:
                    _fail("resource_limit", "bundle file exceeds the byte limit")
                total += size
                if total > MAX_TOTAL_BYTES:
                    _fail("resource_limit", "bundle exceeds the total byte limit")
    except OSError as error:
        raise ControlledLoadError("component_unreadable", "cannot enumerate bundle") from error


@dataclass(frozen=True)
class FrozenCatalogItem:
    item_id: str
    text: str
    first_seen_ms: int


def _item_digest(item):
    if type(item) is not FrozenCatalogItem:
        _fail("input_shape", "actual frozen catalog records are required")
    _ids((item.item_id,), 1)
    _timestamp(item.first_seen_ms)
    if not isinstance(item.text, str) or len(item.text) > 32_768:
        _fail("input_shape", "original item text must be bounded Unicode")
    try:
        raw = json.dumps([item.item_id, item.first_seen_ms, item.text], ensure_ascii=False,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    except UnicodeEncodeError as error:
        raise ControlledLoadError("input_shape", "item text must be valid UTF-8") from error
    return hashlib.sha256(raw).hexdigest()


def catalog_seal(manifest_sha256, identities):
    """The existing canonical persisted identity, independent of row order."""
    raw = json.dumps([KIND, manifest_sha256, sorted(identities)],
                     ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class FrozenR06Bundle:
    bundle_id: UUID
    manifest_sha256: str
    adapter: R06SnapshotRanker
    encoder: ContentEncoder
    catalog_item_sha256: Mapping[str, str]  # Text is not kept after load.
    full_catalog_seal: str = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        # Own the mapping before caching: even a caller's mutable dict cannot
        # change the identities after construction. No request/eligibility cache.
        identities = dict(self.catalog_item_sha256)
        object.__setattr__(self, "catalog_item_sha256", MappingProxyType(identities))
        object.__setattr__(self, "full_catalog_seal", catalog_seal(self.manifest_sha256, identities.items()))

    @property
    def model_version(self):
        return hashlib.sha256((KIND + "\n" + self.manifest_sha256).encode("ascii")).hexdigest()

    def request(self, context, timestamp_ms, full_seen, catalog_items):
        """Bind a trusted actual eligible catalog snapshot, never a client hash."""
        request = FrozenR06Request(context, timestamp_ms, full_seen,
                                   self.adapter.features.manifest_sha256,
                                   self.adapter.features.provenance["catalog_sha256"])
        if context.catalog.bundle_id != self.bundle_id:
            _fail("catalog_changed", "snapshot bundle UUID differs")
        if not isinstance(catalog_items, (tuple, list)) or len(catalog_items) > MAX_ITEMS:
            _fail("input_shape", "bounded actual catalog records are required")
        ids = set()
        for item in catalog_items:
            digest = _item_digest(item)
            if item.item_id in ids or self.catalog_item_sha256.get(item.item_id) != digest:
                _fail("catalog_changed", "catalog has duplicate, new or edited item representations")
            ids.add(item.item_id)
        if ids != context.catalog.eligible_items:
            _fail("catalog_changed", "actual records must exactly cover the eligible snapshot")
        return request

    def score(self, context, timestamp_ms, full_seen, catalog_items):
        result = self.adapter.score(self.request(context, timestamp_ms, full_seen, catalog_items))
        return replace(result, model_version=self.model_version)


def load_r06_bundle(managed_root, bundle_dir, *, expected_manifest_sha256,
                    content_backend="stdlib", ranker_backend="stdlib"):
    """Approval is mandatory; all components and raw sources are revalidated."""
    approved = _digest(expected_manifest_sha256)
    root = _root(managed_root, bundle_dir)
    _inventory(root)
    raw = _read(root, "manifest.json", MAX_MANIFEST_BYTES)
    digest = hashlib.sha256(raw).hexdigest()
    if digest != approved:
        _fail("component_changed", "bundle manifest differs from approval")
    manifest = _object(_json(raw), ("schema_version", "kind", "bundle_id", "serving_policy",
                                  "catalog_policy", "components", "catalog", "metadata", "assembly_revision"))
    if (type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1
            or manifest["kind"] != KIND or manifest["serving_policy"] != SERVING_POLICY
            or manifest["catalog_policy"] != CATALOG_POLICY):
        _fail("unsupported_format", "unsupported frozen bundle policy")
    _digest(manifest["assembly_revision"], 40)
    try:
        bundle_id = UUID(manifest["bundle_id"])
    except (ValueError, AttributeError, TypeError) as error:
        raise ControlledLoadError("component_schema", "bundle UUID must be canonical") from error
    if str(bundle_id) != manifest["bundle_id"] or root.name != str(bundle_id):
        _fail("bundle_identity_mismatch", "bundle directory and canonical UUID must match")
    digests = _object(manifest["components"], FILES)
    for value in digests.values():
        _digest(value)
    features = load_r06_features(root / "features", expected_manifest_sha256=digests["features"])
    retrieval = load_r06_retrieval(root / "retrieval", features,
                                   expected_manifest_sha256=digests["retrieval"], content_backend=content_backend)
    ranker = load_residual_ranker(root / "ranker", expected_manifest_sha256=digests["ranker"], cpu_backend=ranker_backend)
    encoder = load_content_encoder(root / "encoder", expected_manifest_sha256=digests["encoder"])
    encoder.check_features(features)
    adapter = R06SnapshotRanker(bundle_id, features, retrieval, ranker)
    catalog = _json(_verified(root, manifest["catalog"], "catalog.json", MAX_JSON_BYTES))
    metadata = _json(_verified(root, manifest["metadata"], "metadata.json", MAX_JSON_BYTES))
    if (manifest["catalog"]["sha256"] != features.provenance["catalog_sha256"]
            or manifest["metadata"]["sha256"] != encoder.provenance["metadata_sha256"]):
        _fail("component_changed", "original catalog or text source differs from component provenance")
    if (not isinstance(catalog, dict) or set(catalog) != set(features.item_ids)
            or not isinstance(metadata, dict) or not set(metadata).issubset(catalog)):
        _fail("catalog_changed", "source catalog keys must match the complete frozen feature mapping")
    identities = {}
    for index, item in enumerate(features.item_ids):
        if catalog[item] != features._metadata[index].first_seen_ms:
            _fail("catalog_changed", "source first-seen time differs from frozen features")
        identities[item] = _item_digest(FrozenCatalogItem(item, metadata.get(item, ""), catalog[item]))
    return FrozenR06Bundle(bundle_id, digest, adapter, encoder, MappingProxyType(identities))


def assemble_r06_bundle(managed_root, bundle_id, component_dirs, approved_manifest_sha256,
                        catalog_path, metadata_path, *, assembly_revision):
    """Copy approved bytes, validate staging, write the final manifest last.

    New destinations only. Failure leaves diagnostics without a loadable manifest;
    it never overwrites an existing package, even from a competing writer.
    """
    if type(bundle_id) is not UUID:
        _fail("input_shape", "bundle ID must be a UUID")
    _object(component_dirs, FILES)
    _object(approved_manifest_sha256, FILES)
    for value in approved_manifest_sha256.values():
        _digest(value)
    _digest(assembly_revision, 40)
    root = Path(managed_root).resolve(strict=True)
    if not root.is_dir():
        _fail("unsafe_path", "managed root must be a directory")
    target = root / str(bundle_id)
    if target.exists() or _link(target):
        raise FileExistsError("bundle destination already exists")
    with TemporaryDirectory(prefix="r06-assembly-", dir=root) as temporary:
        staging = Path(temporary) / str(bundle_id)
        staging.mkdir()
        total = 0
        for role, names in FILES.items():
            supplied = Path(component_dirs[role])
            if _link(supplied):
                _fail("unsafe_path", "source components cannot be links")
            source = supplied.resolve(strict=True)
            _entries(source, names)
            destination = staging / role
            destination.mkdir()
            for name in names:
                raw = _read(source, name, MAX_FILE_BYTES)
                total += len(raw)
                if total > MAX_TOTAL_BYTES:
                    _fail("resource_limit", "assembly exceeds byte limit")
                (destination / name).write_bytes(raw)
        manifest = dict(schema_version=1, kind=KIND, bundle_id=str(bundle_id), serving_policy=SERVING_POLICY,
                        catalog_policy=CATALOG_POLICY, components=dict(approved_manifest_sha256),
                        assembly_revision=assembly_revision)
        for key, supplied in (("catalog", catalog_path), ("metadata", metadata_path)):
            supplied = Path(supplied)
            if _link(supplied):
                _fail("unsafe_path", "source snapshots cannot be links")
            raw = _read(supplied.resolve(strict=True).parent, supplied.name, MAX_JSON_BYTES)
            total += len(raw)
            if total > MAX_TOTAL_BYTES:
                _fail("resource_limit", "assembly exceeds byte limit")
            name = key + ".json"
            (staging / name).write_bytes(raw)
            manifest[key] = dict(path=name, size_bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
        raw = json.dumps(manifest, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        (staging / "manifest.json").write_bytes(raw)
        digest = hashlib.sha256(raw).hexdigest()
        load_r06_bundle(Path(temporary), staging, expected_manifest_sha256=digest)
        target.mkdir(exist_ok=False)  # Exclusive ownership, no rename-over-empty-directory race.
        owned_manifest = False
        try:
            for role, names in FILES.items():
                (target / role).mkdir()
                for name in names:
                    with (target / role / name).open("xb") as stream:
                        stream.write((staging / role / name).read_bytes())
            for name in ("catalog.json", "metadata.json"):
                with (target / name).open("xb") as stream:
                    stream.write((staging / name).read_bytes())
            with (target / "manifest.json").open("xb") as stream:
                owned_manifest = True
                stream.write(raw)
        except BaseException:
            if owned_manifest:
                (target / "manifest.json").unlink(missing_ok=True)
            raise
        return digest
