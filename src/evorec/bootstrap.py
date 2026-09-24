"""Composition root: the one place that selects concrete business adapters."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from evorec.application.health import ReadinessQuery
from evorec.application.ports import DemoBackendPort
from evorec.application.recommend import Recommend
from evorec.infrastructure.memory import InMemoryDemoBackend
from evorec.infrastructure.readiness import UnconfiguredReadiness

if TYPE_CHECKING:
    from evorec.infrastructure.management import CatalogManager


@dataclass(frozen=True)
class DemoApplication:
    backend: DemoBackendPort
    recommend: Recommend
    readiness: ReadinessQuery
    persistent: bool
    manager: "CatalogManager | None" = None


def build_demo_application(
    backend: DemoBackendPort | None = None,
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
        from evorec.infrastructure.management import CatalogManager

        if not isinstance(backend, PostgresDemoBackend):
            raise TypeError("unsupported persistent demo backend")
        root = os.getenv("EVOREC_BUNDLE_ROOT")
        manager = CatalogManager(backend, Path(root) if root else None)
        backend.manager = manager
        readiness = ReadinessQuery(PostgresDemoReadiness(backend))
    else:
        manager = None
        readiness = ReadinessQuery(UnconfiguredReadiness())
    return DemoApplication(backend, Recommend(backend, backend, backend), readiness,
                           persistent, manager)
