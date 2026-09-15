"""Composition root: the one place that selects concrete business adapters."""

from evorec.application.health import ReadinessQuery
from evorec.infrastructure.readiness import UnconfiguredReadiness


def build_readiness_query() -> ReadinessQuery:
    return ReadinessQuery(UnconfiguredReadiness())
