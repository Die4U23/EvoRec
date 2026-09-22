"""Status API plus a process-local M1 recommendation demonstration."""

from typing import Literal
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from evorec import __version__
from evorec.application.health import ReadinessQuery
from evorec.bootstrap import DemoApplication, build_demo_application, build_readiness_query
from evorec.contracts import RecommendationInput
from evorec.domain.errors import HistoryConflict, ResourceNotFound
from evorec.domain.models import RecommendationCommand, RecommendationResult, SessionSnapshot


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
    persistence: Literal[False] = False
    recommendations: Literal[True] = True
    catalog_publication: Literal[False] = False
    model_training: Literal[False] = False
    frontend: Literal[False] = False


class SystemInfo(BaseModel):
    name: Literal["EvoRec"] = "EvoRec"
    version: str = __version__
    phase: Literal["M1-memory-demo"] = "M1-memory-demo"
    capabilities: Capabilities


class SessionResponse(BaseModel):
    session_id: UUID
    epoch: int
    history_version: int
    history: list[str]
    hidden_items: list[str]


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
    readiness_query = readiness_query or build_readiness_query()
    demo = demo_application or build_demo_application()
    app = FastAPI(
        title="EvoRec Demo API",
        version=__version__,
        description=(
            "M1 进程内演示：会话和推荐结果不会跨进程重启保存。"
            "业务就绪仍为 false，直到数据库和真实模型运行时接入。"
        ),
    )

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
        return SystemInfo(capabilities=Capabilities())

    @app.post("/api/v1/sessions", response_model=SessionResponse, status_code=201, tags=["demo"])
    async def create_session() -> SessionResponse:
        return _session_response(await demo.backend.create_session())

    @app.get(
        "/api/v1/sessions/{session_id}", response_model=SessionResponse, tags=["demo"],
        responses={404: {"model": ErrorEnvelope, "description": "Session not found"}},
    )
    async def get_session(session_id: UUID):
        try:
            return _session_response(await demo.backend.get_session(session_id))
        except ResourceNotFound as exc:
            return _error(404, "session_not_found", str(exc))

    @app.post(
        "/api/v1/sessions/{session_id}/reset", response_model=SessionResponse, tags=["demo"],
        responses={404: {"model": ErrorEnvelope, "description": "Session not found"}},
    )
    async def reset_session(session_id: UUID):
        try:
            return _session_response(await demo.backend.reset_session(session_id))
        except ResourceNotFound as exc:
            return _error(404, "session_not_found", str(exc))

    @app.post(
        "/api/v1/recommendations",
        response_model=RecommendationResponse,
        tags=["demo"],
        responses={
            404: {"model": ErrorEnvelope, "description": "Session not found"},
            409: {"model": ErrorEnvelope, "description": "History version conflict"},
            504: {"model": ErrorEnvelope, "description": "Recommendation deadline exceeded"},
        },
    )
    async def recommend(payload: RecommendationInput):
        request_id = uuid4()
        command = RecommendationCommand(
            request_id=request_id,
            session_id=payload.session_id,
            expected_history_version=payload.expected_history_version,
            strategy=payload.strategy,
            k=payload.k,
            timeout_seconds=2.0,
        )
        try:
            return _recommendation_response(await demo.recommend.execute(command))
        except ResourceNotFound as exc:
            return _error(404, "session_not_found", str(exc), request_id)
        except HistoryConflict as exc:
            return _error(409, "history_conflict", str(exc), request_id)
        except TimeoutError:
            return _error(
                504, "recommendation_timeout", "recommendation deadline exceeded",
                request_id, retryable=True,
            )

    return app


app = create_app()
