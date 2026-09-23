"""Composition root: the one place that selects concrete business adapters."""

import os
from dataclasses import dataclass

from evorec.application.health import ReadinessQuery
from evorec.application.ports import SessionPort
from evorec.application.recommend import Recommend
from evorec.infrastructure.memory import InMemoryDemoBackend
from evorec.infrastructure.readiness import UnconfiguredReadiness


@dataclass(frozen=True)
class DemoApplication:
    backend: SessionPort
    recommend: Recommend
    readiness: ReadinessQuery
    persistent: bool


def build_demo_application(
    backend: SessionPort | None = None,
) -> DemoApplication:
    database_url = os.getenv("EVOREC_DATABASE_URL")
    if backend is None:
        if database_url:
            from evorec.infrastructure.postgres import PostgresDemoBackend

            backend = PostgresDemoBackend(database_url)
        else:
            backend = InMemoryDemoBackend()

    persistent = not isinstance(backend, InMemoryDemoBackend)
    if persistent:
        from evorec.infrastructure.postgres import PostgresDemoBackend, PostgresDemoReadiness

        if not isinstance(backend, PostgresDemoBackend):
            raise TypeError("unsupported persistent demo backend")
        readiness = ReadinessQuery(PostgresDemoReadiness(backend.database_url))
    else:
        readiness = ReadinessQuery(UnconfiguredReadiness())
    return DemoApplication(backend, Recommend(backend, backend, backend), readiness, persistent)
