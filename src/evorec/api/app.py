"""API for the local recommendation and controlled catalog workflow."""

import asyncio
import hmac
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from fastapi import FastAPI, Header
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict
import psycopg

from evorec import __version__
from evorec.application.health import ReadinessQuery
from evorec.bootstrap import DemoApplication, build_demo_application
from evorec.contracts import CatalogImportInput, FeedbackInput, RecommendationInput
from evorec.domain.errors import (
    AccessDenied,
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


def _session_response(snapshot: SessionSnapshot) -> SessionResponse:
    return SessionResponse(
        session_id=snapshot.session_id,
        epoch=snapshot.epoch,
        history_version=snapshot.history_version,
        history=list(snapshot.history),
        hidden_items=sorted(snapshot.hidden_items),
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
) -> JSONResponse:
    body = ErrorEnvelope(error=ErrorDetail(
        code=code, message=message, retryable=retryable, request_id=request_id,
    ))
    return JSONResponse(
        body.model_dump(mode="json"),
        status_code=status_code,
    )


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
                      retryable=exc.status_code == 503)

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
    async def create_session() -> SessionCreatedResponse:
        return _created_session_response(await demo.backend.create_session())

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
    async def list_items():
        if demo.manager is None:
            return _error(503, "database_not_configured", "PostgreSQL is required")
        try:
            return await asyncio.to_thread(demo.manager.list_items)
        except psycopg.Error:
            return _error(503, "database_unavailable", "database is unavailable", retryable=True)

    @app.get("/api/v1/items/{item_id}", response_model=CatalogItemResponse, tags=["catalog"])
    async def get_item(item_id: str):
        if demo.manager is None:
            return _error(503, "database_not_configured", "PostgreSQL is required")
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
