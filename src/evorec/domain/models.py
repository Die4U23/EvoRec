"""Immutable request snapshots. Large catalog sets can be shared between requests."""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from math import isfinite
from uuid import UUID


class Strategy(StrEnum):
    POPULAR = "popular"
    DENSE = "dense"
    GENERATIVE = "generative"
    HYBRID = "hybrid"
    ADAPTIVE = "adaptive"


class FeedbackKind(StrEnum):
    DETAIL_VIEW = "detail_view"
    EXPOSURE = "exposure"
    FAVORITE_SET = "favorite_set"
    HIDE_SET = "hide_set"


@dataclass(frozen=True)
class RecommendationCommand:
    request_id: UUID
    session_id: UUID
    session_token: str
    expected_history_version: int
    strategy: Strategy
    k: int
    timeout_seconds: float

    def __post_init__(self) -> None:
        if not self.session_token or not self.session_token.strip():
            raise ValueError("session token must be present")
        if type(self.k) is not int or not 1 <= self.k <= 50:
            raise ValueError("k must be an integer between 1 and 50")
        if type(self.expected_history_version) is not int or self.expected_history_version < 0:
            raise ValueError("history version must be a non-negative integer")
        if isinstance(self.timeout_seconds, bool) or not isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("timeout must be finite and positive")
        object.__setattr__(self, "strategy", Strategy(self.strategy))


@dataclass(frozen=True)
class CreatedSession:
    snapshot: "SessionSnapshot"
    access_token: str

    def __post_init__(self) -> None:
        if not self.access_token or not self.access_token.strip():
            raise ValueError("session access token must be present")


@dataclass(frozen=True)
class FeedbackCommand:
    event_id: UUID
    session_id: UUID
    session_token: str
    request_id: UUID
    item_id: str
    kind: FeedbackKind
    observed_at: datetime
    desired_state: bool | None = None
    visible_ratio: float | None = None
    visible_duration_ms: int | None = None
    schema_version: str = "0.1"

    def __post_init__(self) -> None:
        if not self.session_token or not self.session_token.strip():
            raise ValueError("session token must be present")
        if not self.item_id:
            raise ValueError("item id must be present")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must include a timezone")
        object.__setattr__(self, "kind", FeedbackKind(self.kind))
        state_event = self.kind in {FeedbackKind.FAVORITE_SET, FeedbackKind.HIDE_SET}
        if state_event and type(self.desired_state) is not bool:
            raise ValueError("state-setting feedback requires a boolean desired state")
        if not state_event and self.desired_state is not None:
            raise ValueError("this feedback kind does not accept desired state")
        if self.kind == FeedbackKind.EXPOSURE:
            ratio = self.visible_ratio
            duration = self.visible_duration_ms
            if (
                type(ratio) is not float
                or not isfinite(ratio)
                or not 0.5 <= ratio <= 1
                or type(duration) is not int
                or duration < 1000
            ):
                raise ValueError("exposure evidence does not meet the visibility threshold")
        elif self.visible_ratio is not None or self.visible_duration_ms is not None:
            raise ValueError("visibility evidence is only accepted for exposure")
        if self.schema_version != "0.1":
            raise ValueError("unsupported feedback schema version")

    @property
    def payload_sha256(self) -> str:
        payload = {
            "desired_state": self.desired_state,
            "event_id": str(self.event_id),
            "item_id": self.item_id,
            "kind": self.kind.value,
            "observed_at": self.observed_at.astimezone(timezone.utc).isoformat(
                timespec="microseconds"
            ),
            "request_id": str(self.request_id),
            "schema_version": self.schema_version,
            "session_id": str(self.session_id),
            "visible_duration_ms": self.visible_duration_ms,
            "visible_ratio": self.visible_ratio,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class FeedbackResult:
    event_id: UUID
    session_id: UUID
    session_epoch: int
    history_version: int
    replayed: bool


@dataclass(frozen=True)
class SessionSnapshot:
    session_id: UUID
    epoch: int
    history_version: int
    history: tuple[str, ...]
    hidden_items: frozenset[str]

    def __post_init__(self) -> None:
        if self.epoch < 0 or self.history_version < 0:
            raise ValueError("session versions cannot be negative")
        object.__setattr__(self, "history", tuple(self.history))
        object.__setattr__(self, "hidden_items", frozenset(self.hidden_items))


@dataclass(frozen=True)
class CatalogSnapshot:
    bundle_id: UUID
    exclusion_version: int
    eligible_items: frozenset[str]

    def __post_init__(self) -> None:
        if self.exclusion_version < 0:
            raise ValueError("exclusion version cannot be negative")
        object.__setattr__(self, "eligible_items", frozenset(self.eligible_items))


@dataclass(frozen=True)
class RequestBinding:
    request_id: UUID
    session_id: UUID
    session_epoch: int
    history_version: int
    bundle_id: UUID
    exclusion_version: int


@dataclass(frozen=True)
class RequestContext:
    request_id: UUID
    session: SessionSnapshot
    catalog: CatalogSnapshot

    @property
    def binding(self) -> RequestBinding:
        return RequestBinding(
            self.request_id, self.session.session_id, self.session.epoch,
            self.session.history_version, self.catalog.bundle_id, self.catalog.exclusion_version,
        )


@dataclass(frozen=True)
class ScoredCandidate:
    item_id: str
    score: float
    source: str

    def __post_init__(self) -> None:
        if not self.item_id or not self.source:
            raise ValueError("candidate item and source must be present")
        if isinstance(self.score, bool) or not isfinite(self.score):
            raise ValueError("candidate score must be finite")


@dataclass(frozen=True)
class RankedBatch:
    binding: RequestBinding
    actual_strategy: Strategy
    candidates: tuple[ScoredCandidate, ...]
    fallback_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "actual_strategy", Strategy(self.actual_strategy))
        object.__setattr__(self, "candidates", tuple(self.candidates))
        if self.actual_strategy == Strategy.ADAPTIVE:
            raise ValueError("adaptive routing must resolve to an actual execution strategy")
        if self.fallback_reason is not None and not self.fallback_reason.strip():
            raise ValueError("fallback reason cannot be blank")


@dataclass(frozen=True)
class RecommendationResult:
    binding: RequestBinding
    requested_strategy: Strategy
    actual_strategy: Strategy
    items: tuple[ScoredCandidate, ...]
    fallback_reason: str | None


@dataclass(frozen=True)
class ReadinessReport:
    blockers: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "blockers", tuple(self.blockers))

    @property
    def ready(self) -> bool:
        return not self.blockers
