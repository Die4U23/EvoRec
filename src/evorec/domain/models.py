"""Immutable request snapshots. Large catalog sets can be shared between requests."""

from dataclasses import dataclass
from enum import StrEnum
from math import isfinite
from uuid import UUID


class Strategy(StrEnum):
    POPULAR = "popular"
    DENSE = "dense"
    GENERATIVE = "generative"
    HYBRID = "hybrid"
    ADAPTIVE = "adaptive"


@dataclass(frozen=True)
class RecommendationCommand:
    request_id: UUID
    session_id: UUID
    expected_history_version: int
    strategy: Strategy
    k: int
    timeout_seconds: float

    def __post_init__(self) -> None:
        if type(self.k) is not int or not 1 <= self.k <= 50:
            raise ValueError("k must be an integer between 1 and 50")
        if type(self.expected_history_version) is not int or self.expected_history_version < 0:
            raise ValueError("history version must be a non-negative integer")
        if isinstance(self.timeout_seconds, bool) or not isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("timeout must be finite and positive")
        object.__setattr__(self, "strategy", Strategy(self.strategy))


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
