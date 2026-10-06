"""Session-owned batch evaluation API; no dataset filesystem paths accepted."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Header, Query
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel

from evorec.contracts import EvaluationInput
from evorec.domain.errors import AccessDenied, IdempotencyConflict, ManagementError, ResourceNotFound


class EvaluationJobResponse(BaseModel):
    job_id: UUID
    session_id: UUID
    status: Literal['queued','running','cancelling','completed','cancelled','failed']
    completed_cases: int
    total_cases: int
    attempts: int
    cancel_requested: bool
    error_code: str | None
    replay_of: UUID | None
    dataset_name: str
    label_origin: Literal['synthetic','manual','historical_export']
    dataset_sha256: str
    protocol: Literal['held-out-saved-snapshot-v1']
    created_at: datetime
    updated_at: datetime


class EvaluationPageResponse(BaseModel):
    items: list[EvaluationJobResponse]
    offset: int
    limit: int
    has_more: bool


def router(service, error_response):
    routes = APIRouter(tags=['evaluation'])

    async def call(method, *args):
        if service is None:
            return error_response(503,'evaluation_storage_unavailable','evaluations require PostgreSQL')
        try:
            return await getattr(service,method)(*args)
        except AccessDenied:
            return error_response(401,'session_access_denied','session token is invalid')
        except ResourceNotFound as error:
            return error_response(404,'evaluation_not_found',str(error))
        except IdempotencyConflict as error:
            return error_response(409,'idempotency_conflict',str(error))
        except ManagementError as error:
            return error_response(error.status_code,error.code,str(error),retryable=error.status_code in (429,503))

    @routes.post('/api/v1/evaluation-jobs',response_model=EvaluationJobResponse,status_code=202)
    async def enqueue(payload: EvaluationInput,
        session_token: str = Header(alias='X-Session-Token'),
        idempotency_key: UUID = Header(alias='Idempotency-Key')):
        return await call('enqueue',idempotency_key,session_token,payload)

    @routes.get('/api/v1/evaluation-jobs',response_model=EvaluationPageResponse)
    async def listing(session_id: UUID, offset: int = Query(0,ge=0,le=10000),limit: int = Query(20,ge=1,le=50),
        session_token: str = Header(alias='X-Session-Token')):
        return await call('list',session_id,session_token,offset,limit)

    @routes.get('/api/v1/evaluation-jobs/{job_id}',response_model=EvaluationJobResponse)
    async def get(job_id: UUID,session_id: UUID,session_token: str = Header(alias='X-Session-Token')):
        return await call('get',job_id,session_id,session_token)

    @routes.post('/api/v1/evaluation-jobs/{job_id}/cancel',response_model=EvaluationJobResponse)
    async def cancel(job_id: UUID,session_id: UUID,session_token: str = Header(alias='X-Session-Token')):
        return await call('cancel',job_id,session_id,session_token)

    @routes.post('/api/v1/evaluation-jobs/{job_id}/replay',response_model=EvaluationJobResponse,status_code=202)
    async def replay(job_id: UUID,session_id: UUID,session_token: str = Header(alias='X-Session-Token'),
        idempotency_key: UUID = Header(alias='Idempotency-Key')):
        return await call('replay',job_id,idempotency_key,session_id,session_token)

    @routes.get('/api/v1/evaluation-jobs/{job_id}/report')
    async def report(job_id: UUID,session_id: UUID,download: bool = False,
        session_token: str = Header(alias='X-Session-Token')):
        result = await call('result',job_id,session_id,session_token)
        if isinstance(result,JSONResponse):
            return result
        headers = {'Cache-Control':'no-store'}
        if download:
            headers['Content-Disposition'] = f'attachment; filename="evaluation-{job_id}.json"'
        return JSONResponse(jsonable_encoder(result),headers=headers)

    return routes
