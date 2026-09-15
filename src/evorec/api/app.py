"""M0 API surface. Readiness stays false until real business probes are connected."""

from typing import Literal

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from evorec import __version__
from evorec.application.health import ReadinessQuery
from evorec.bootstrap import build_readiness_query


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
    recommendations: Literal[False] = False
    catalog_publication: Literal[False] = False
    model_training: Literal[False] = False
    frontend: Literal[False] = False


class SystemInfo(BaseModel):
    name: Literal["EvoRec"] = "EvoRec"
    version: str = __version__
    phase: Literal["M0-framework"] = "M0-framework"
    capabilities: Capabilities


def create_app(*, readiness_query: ReadinessQuery | None = None) -> FastAPI:
    readiness_query = readiness_query or build_readiness_query()
    app = FastAPI(
        title="EvoRec Framework API",
        version=__version__,
        description=(
            "M0 工程框架。仅提供运行状态接口；业务接口见设计文档，尚未注册。"
            "存活检查通过不代表推荐业务就绪。"
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

    return app


app = create_app()
