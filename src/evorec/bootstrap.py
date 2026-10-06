"""Composition root: the one place that selects concrete business adapters."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from evorec.application.health import ReadinessQuery
from evorec.application.compare import CompareStrategies
from evorec.application.ports import DemoBackendPort
from evorec.application.recommend import Recommend
from evorec.infrastructure.memory import InMemoryDemoBackend
from evorec.infrastructure.readiness import UnconfiguredReadiness

if TYPE_CHECKING:
    from evorec.infrastructure.management import CatalogManager
    from evorec.infrastructure.comparison_job import ComparisonJobService
    from evorec.infrastructure.evaluation_job import EvaluationJobService


@dataclass(frozen=True)
class DemoApplication:
    backend: DemoBackendPort
    recommend: Recommend
    compare: CompareStrategies
    readiness: ReadinessQuery
    persistent: bool
    manager: "CatalogManager | None" = None
    comparison_jobs: "ComparisonJobService | None" = None
    evaluation_jobs: "EvaluationJobService | None" = None


def build_demo_application(
    backend: DemoBackendPort | None = None,
    *, managed_root: Path | None = None,
) -> DemoApplication:
    database_url = os.getenv("EVOREC_DATABASE_URL")
    if backend is None:
        if database_url:
            from evorec.infrastructure.postgres import PostgresDemoBackend

            backend = PostgresDemoBackend(database_url)
        else:
            backend = InMemoryDemoBackend()

    persistent = not isinstance(backend, InMemoryDemoBackend)
    comparison_records = None
    if persistent:
        from evorec.infrastructure.postgres import PostgresDemoBackend, PostgresDemoReadiness
        from evorec.infrastructure.management import CatalogManager
        from evorec.infrastructure.comparison_store import PostgresComparisonStore

        if not isinstance(backend, PostgresDemoBackend):
            raise TypeError("unsupported persistent demo backend")
        root = os.getenv("EVOREC_BUNDLE_ROOT")
        manager = CatalogManager(backend, managed_root if managed_root is not None else Path(root) if root else None)
        backend.manager = manager
        readiness = ReadinessQuery(PostgresDemoReadiness(backend))
        comparison_records = PostgresComparisonStore(backend)
    else:
        manager = None
        readiness = ReadinessQuery(UnconfiguredReadiness())
    compare = CompareStrategies(backend, backend, comparison_records)
    jobs = None
    evaluations = None
    if comparison_records is not None:
        from evorec.infrastructure.comparison_job import ComparisonJobService

        jobs = ComparisonJobService(compare, comparison_records)
        from evorec.infrastructure.evaluation_job import EvaluationJobService
        evaluations = EvaluationJobService(compare, comparison_records)
    return DemoApplication(backend, Recommend(backend, backend, backend), compare, readiness,
                           persistent, manager, jobs, evaluations)
