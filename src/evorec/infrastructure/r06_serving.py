"""Stateless frozen-subset snapshot adaptation, not publication or an API backend.

Identity claims come from a trusted future admission/publisher, not from users.
New/edited product representations require another approved bundle; IDs alone
never approve a catalog. No clock, database, request cache or hidden activation.
"""

from dataclasses import dataclass
import hashlib
from uuid import UUID

from evorec.domain.models import CatalogSnapshot, RankedBatch, RequestContext, ScoredCandidate, SessionSnapshot, Strategy
from evorec.infrastructure.r06_features import (
    MAX_ITEMS, R06Features, _ids, _validate_id_values, validate_request,
)
from evorec.infrastructure.r06_retrieval import BINDING_KEYS, R06Retrieval, RetrievalResult
from evorec.infrastructure.residual_ranker import ControlledLoadError, ResidualRanker, _digest

SERVING_POLICY = "r06-a-frozen-subset-v1"


def _fail(code, message):
    raise ControlledLoadError(code, message)


@dataclass(frozen=True)
class FrozenR06Request:
    context: RequestContext
    timestamp_ms: int
    full_seen: frozenset[str]
    features_manifest_sha256: str
    catalog_sha256: str

    def __post_init__(self):
        context = self.context
        if (type(context) is not RequestContext or type(context.session) is not SessionSnapshot
                or type(context.catalog) is not CatalogSnapshot):
            _fail("input_shape", "an immutable admitted request context is required")
        binding = context.binding
        if any(type(value) is not UUID for value in (binding.request_id, binding.session_id, binding.bundle_id)):
            _fail("input_shape", "request, session and bundle IDs must be UUIDs")
        if any(type(value) is not int or value < 0 for value in
               (binding.session_epoch, binding.history_version, binding.exclusion_version)):
            _fail("input_shape", "snapshot versions must be non-negative integers")
        history = _ids(context.session.history, 10_000)
        _, seen, _ = validate_request((), self.full_seen, self.timestamp_ms)
        if not set(history).issubset(seen):
            _fail("input_shape", "full seen set must cover the entire session history, not just its last 50 events")
        if max(len(context.session.hidden_items), len(context.session.favorite_items)) > 10_000:
            _fail("input_shape", "session state exceeds the exclusion limit")
        hidden = _ids(tuple(context.session.hidden_items), 10_000)
        favorites = _ids(tuple(context.session.favorite_items), 10_000)
        excluded = seen | frozenset(hidden) | frozenset(favorites)
        validate_request((), excluded, self.timestamp_ms)
        # Freeze the caller's mutable input and include all current state exclusions.
        object.__setattr__(self, "full_seen", excluded)
        if len(context.catalog.eligible_items) > MAX_ITEMS:
            _fail("resource_limit", "eligible catalog exceeds the frozen limit")
        _validate_id_values(context.catalog.eligible_items)
        _digest(self.features_manifest_sha256)
        _digest(self.catalog_sha256)


@dataclass(frozen=True)
class R06ServingResult:
    batch: RankedBatch
    retrieval: RetrievalResult
    model_version: str
    timestamp_ms: int
    history_input_count: int
    history_used_count: int
    unknown_history_count: int
    serving_policy: str = SERVING_POLICY


@dataclass(frozen=True)
class R06SnapshotRanker:
    bundle_id: UUID
    features: R06Features
    retrieval: R06Retrieval
    ranker: ResidualRanker

    def __post_init__(self):
        if (type(self.bundle_id) is not UUID or type(self.features) is not R06Features
                or type(self.retrieval) is not R06Retrieval or type(self.ranker) is not ResidualRanker):
            _fail("component_schema", "approved frozen component objects and bundle UUID are required")
        for component in (self.features, self.retrieval, self.ranker):
            _digest(component.manifest_sha256)
        if (self.retrieval._features is not self.features
                or self.retrieval.provenance.get("features_manifest_sha256") != self.features.manifest_sha256
                or any(self.retrieval.provenance.get(key) != self.features.provenance.get(key) for key in BINDING_KEYS)
                or self.retrieval.provenance.get("selected_method") != "A-frozen-s17"
                or self.ranker.provenance.get("selected_method") != "A-frozen-s17"):
            _fail("component_changed", "retrieval, features and selected method do not match")
        # Checks all existing ranker bindings even when there are no candidates.
        self.features.build_pool((), (), 0, (), ()).score(self.ranker)

    @property
    def model_version(self):
        identity = (SERVING_POLICY, str(self.bundle_id), self.features.manifest_sha256,
                    self.retrieval.manifest_sha256, self.ranker.manifest_sha256)
        return hashlib.sha256("\n".join(identity).encode("ascii")).hexdigest()

    def score(self, request: FrozenR06Request):
        if type(request) is not FrozenR06Request:
            _fail("input_shape", "a frozen R06 request is required")
        context = request.context
        if (context.catalog.bundle_id != self.bundle_id
                or request.features_manifest_sha256 != self.features.manifest_sha256
                or request.catalog_sha256 != self.features.provenance["catalog_sha256"]):
            _fail("catalog_changed", "request is not bound to the approved frozen catalog and bundle")
        if any(self.features._metadata[self.features._indices[item]].first_seen_ms >= request.timestamp_ms
               for item in context.session.history if item in self.features._indices):
            _fail("input_shape", "known session history is not strictly available at the admitted time")
        history = context.session.history[-50:]  # Unknown events keep their original decay positions.
        result = self.retrieval.retrieve(history, request.full_seen, request.timestamp_ms,
                                         eligible_items=context.catalog.eligible_items)
        pool = self.features.build_pool(history, request.full_seen, request.timestamp_ms,
                                        result.collaborative, result.content)
        scores = pool.score(self.ranker)
        candidates = tuple(ScoredCandidate(item, score, "r06-a-frozen-s17")
                           for item, score in zip(pool.item_ids, scores, strict=True))
        return R06ServingResult(
            RankedBatch(context.binding, Strategy.DENSE, candidates), result, self.model_version,
            request.timestamp_ms, len(context.session.history), len(history),
            sum(item not in self.features._indices for item in history),
        )
