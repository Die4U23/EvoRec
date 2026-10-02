"""API for the local recommendation and controlled catalog workflow."""

import asyncio
import hmac
import os
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from fastapi import FastAPI, Header, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict
import psycopg

from evorec import __version__
from evorec.application.health import ReadinessQuery
from evorec.application.compare import ComparisonCommand
from evorec.api.catalog_file import (
    MAX_FILE_BYTES, CatalogFileError, parse_catalog_file,
)
from evorec.bootstrap import DemoApplication, build_demo_application
from evorec.contracts import (
    CatalogImportInput, ComparisonPreviewInput, FeedbackInput, RecommendationInput,
)
from evorec.domain.errors import (
    AccessDenied,
    ComparisonStorageUnavailable,
    FeedbackSourceMismatch,
    HistoryConflict,
    IdempotencyConflict,
    IdempotencyInProgress,
    IdempotencyReplay,
    ManagementError,
    ResourceNotFound,
    SessionEpochConflict,
)
from evorec.domain.models import (
    CreatedSession,
    FeedbackCommand,
    FeedbackResult,
    RecommendationCommand,
    RecommendationResult,
    SessionSnapshot,
    Strategy,
)


class Liveness(BaseModel):
    status: Literal["alive"] = "alive"
    component: Literal["api"] = "api"
    version: str = __version__


class Readiness(BaseModel):
    ready: bool
    blockers: list[str]


class Capabilities(BaseModel):
    health_checks: Literal[True] = True
    input_contracts: Literal[True] = True
    persistence: bool = False
    recommendations: Literal[True] = True
    catalog_publication: bool = False
    model_training: Literal[False] = False
    frontend: Literal[True] = True


class SystemInfo(BaseModel):
    name: Literal["EvoRec"] = "EvoRec"
    version: str = __version__
    phase: Literal["M23-local-managed-demo"] = "M23-local-managed-demo"
    capabilities: Capabilities


class SessionResponse(BaseModel):
    session_id: UUID
    epoch: int
    history_version: int
    history: list[str]
    hidden_items: list[str]
    favorite_items: list[str]
    profile_id: str


class SessionCreateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile_id: Literal["new", "sample"] = "new"


class SessionCreatedResponse(SessionResponse):
    access_token: str


class RecommendationItemResponse(BaseModel):
    item_id: str
    score: float
    source: str


class RecommendationResponse(BaseModel):
    request_id: UUID
    session_id: UUID
    session_epoch: int
    history_version: int
    bundle_id: UUID
    exclusion_version: int
    requested_strategy: str
    actual_strategy: str
    fallback_reason: str | None
    items: list[RecommendationItemResponse]


class ComparedStrategyResponse(BaseModel):
    requested_strategy: str
    actual_strategy: str
    fallback_reason: str | None
    elapsed_ms: float
    items: list[RecommendationItemResponse]
    unique_item_ids: list[str]


class ComparisonPreviewResponse(BaseModel):
    comparison_id: UUID
    session_id: UUID
    session_epoch: int
    history_version: int
    bundle_id: UUID
    exclusion_version: int
    snapshot_at: datetime
    common_item_ids: list[str]
    strategies: list[ComparedStrategyResponse]
    persisted: Literal[False] = False


class ComparisonInputSnapshotResponse(BaseModel):
    history: list[str]
    hidden_items: list[str]
    favorite_items: list[str]
    profile_id: str
    eligible_items: list[str]


class ComparisonSavedResponse(ComparisonPreviewResponse):
    persisted: Literal[True] = True
    status: Literal["completed"] = "completed"
    requested_k: int
    input_snapshot: ComparisonInputSnapshotResponse


class ComparisonSummaryResponse(BaseModel):
    comparison_id: UUID
    session_id: UUID
    history_version: int
    bundle_id: UUID
    snapshot_at: datetime
    requested_k: int
    requested_strategies: list[str]
    actual_strategies: list[str]


class ComparisonPageResponse(BaseModel):
    items: list[ComparisonSummaryResponse]
    offset: int
    limit: int
    has_more: bool


class ComparisonJobResponse(BaseModel):
    job_id: UUID
    session_id: UUID
    status: Literal['queued', 'running', 'cancelling', 'completed', 'cancelled', 'failed']
    completed_strategies: int
    total_strategies: int
    attempts: int
    cancel_requested: bool
    error_code: str | None
    comparison_id: UUID | None
    bundle_id: UUID
    history_version: int
    snapshot_at: datetime
    created_at: datetime
    updated_at: datetime


class FeedbackResponse(BaseModel):
    event_id: UUID
    session_id: UUID
    session_epoch: int
    history_version: int
    replayed: bool
    exposure_event_id: UUID | None


class PublicationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: UUID
    expected_active_bundle_id: UUID | None


class CatalogItemResponse(BaseModel):
    item_id: str
    title: str
    category: str
    description: str
    image_url: str | None
    is_active: bool


class CatalogImportResponse(BaseModel):
    batch_id: UUID
    item_count: int
    replayed: bool


class CatalogBuildInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    build_id: UUID


class CatalogBuildPublishInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: UUID


class CatalogBuildResponse(BaseModel):
    build_id: UUID
    batch_id: UUID
    bundle_id: UUID
    base_bundle_id: UUID | None
    status: Literal["queued", "processing", "ready", "failed"]
    total_count: int
    processed_count: int
    failed_count: int
    attempts: int
    error_code: str | None
    publication_status: str | None


class CatalogImportStateResponse(BaseModel):
    batch_id: UUID
    item_count: int
    imported_at: datetime
    snapshot_available: bool
    latest_build: CatalogBuildResponse | None


class CatalogPreviewItem(CatalogItemResponse):
    currently_recommendable: bool
    ready_for_publication: bool


class CatalogPreviewResponse(BaseModel):
    build_id: UUID
    total_count: int
    offset: int
    items: list[CatalogPreviewItem]


class ItemDeactivationResponse(BaseModel):
    item_id: str
    is_active: bool
    exclusion_version: int


class BundleRegistrationResponse(BaseModel):
    bundle_id: UUID
    manifest_sha256: str
    item_count: int
    replayed: bool


class PendingPublication(BaseModel):
    operation_id: UUID
    status: str


class PublicationStateResponse(BaseModel):
    active_bundle_id: UUID | None
    exclusion_version: int
    admission_open: bool
    pending_operation: PendingPublication | None


class PublicationResponse(BaseModel):
    operation_id: UUID
    active_bundle_id: UUID
    status: Literal["completed"]
    replayed: bool


class ErrorDetail(BaseModel):
    code: str
    message: str
    retryable: bool
    request_id: UUID | None


class ErrorEnvelope(BaseModel):
    error: ErrorDetail


class CatalogFileRowError(BaseModel):
    row: int
    field: str
    reason: str


class CatalogFileErrorResponse(ErrorEnvelope):
    rows: list[CatalogFileRowError]


class CatalogFileJobResponse(BaseModel):
    batch_id: UUID
    status: Literal["queued", "validating", "imported", "failed"]
    item_count: int
    error_code: str | None
    row_errors: list[CatalogFileRowError]
    attempts: int
    created_at: datetime
    updated_at: datetime
    replayed: bool = False


def _session_response(snapshot: SessionSnapshot) -> SessionResponse:
    return SessionResponse(
        session_id=snapshot.session_id,
        epoch=snapshot.epoch,
        history_version=snapshot.history_version,
        history=list(snapshot.history),
        hidden_items=sorted(snapshot.hidden_items),
        favorite_items=sorted(snapshot.favorite_items),
        profile_id=snapshot.profile_id,
    )


def _created_session_response(created: CreatedSession) -> SessionCreatedResponse:
    return SessionCreatedResponse(
        **_session_response(created.snapshot).model_dump(),
        access_token=created.access_token,
    )


def _recommendation_response(result: RecommendationResult) -> RecommendationResponse:
    binding = result.binding
    return RecommendationResponse(
        request_id=binding.request_id,
        session_id=binding.session_id,
        session_epoch=binding.session_epoch,
        history_version=binding.history_version,
        bundle_id=binding.bundle_id,
        exclusion_version=binding.exclusion_version,
        requested_strategy=result.requested_strategy,
        actual_strategy=result.actual_strategy,
        fallback_reason=result.fallback_reason,
        items=[
            RecommendationItemResponse(item_id=item.item_id, score=item.score, source=item.source)
            for item in result.items
        ],
    )


def _feedback_response(result: FeedbackResult) -> FeedbackResponse:
    return FeedbackResponse(
        event_id=result.event_id,
        session_id=result.session_id,
        session_epoch=result.session_epoch,
        history_version=result.history_version,
        replayed=result.replayed,
        exposure_event_id=result.exposure_event_id,
    )


def _error(
    status_code: int,
    code: str,
    message: str,
    request_id: UUID | None = None,
    *,
    retryable: bool = False,
    rows: list[dict[str, object]] | None = None,
) -> JSONResponse:
    body = ErrorEnvelope(error=ErrorDetail(
        code=code, message=message, retryable=retryable, request_id=request_id,
    ))
    data = body.model_dump(mode="json")
    if rows is not None:
        data["rows"] = rows
    return JSONResponse(data, status_code=status_code)


def create_app(
    *,
    readiness_query: ReadinessQuery | None = None,
    demo_application: DemoApplication | None = None,
) -> FastAPI:
    demo = demo_application or build_demo_application()
    readiness_query = readiness_query or demo.readiness
    storage_description = (
        "配置 PostgreSQL：会话、请求和推荐结果可跨应用实例恢复。"
        if demo.persistent
        else "未配置 PostgreSQL：会话和推荐结果只保存在当前进程。"
    )
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if demo.manager is not None:
            try:
                await asyncio.to_thread(demo.manager.file_jobs.recover_interrupted)
                await asyncio.to_thread(demo.manager.builds.recover_interrupted)
                await asyncio.to_thread(demo.manager.recover)
            except psycopg.Error:
                pass  # Liveness remains available; readiness reports the database failure.
        yield

    app = FastAPI(
        title="EvoRec Demo API",
        version=__version__,
        description=(
            f"本地推荐与管理演示。{storage_description}"
            "受控 CPU bundle 可发布；真实 R06 研究模型尚未在线接入。"
        ),
        lifespan=lifespan,
    )

    @app.exception_handler(psycopg.Error)
    async def database_error(_, __):
        return _error(503, "database_unavailable", "database is unavailable", retryable=True)

    def admin_guard(token: str | None) -> JSONResponse | None:
        if demo.manager is None:
            return _error(503, "database_not_configured", "PostgreSQL is required")
        expected = os.getenv("EVOREC_ADMIN_TOKEN")
        if not expected or len(expected) < 32:
            return _error(503, "admin_not_configured", "administrator token is not configured")
        if not token or not hmac.compare_digest(token, expected):
            return _error(403, "admin_access_denied", "administrator token is invalid")
        return None

    def management_error(exc: ManagementError) -> JSONResponse:
        return _error(exc.status_code, exc.code, str(exc),
                      retryable=exc.status_code == 503, rows=exc.rows)

    management_responses = {
        code: {"model": ErrorEnvelope} for code in (403, 404, 409, 422, 503)
    }

    @app.get("/health/live", response_model=Liveness, tags=["health"])
    def live() -> Liveness:
        return Liveness()

    @app.get(
        "/health/ready", response_model=Readiness, tags=["health"],
        responses={503: {"model": Readiness, "description": "Business dependencies unavailable"}},
    )
    async def ready() -> JSONResponse:
        report = await readiness_query.execute()
        body = Readiness(ready=report.ready, blockers=list(report.blockers))
        return JSONResponse(body.model_dump(), status_code=200 if report.ready else 503)

    @app.get("/api/v1/system", response_model=SystemInfo, tags=["system"])
    def system() -> SystemInfo:
        configured_token = os.getenv("EVOREC_ADMIN_TOKEN") or ""
        publishing = bool(demo.manager and demo.manager.managed_root
                          and demo.manager.managed_root.is_dir()
                          and len(configured_token) >= 32)
        return SystemInfo(capabilities=Capabilities(
            persistence=demo.persistent, catalog_publication=publishing,
        ))

    @app.get("/app", tags=["web"], include_in_schema=False)
    def web_page() -> FileResponse:
        return FileResponse(Path(__file__).resolve().parents[3] / "web" / "index.html")

    @app.post(
        "/api/v1/sessions", response_model=SessionCreatedResponse, status_code=201, tags=["demo"],
    )
    async def create_session(payload: SessionCreateInput | None = None):
        try:
            return _created_session_response(
                await demo.backend.create_session((payload or SessionCreateInput()).profile_id)
            )
        except ValueError as exc:
            return _error(409, "sample_profile_unavailable", str(exc))

    @app.get(
        "/api/v1/sessions/{session_id}", response_model=SessionResponse, tags=["demo"],
        responses={
            401: {"model": ErrorEnvelope, "description": "Missing or invalid session token"},
            404: {"model": ErrorEnvelope, "description": "Session not found"},
        },
    )
    async def get_session(
        session_id: UUID,
        session_token: str | None = Header(default=None, alias="X-Session-Token"),
    ):
        if not session_token:
            return _error(401, "session_access_denied", "session token is required")
        try:
            return _session_response(await demo.backend.get_session(session_id, session_token))
        except ResourceNotFound as exc:
            return _error(404, "session_not_found", str(exc))
        except AccessDenied as exc:
            return _error(401, "session_access_denied", str(exc))

    @app.post(
        "/api/v1/sessions/{session_id}/reset", response_model=SessionResponse, tags=["demo"],
        responses={
            401: {"model": ErrorEnvelope, "description": "Missing or invalid session token"},
            404: {"model": ErrorEnvelope, "description": "Session not found"},
        },
    )
    async def reset_session(
        session_id: UUID,
        session_token: str | None = Header(default=None, alias="X-Session-Token"),
    ):
        if not session_token:
            return _error(401, "session_access_denied", "session token is required")
        try:
            return _session_response(await demo.backend.reset_session(session_id, session_token))
        except ResourceNotFound as exc:
            return _error(404, "session_not_found", str(exc))
        except AccessDenied as exc:
            return _error(401, "session_access_denied", str(exc))

    @app.post(
        "/api/v1/recommendations",
        response_model=RecommendationResponse,
        tags=["demo"],
        responses={
            404: {"model": ErrorEnvelope, "description": "Session not found"},
            401: {"model": ErrorEnvelope, "description": "Missing or invalid session token"},
            409: {"model": ErrorEnvelope, "description": "History version conflict"},
            504: {"model": ErrorEnvelope, "description": "Recommendation deadline exceeded"},
        },
    )
    async def recommend(
        payload: RecommendationInput,
        session_token: str | None = Header(default=None, alias="X-Session-Token"),
        idempotency_key: UUID | None = Header(default=None, alias="Idempotency-Key"),
    ):
        request_id = idempotency_key or uuid4()
        if not session_token:
            return _error(
                401, "session_access_denied", "session token is required", request_id,
            )
        command = RecommendationCommand(
            request_id=request_id,
            session_id=payload.session_id,
            session_token=session_token,
            expected_history_version=payload.expected_history_version,
            strategy=payload.strategy,
            k=payload.k,
            timeout_seconds=2.0,
        )
        try:
            return _recommendation_response(await demo.recommend.execute(command))
        except ResourceNotFound as exc:
            return _error(404, "session_not_found", str(exc), request_id)
        except AccessDenied as exc:
            return _error(401, "session_access_denied", str(exc), request_id)
        except HistoryConflict as exc:
            return _error(409, "history_conflict", str(exc), request_id)
        except IdempotencyReplay as exc:
            return _recommendation_response(exc.result)
        except IdempotencyInProgress as exc:
            return _error(409, "recommendation_in_progress", str(exc), request_id, retryable=True)
        except IdempotencyConflict as exc:
            return _error(409, "recommendation_idempotency_conflict", str(exc), request_id)
        except RuntimeError:
            return _error(503, "catalog_unavailable", "catalog is not ready", request_id,
                          retryable=True)
        except psycopg.Error:
            return _error(503, "database_unavailable", "database is unavailable", request_id,
                          retryable=True)
        except TimeoutError:
            return _error(
                504, "recommendation_timeout", "recommendation deadline exceeded",
                request_id, retryable=True,
            )

    @app.post(
        "/api/v1/strategy-comparisons/preview",
        response_model=ComparisonPreviewResponse,
        tags=["demo"],
        responses={
            401: {"model": ErrorEnvelope, "description": "Missing or invalid session token"},
            404: {"model": ErrorEnvelope, "description": "Session not found"},
            409: {"model": ErrorEnvelope, "description": "History version conflict"},
            503: {"model": ErrorEnvelope, "description": "Catalog is not ready"},
            504: {"model": ErrorEnvelope, "description": "Comparison deadline exceeded"},
        },
    )
    async def preview_comparison(
        payload: ComparisonPreviewInput,
        session_token: str | None = Header(default=None, alias="X-Session-Token"),
    ):
        return await execute_comparison(payload, session_token, uuid4(), persist=False)

    @app.post(
        "/api/v1/strategy-comparisons", response_model=ComparisonSavedResponse, tags=["demo"],
        responses={401: {"model": ErrorEnvelope}, 404: {"model": ErrorEnvelope},
                   409: {"model": ErrorEnvelope}, 503: {"model": ErrorEnvelope},
                   504: {"model": ErrorEnvelope}},
    )
    async def save_comparison(
        payload: ComparisonPreviewInput,
        session_token: str | None = Header(default=None, alias="X-Session-Token"),
        idempotency_key: UUID | None = Header(default=None, alias="Idempotency-Key"),
    ):
        return await execute_comparison(payload, session_token, idempotency_key or uuid4(), persist=True)

    def comparison_response(result, *, persist: bool):
        binding = result.binding
        preview = ComparisonPreviewResponse(
            comparison_id=binding.request_id, session_id=binding.session_id,
            session_epoch=binding.session_epoch, history_version=binding.history_version,
            bundle_id=binding.bundle_id, exclusion_version=binding.exclusion_version,
            snapshot_at=result.snapshot_at, common_item_ids=list(result.common_item_ids),
            strategies=[ComparedStrategyResponse(
                requested_strategy=entry.requested_strategy,
                actual_strategy=entry.actual_strategy,
                fallback_reason=entry.fallback_reason, elapsed_ms=entry.elapsed_ms,
                items=[RecommendationItemResponse(
                    item_id=item.item_id, score=item.score, source=item.source,
                ) for item in entry.items],
                unique_item_ids=list(entry.unique_item_ids),
            ) for entry in result.strategies],
        )
        if not persist:
            return preview
        session, catalog = result.context.session, result.context.catalog
        return ComparisonSavedResponse(**{
            **preview.model_dump(), "persisted": True, "requested_k": result.requested_k,
            "input_snapshot": ComparisonInputSnapshotResponse(
                history=list(session.history), hidden_items=sorted(session.hidden_items),
                favorite_items=sorted(session.favorite_items), profile_id=session.profile_id,
                eligible_items=sorted(catalog.eligible_items),
            ),
        })

    async def execute_comparison(payload, session_token, comparison_id, *, persist: bool):
        if not session_token:
            return _error(401, "session_access_denied", "session token is required", comparison_id)
        command = ComparisonCommand(
            comparison_id, payload.session_id, session_token,
            payload.expected_history_version,
            tuple(Strategy(strategy) for strategy in payload.strategies), payload.k,
        )
        try:
            result = await (demo.compare.save(command) if persist else demo.compare.preview(command))
        except ResourceNotFound as exc:
            return _error(404, "session_not_found", str(exc), comparison_id)
        except AccessDenied as exc:
            return _error(401, "session_access_denied", str(exc), comparison_id)
        except HistoryConflict as exc:
            return _error(409, "history_conflict", str(exc), comparison_id)
        except IdempotencyConflict as exc:
            return _error(409, "idempotency_conflict", str(exc), comparison_id)
        except ComparisonStorageUnavailable as exc:
            return _error(503, "comparison_storage_unavailable", str(exc), comparison_id)
        except RuntimeError:
            return _error(503, "catalog_unavailable", "catalog is not ready", comparison_id,
                          retryable=True)
        except psycopg.Error:
            return _error(503, "database_unavailable", "database is unavailable", comparison_id,
                          retryable=True)
        except TimeoutError:
            return _error(504, "comparison_timeout", "comparison deadline exceeded",
                          comparison_id, retryable=True)
        return comparison_response(result, persist=persist)

    @app.get(
        "/api/v1/strategy-comparisons", response_model=ComparisonPageResponse, tags=["demo"],
        responses={401: {"model": ErrorEnvelope}, 404: {"model": ErrorEnvelope},
                   503: {"model": ErrorEnvelope}},
    )
    async def list_comparisons(
        session_id: UUID, offset: int = Query(default=0, ge=0, le=10000),
        limit: int = Query(default=20, ge=1, le=50),
        session_token: str | None = Header(default=None, alias="X-Session-Token"),
    ):
        request_id = uuid4()
        if not session_token:
            return _error(401, "session_access_denied", "session token is required", request_id)
        try:
            page = await demo.compare.list(session_id, session_token, offset, limit)
        except AccessDenied as exc:
            return _error(401, "session_access_denied", str(exc), request_id)
        except ResourceNotFound as exc:
            return _error(404, "session_not_found", str(exc), request_id)
        except ComparisonStorageUnavailable as exc:
            return _error(503, "comparison_storage_unavailable", str(exc), request_id)
        except psycopg.Error:
            return _error(503, "database_unavailable", "database is unavailable", request_id,
                          retryable=True)
        return ComparisonPageResponse(
            offset=page.offset, limit=page.limit, has_more=page.has_more,
            items=[ComparisonSummaryResponse(
                comparison_id=row.comparison_id, session_id=row.session_id,
                history_version=row.history_version, bundle_id=row.bundle_id,
                snapshot_at=row.snapshot_at, requested_k=row.requested_k,
                requested_strategies=list(row.requested_strategies),
                actual_strategies=list(row.actual_strategies),
            ) for row in page.items],
        )

    @app.get(
        "/api/v1/strategy-comparisons/{comparison_id}", response_model=ComparisonSavedResponse,
        tags=["demo"], responses={401: {"model": ErrorEnvelope}, 404: {"model": ErrorEnvelope},
                                  503: {"model": ErrorEnvelope}},
    )
    async def get_comparison(
        comparison_id: UUID, session_id: UUID,
        session_token: str | None = Header(default=None, alias="X-Session-Token"),
    ):
        if not session_token:
            return _error(401, "session_access_denied", "session token is required", comparison_id)
        try:
            result = await demo.compare.get(comparison_id, session_id, session_token)
        except AccessDenied as exc:
            return _error(401, "session_access_denied", str(exc), comparison_id)
        except ResourceNotFound as exc:
            return _error(404, "comparison_not_found", str(exc), comparison_id)
        except ComparisonStorageUnavailable as exc:
            return _error(503, "comparison_storage_unavailable", str(exc), comparison_id)
        except psycopg.Error:
            return _error(503, "database_unavailable", "database is unavailable", comparison_id,
                          retryable=True)
        return comparison_response(result, persist=True)

    async def comparison_job_operation(operation, session_id, session_token, job_id, payload=None):
        if not session_token:
            return _error(401, 'session_access_denied', 'session token is required', job_id)
        if demo.comparison_jobs is None:
            return _error(503, 'comparison_storage_unavailable', 'comparison jobs require PostgreSQL', job_id)
        try:
            if operation == 'enqueue':
                command = ComparisonCommand(job_id, session_id, session_token,
                                            payload.expected_history_version,
                                            tuple(Strategy(s) for s in payload.strategies), payload.k)
                row = await demo.comparison_jobs.enqueue(command)
            else:
                row = await getattr(demo.comparison_jobs, operation)(job_id, session_id, session_token)
            return ComparisonJobResponse(**row)
        except AccessDenied as exc:
            return _error(401, 'session_access_denied', str(exc), job_id)
        except ResourceNotFound as exc:
            return _error(404, 'comparison_job_not_found', str(exc), job_id)
        except (HistoryConflict, IdempotencyConflict) as exc:
            code = 'history_conflict' if isinstance(exc, HistoryConflict) else 'idempotency_conflict'
            return _error(409, code, str(exc), job_id)
        except ManagementError as exc:
            return _error(exc.status_code, exc.code, str(exc), job_id, retryable=exc.status_code == 429)
        except psycopg.Error:
            return _error(503, 'database_unavailable', 'database is unavailable', job_id, retryable=True)
        except RuntimeError:
            return _error(503, 'catalog_unavailable', 'catalog is not ready', job_id, retryable=True)
        except TimeoutError:
            return _error(504, 'comparison_timeout', 'comparison submission deadline exceeded', job_id, retryable=True)

    job_errors = {401: {'model': ErrorEnvelope}, 404: {'model': ErrorEnvelope},
                  409: {'model': ErrorEnvelope}, 429: {'model': ErrorEnvelope},
                  503: {'model': ErrorEnvelope}, 504: {'model': ErrorEnvelope}}

    @app.post('/api/v1/strategy-comparison-jobs', response_model=ComparisonJobResponse,
              status_code=202, tags=['demo'], responses=job_errors)
    async def enqueue_comparison_job(
        payload: ComparisonPreviewInput,
        session_token: str | None = Header(default=None, alias='X-Session-Token'),
        idempotency_key: UUID | None = Header(default=None, alias='Idempotency-Key'),
    ):
        return await comparison_job_operation('enqueue', payload.session_id, session_token,
                                              idempotency_key or uuid4(), payload)

    @app.get('/api/v1/strategy-comparison-jobs/{job_id}', response_model=ComparisonJobResponse,
             tags=['demo'], responses=job_errors)
    async def get_comparison_job(
        job_id: UUID, session_id: UUID,
        session_token: str | None = Header(default=None, alias='X-Session-Token'),
    ):
        return await comparison_job_operation('get', session_id, session_token, job_id)

    @app.post('/api/v1/strategy-comparison-jobs/{job_id}/cancel', response_model=ComparisonJobResponse,
              tags=['demo'], responses=job_errors)
    async def cancel_comparison_job(
        job_id: UUID, session_id: UUID,
        session_token: str | None = Header(default=None, alias='X-Session-Token'),
    ):
        return await comparison_job_operation('cancel', session_id, session_token, job_id)

    @app.post(
        "/api/v1/feedback",
        response_model=FeedbackResponse,
        tags=["demo"],
        responses={
            401: {"model": ErrorEnvelope, "description": "Missing or invalid session token"},
            404: {"model": ErrorEnvelope, "description": "Session not found"},
            409: {"model": ErrorEnvelope, "description": "Feedback conflict"},
        },
    )
    async def feedback(
        payload: FeedbackInput,
        session_token: str | None = Header(default=None, alias="X-Session-Token"),
    ):
        if not session_token:
            return _error(
                401, "session_access_denied", "session token is required", payload.request_id,
            )
        command = FeedbackCommand(
            event_id=payload.event_id,
            session_id=payload.session_id,
            session_token=session_token,
            request_id=payload.request_id,
            item_id=payload.item_id,
            kind=payload.kind,
            observed_at=payload.observed_at,
            desired_state=payload.desired_state,
            visible_ratio=payload.visible_ratio,
            visible_duration_ms=payload.visible_duration_ms,
            schema_version=payload.schema_version,
        )
        try:
            return _feedback_response(await demo.backend.record_feedback(command))
        except ResourceNotFound as exc:
            return _error(404, "session_not_found", str(exc), payload.request_id)
        except AccessDenied as exc:
            return _error(401, "session_access_denied", str(exc), payload.request_id)
        except IdempotencyConflict as exc:
            return _error(409, "feedback_idempotency_conflict", str(exc), payload.request_id)
        except SessionEpochConflict as exc:
            return _error(409, "session_epoch_conflict", str(exc), payload.request_id)
        except FeedbackSourceMismatch as exc:
            return _error(409, "feedback_source_conflict", str(exc), payload.request_id)

    @app.get("/api/v1/items", response_model=list[CatalogItemResponse], tags=["catalog"])
    async def list_items(offset: int = Query(default=0, ge=0),
                         limit: int = Query(default=100, ge=1, le=100)):
        if demo.manager is None:
            return demo.backend.list_items()[offset:offset + limit]
        try:
            return await asyncio.to_thread(demo.manager.list_items, offset=offset, limit=limit)
        except psycopg.Error:
            return _error(503, "database_unavailable", "database is unavailable", retryable=True)

    @app.get("/api/v1/items/{item_id}", response_model=CatalogItemResponse, tags=["catalog"])
    async def get_item(item_id: str):
        if demo.manager is None:
            item = demo.backend.get_item(item_id)
            return item if item is not None else _error(404, "item_not_found", "item does not exist")
        try:
            return await asyncio.to_thread(demo.manager.get_item, item_id)
        except ManagementError as exc:
            return management_error(exc)

    @app.post("/api/v1/admin/catalog/imports", response_model=CatalogImportResponse,
              tags=["admin"])
    async def import_catalog(
        payload: CatalogImportInput,
        admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
    ):
        denied = admin_guard(admin_token)
        if denied is not None:
            return denied
        try:
            return await asyncio.to_thread(demo.manager.import_items, payload)
        except ManagementError as exc:
            return management_error(exc)

    @app.post(
        "/api/v1/admin/catalog/file-imports",
        response_model=CatalogImportResponse,
        responses={
            403: {"model": ErrorEnvelope},
            409: {"model": CatalogFileErrorResponse},
            413: {"model": CatalogFileErrorResponse},
            422: {"model": CatalogFileErrorResponse},
            503: {"model": ErrorEnvelope},
        },
        openapi_extra={"requestBody": {"required": True, "content": {
            "text/csv": {"schema": {"type": "string", "format": "binary"}},
            "application/json": {"schema": {"type": "array", "items": {
                "$ref": "#/components/schemas/CatalogItem"}}},
        }}},
        tags=["admin"],
    )
    async def import_catalog_file(
        request: Request,
        batch_id: UUID = Header(alias="X-Batch-Id"),
        admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
    ):
        denied = admin_guard(admin_token)
        if denied is not None:
            return denied
        data = bytearray()
        async for chunk in request.stream():
            if len(chunk) > MAX_FILE_BYTES - len(data):
                return _error(413, "file_too_large", "catalog file exceeds 20 MB",
                              rows=[{"row": 0, "field": "file", "reason": "file exceeds 20 MB"}])
            data.extend(chunk)
        media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        try:
            payload = parse_catalog_file(bytes(data), media_type, batch_id)
            offset = 2 if media_type == "text/csv" else 1
            numbers = {item.item_id: row for row, item in enumerate(payload.items, offset)}
            return await asyncio.to_thread(demo.manager.import_items, payload, row_numbers=numbers)
        except CatalogFileError as exc:
            return _error(422, exc.code, str(exc),
                          rows=[row.__dict__ for row in exc.rows])
        except ManagementError as exc:
            return _error(exc.status_code, exc.code, str(exc),
                          retryable=exc.status_code == 503, rows=exc.rows or [])

    @app.post(
        "/api/v1/admin/catalog/file-import-jobs",
        response_model=CatalogFileJobResponse,
        status_code=202,
        responses={200: {"model": CatalogFileJobResponse}, 403: {"model": ErrorEnvelope},
                   409: {"model": ErrorEnvelope}, 413: {"model": ErrorEnvelope},
                   503: {"model": ErrorEnvelope}},
        openapi_extra={"requestBody": {"required": True, "content": {
            "text/csv": {"schema": {"type": "string", "format": "binary"}},
            "application/json": {"schema": {"type": "array", "items": {
                "$ref": "#/components/schemas/CatalogItem"}}},
        }}},
        tags=["admin"],
    )
    async def enqueue_catalog_file(
        request: Request,
        response: Response,
        batch_id: UUID = Header(alias="X-Batch-Id"),
        admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
    ):
        denied = admin_guard(admin_token)
        if denied is not None:
            return denied
        data = bytearray()
        async for chunk in request.stream():
            if len(chunk) > MAX_FILE_BYTES - len(data):
                return _error(413, "file_too_large", "catalog file exceeds 20 MB")
            data.extend(chunk)
        media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        try:
            state = await asyncio.to_thread(demo.manager.file_jobs.enqueue,
                                            batch_id, bytes(data), media_type)
            response.status_code = 200 if state["status"] in {"imported", "failed"} else 202
            return state
        except ManagementError as exc:
            return management_error(exc)

    @app.get("/api/v1/admin/catalog/file-import-jobs/{batch_id}",
             response_model=CatalogFileJobResponse, responses=management_responses,
             tags=["admin"])
    async def catalog_file_job_status(
        batch_id: UUID,
        admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
    ):
        denied = admin_guard(admin_token)
        if denied is not None:
            return denied
        try:
            return await asyncio.to_thread(demo.manager.file_jobs.get, batch_id)
        except ManagementError as exc:
            return management_error(exc)

    @app.get("/api/v1/admin/catalog/imports/{batch_id}",
             response_model=CatalogImportStateResponse, responses=management_responses,
             tags=["admin"])
    async def catalog_import_status(batch_id: UUID,
                                    admin_token: str | None = Header(default=None, alias="X-Admin-Token")):
        denied = admin_guard(admin_token)
        if denied is not None:
            return denied
        try:
            return await asyncio.to_thread(demo.manager.get_import, batch_id)
        except ManagementError as exc:
            return management_error(exc)

    @app.post("/api/v1/admin/catalog/imports/{batch_id}/builds",
              response_model=CatalogBuildResponse, responses=management_responses, tags=["admin"])
    async def process_catalog(batch_id: UUID, payload: CatalogBuildInput,
                              admin_token: str | None = Header(default=None, alias="X-Admin-Token")):
        denied = admin_guard(admin_token)
        if denied is not None:
            return denied
        try:
            return await asyncio.to_thread(demo.manager.builds.process, batch_id, payload.build_id)
        except ManagementError as exc:
            return management_error(exc)

    @app.post("/api/v1/admin/catalog/imports/{batch_id}/build-jobs",
              response_model=CatalogBuildResponse, status_code=202,
              responses={200: {"model": CatalogBuildResponse}, **management_responses}, tags=["admin"])
    async def enqueue_catalog(batch_id: UUID, payload: CatalogBuildInput,
                              response: Response,
                              admin_token: str | None = Header(default=None, alias="X-Admin-Token")):
        denied = admin_guard(admin_token)
        if denied is not None:
            return denied
        try:
            state = await asyncio.to_thread(demo.manager.builds.enqueue, batch_id, payload.build_id)
            response.status_code = 200 if state["status"] in {"ready", "failed"} else 202
            return state
        except ManagementError as exc:
            return management_error(exc)

    @app.get("/api/v1/admin/catalog/builds/{build_id}",
             response_model=CatalogBuildResponse, responses=management_responses, tags=["admin"])
    async def catalog_build_status(build_id: UUID,
                                    admin_token: str | None = Header(default=None, alias="X-Admin-Token")):
        denied = admin_guard(admin_token)
        if denied is not None:
            return denied
        try:
            return await asyncio.to_thread(demo.manager.builds.get, build_id)
        except ManagementError as exc:
            return management_error(exc)

    @app.get("/api/v1/admin/catalog/builds/{build_id}/items",
             response_model=CatalogPreviewResponse, responses=management_responses, tags=["admin"])
    async def preview_catalog(build_id: UUID, offset: int = Query(default=0, ge=0),
                              limit: int = Query(default=50, ge=1, le=100),
                              admin_token: str | None = Header(default=None, alias="X-Admin-Token")):
        denied = admin_guard(admin_token)
        if denied is not None:
            return denied
        try:
            return await asyncio.to_thread(demo.manager.builds.preview, build_id, offset, limit)
        except ManagementError as exc:
            return management_error(exc)

    @app.post("/api/v1/admin/catalog/builds/{build_id}/publish",
              response_model=PublicationResponse, responses=management_responses, tags=["admin"])
    async def publish_catalog(build_id: UUID, payload: CatalogBuildPublishInput,
                              admin_token: str | None = Header(default=None, alias="X-Admin-Token")):
        denied = admin_guard(admin_token)
        if denied is not None:
            return denied
        try:
            return await asyncio.to_thread(demo.manager.builds.publish, build_id, payload.operation_id)
        except ManagementError as exc:
            return management_error(exc)

    @app.post("/api/v1/admin/items/{item_id}/deactivate",
              response_model=ItemDeactivationResponse, tags=["admin"])
    async def deactivate_item(
        item_id: str,
        admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
    ):
        denied = admin_guard(admin_token)
        if denied is not None:
            return denied
        try:
            return await asyncio.to_thread(demo.manager.deactivate_item, item_id)
        except ManagementError as exc:
            return management_error(exc)

    @app.post("/api/v1/admin/bundles/{bundle_id}/register",
              response_model=BundleRegistrationResponse, tags=["admin"])
    async def register_bundle(
        bundle_id: UUID,
        admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
    ):
        denied = admin_guard(admin_token)
        if denied is not None:
            return denied
        try:
            return await asyncio.to_thread(demo.manager.register_bundle, bundle_id)
        except ManagementError as exc:
            return management_error(exc)

    @app.get("/api/v1/admin/publication", response_model=PublicationStateResponse,
             tags=["admin"])
    async def publication_state(
        admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
    ):
        denied = admin_guard(admin_token)
        if denied is not None:
            return denied
        return await asyncio.to_thread(demo.manager.publication_state)

    @app.post("/api/v1/admin/bundles/{bundle_id}/publish",
              response_model=PublicationResponse, tags=["admin"])
    async def publish_bundle(
        bundle_id: UUID,
        payload: PublicationInput,
        admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
    ):
        denied = admin_guard(admin_token)
        if denied is not None:
            return denied
        try:
            return await asyncio.to_thread(
                demo.manager.publish, payload.operation_id, bundle_id,
                payload.expected_active_bundle_id,
            )
        except ManagementError as exc:
            return management_error(exc)
        except psycopg.Error:
            return _error(503, "publication_uncertain", "check publication state before retrying",
                          retryable=True)

    @app.post("/api/v1/admin/bundles/{bundle_id}/rollback",
              response_model=PublicationResponse, tags=["admin"])
    async def rollback_bundle(
        bundle_id: UUID,
        payload: PublicationInput,
        admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
    ):
        denied = admin_guard(admin_token)
        if denied is not None:
            return denied
        try:
            return await asyncio.to_thread(
                demo.manager.rollback, payload.operation_id, bundle_id,
                payload.expected_active_bundle_id,
            )
        except ManagementError as exc:
            return management_error(exc)
        except psycopg.Error:
            return _error(503, "publication_uncertain", "check publication state before retrying",
                          retryable=True)

    @app.post("/api/v1/admin/publication/recover",
              response_model=PublicationStateResponse, tags=["admin"])
    async def recover_publication(
        admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
    ):
        denied = admin_guard(admin_token)
        if denied is not None:
            return denied
        await asyncio.to_thread(demo.manager.recover)
        return await asyncio.to_thread(demo.manager.publication_state)

    return app


app = create_app()
