"""Composition root: the one place that selects concrete business adapters."""

from dataclasses import dataclass

from evorec.application.health import ReadinessQuery
from evorec.application.recommend import Recommend
from evorec.infrastructure.memory import InMemoryDemoBackend
from evorec.infrastructure.readiness import UnconfiguredReadiness


@dataclass(frozen=True)
class DemoApplication:
    backend: InMemoryDemoBackend
    recommend: Recommend


def build_readiness_query() -> ReadinessQuery:
    return ReadinessQuery(UnconfiguredReadiness())


def build_demo_application(backend: InMemoryDemoBackend | None = None) -> DemoApplication:
    backend = backend or InMemoryDemoBackend()
    return DemoApplication(backend, Recommend(backend, backend, backend))
