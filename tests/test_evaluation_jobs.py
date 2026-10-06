"""Real PostgreSQL evaluations, independent metric oracle, ownership and fencing."""

import asyncio
from datetime import datetime, timedelta, timezone
import math
from pathlib import Path
import os
import subprocess
import sys
import threading
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest

from evorec.api.app import create_app
from evorec.application.compare import ComparisonCommand
from evorec.application.evaluation import aggregate, metrics
from evorec.contracts import EvaluationInput
from evorec.domain.errors import IdempotencyConflict, ManagementError, ResourceNotFound
from evorec.domain.models import Strategy
from evorec.infrastructure.comparison_job import ComparisonLeaseLost
from test_strategy_comparison import published_application
from test_r06_online import online


async def inputs(application, count=2):
    created = await application.backend.create_session()
    session, token = created.snapshot.session_id, created.access_token
    cases = []
    for index in range(count):
        command = ComparisonCommand(uuid4(),session,token,0,(Strategy.POPULAR,Strategy.DENSE),2)
        comparison = await application.compare.save(command)
        cases.append(dict(comparison_id=str(command.comparison_id),target_item_id='alpha' if index==0 else 'absent',
                          target_at=(comparison.snapshot_at+timedelta(seconds=1)).isoformat(),cohort='group-'+str(index)))
    return token, EvaluationInput(session_id=session,dataset_name='synthetic-batch',label_origin='synthetic',
                                 source_description='synthetic labels, not research evidence',cases=cases,k=2)


def test_metric_oracle_and_invalid_duplicate():
    assert metrics(['a','b'],'a',2) == {'ndcg@2':1.,'recall@2':1.}
    assert metrics(['a','b'],'b',2) == {'ndcg@2':1/math.log2(3),'recall@2':1.}
    assert metrics([],'b',2) == {'ndcg@2':0.,'recall@2':0.}
    with pytest.raises(ValueError): metrics(['a','a'],'a',2)
    with pytest.raises(ValueError): metrics(['a','b'],'a',1)
    ranking=[str(i) for i in range(20)]
    assert metrics(ranking,'19',20)['recall@20']==1
    assert metrics(ranking,'19',20)['ndcg@10']==0


def test_failed_metrics_unknown_not_zero_and_denominators_explicit():
    row=dict(groups=['cold_user'],strategies={'dense':dict(status='failed',fallback_reason=None)})
    result=aggregate([row],['dense'],10)
    assert result['all_cases']['dense']['metrics']=={'ndcg@10':None,'recall@10':None}
    assert result['all_cases']['dense']['failed_cases']==1
    assert result['all_cases']['dense']['evaluated_cases']==0


def test_real_batch_freeze_metrics_persistence_replay_and_authorization(isolated_database,monkeypatch,tmp_path):
    application,_=published_application(monkeypatch,tmp_path)
    service=application.evaluation_jobs
    async def exercise():
        token,payload=await inputs(application)
        key=uuid4()
        queued=await service.enqueue(key,token,payload)
        assert queued['status']=='queued' and queued['total_cases']==2
        assert await service.enqueue(key,token,payload)==queued
        changed=payload.model_copy(update={'dataset_name':'changed'})
        with pytest.raises(IdempotencyConflict): await service.enqueue(key,token,changed)
        with pytest.raises(ManagementError): await service.result(key,payload.session_id,token)
        # Neither current mutable eligibility nor history may rewrite the saved inputs.
        await application.backend.reset_session(payload.session_id,token)
        with application.backend._connect() as connection:
            connection.execute('UPDATE items SET is_active=false')
        assert await service.run_next()
        completed=await service.get(key,payload.session_id,token)
        assert completed['status']=='completed' and completed['completed_cases']==2
        result=await service.result(key,payload.session_id,token)
        for method in ['popular','dense']:
            assert result['groups']['all_cases'][method]['cases']==2
            assert result['groups']['all_cases'][method]['failed_cases']==0
            first=result['cases'][0]['strategies'][method]
            # Independent rank oracle; fixture scores/order are not assumed.
            rank=first['items'].index('alpha')+1
            assert first['metrics']['ndcg@2']==1/math.log2(rank+1)
            assert first['metrics']['recall@2']==1
            assert result['cases'][1]['strategies'][method]['metrics']['recall@2']==0
        assert result['cases'][0]['target_available'] and not result['cases'][1]['target_available']
        assert result['label_provenance_verified'] is False
        assert result['configuration']['requested_k']==2
        assert result['groups']['cold_user']['popular']['cases']==2
        replay_id=uuid4()
        replay=await service.replay(key,replay_id,payload.session_id,token)
        assert replay['replay_of']==key
        assert await service.run_next()
        replay_result=await service.result(replay_id,payload.session_id,token)
        assert replay_result['semantic_result_sha256']==result['semantic_result_sha256']
        assert replay_result['frozen_inputs_sha256']==result['frozen_inputs_sha256']
        assert replay_result['dataset_sha256']==result['dataset_sha256']
        assert (await service.list(payload.session_id,token,0,1))['has_more']
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(demo_application=application)),base_url='http://test') as client:
            query=f'/api/v1/evaluation-jobs/{key}?session_id={payload.session_id}'
            assert (await client.get(query,headers={'X-Session-Token':'wrong'})).status_code==401
            other=await application.backend.create_session()
            assert (await client.get(f'/api/v1/evaluation-jobs/{key}?session_id={other.snapshot.session_id}',
                headers={'X-Session-Token':other.access_token})).status_code==404
            exported=await client.get(f'/api/v1/evaluation-jobs/{key}/report?session_id={payload.session_id}&download=true',headers={'X-Session-Token':token})
            assert exported.status_code==200 and 'attachment' in exported.headers['content-disposition']
            assert exported.json()==result
            assert (await client.get('/app/evaluations')).status_code==200
            assert (await client.get('/app/evaluations.js')).status_code==200
        with application.backend._connect() as connection:
            assert connection.execute('SELECT count(*) AS n FROM recommendation_requests').fetchone()['n']==0
            stored=connection.execute('SELECT frozen_input FROM evaluation_jobs WHERE job_id=%s',(key,)).fetchone()['frozen_input']
            assert token not in str(stored) and 'session_token' not in str(stored)
    asyncio.run(exercise())


@pytest.mark.parametrize('change',['duplicate','time','seen','foreign'])
def test_reject_labels_before_queue_without_partial_insert(isolated_database,monkeypatch,tmp_path,change):
    application,_=published_application(monkeypatch,tmp_path)
    async def exercise():
        token,payload=await inputs(application)
        data=payload.model_dump(mode='json')
        if change=='duplicate':
            data['cases'].append(data['cases'][0])
            with pytest.raises(ValueError): EvaluationInput(**data)
            return
        if change=='time': data['cases'][0]['target_at']='2000-01-01T00:00:00+00:00'
        if change=='seen':
            # A server-owned snapshot containing the target in history, not a user claim.
            with application.backend._connect() as connection:
                connection.execute("UPDATE strategy_comparisons SET result=jsonb_set(result,'{session,history}','[\"alpha\"]') WHERE comparison_id=%s",
                                   (data['cases'][0]['comparison_id'],))
        if change=='foreign': data['cases'][0]['comparison_id']=str(uuid4())
        with pytest.raises(ResourceNotFound if change=='foreign' else ManagementError):
            await application.evaluation_jobs.enqueue(uuid4(),token,EvaluationInput(**data))
        with application.backend._connect() as connection:
            assert connection.execute('SELECT count(*) AS n FROM evaluation_jobs').fetchone()['n']==0
    asyncio.run(exercise())


@pytest.mark.parametrize('phase',['queued','running'])
def test_cancellation_cpu_drain_no_partial_report(isolated_database,monkeypatch,tmp_path,phase):
    application,_=published_application(monkeypatch,tmp_path)
    service=application.evaluation_jobs
    entered,release=threading.Event(),threading.Event()
    original=application.backend.rank
    async def paused(context,command):
        entered.set()
        assert await asyncio.to_thread(release.wait,10)
        return await original(context,command)
    async def exercise():
        token,payload=await inputs(application)
        monkeypatch.setattr(application.backend,'rank',paused)
        key=uuid4();await service.enqueue(key,token,payload)
        work=None
        try:
            if phase=='running':
                work=asyncio.create_task(service.run_next())
                assert await asyncio.to_thread(entered.wait,10)
            cancelled=await service.cancel(key,payload.session_id,token)
            assert cancelled['status']==('cancelled' if phase=='queued' else 'cancelling')
            if work:
                assert not work.done()
                assert not await service.run_next()
                release.set();assert await work
            assert (await service.get(key,payload.session_id,token))['status']=='cancelled'
            with pytest.raises(ManagementError): await service.result(key,payload.session_id,token)
        finally:
            release.set()
            if work: await work
    asyncio.run(exercise())


def test_case_failures_unknown_no_private_error_and_recovering_worker(isolated_database,monkeypatch,tmp_path):
    application,_=published_application(monkeypatch,tmp_path)
    async def exercise():
        token,payload=await inputs(application)
        async def fail(context,command): raise RuntimeError('private traceback marker')
        monkeypatch.setattr(application.backend,'rank',fail)
        key=uuid4();await application.evaluation_jobs.enqueue(key,token,payload)
        assert await application.evaluation_jobs.run_next()
        result=await application.evaluation_jobs.result(key,payload.session_id,token)
        assert 'private traceback marker' not in str(result)
        assert result['groups']['all_cases']['dense']['failed_cases']==2
        assert result['groups']['all_cases']['dense']['metrics']['recall@2'] is None
    asyncio.run(exercise())


def test_stale_attempt_cannot_write_and_independent_process_worker(isolated_database,monkeypatch,tmp_path):
    application,_=published_application(monkeypatch,tmp_path)
    async def exercise():
        token,payload=await inputs(application)
        key=uuid4();await application.evaluation_jobs.enqueue(key,token,payload)
        lock,claim=application.evaluation_jobs._claim()
        try:
            with application.backend._connect() as connection:
                connection.execute("UPDATE evaluation_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE job_id=%s",(key,))
            assert not await application.evaluation_jobs.run_next() # live CPU-owner advisory lock
            with pytest.raises(ComparisonLeaseLost): application.evaluation_jobs._finish(claim,error_code='old')
        finally: lock.close()
        worker=subprocess.run([sys.executable,'-m','scripts.evaluation_worker','--once'],
            cwd=Path(__file__).resolve().parents[1],capture_output=True,timeout=30,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        assert worker.returncode==0, 'owned worker failed'
        state=await application.evaluation_jobs.get(key,payload.session_id,token)
        assert state['status']=='completed' and state['attempts']==2
        with pytest.raises(ComparisonLeaseLost): application.evaluation_jobs._finish(claim,error_code='late')
    asyncio.run(exercise())


def test_r06_frozen_manifest_catalog_time_cold_group_and_true_top20(online):
    application,identity,manifest=online
    async def exercise():
        token,payload=await inputs(application,1)
        # Known fixture e is model-cold, available at timestamp 10 but not in frozen popular stats.
        payload=payload.model_copy(update={'k':20,'cases':[payload.cases[0].model_copy(update={'target_item_id':'e'})]})
        key=uuid4();await application.evaluation_jobs.enqueue(key,token,payload)
        with application.backend._connect() as connection:
            connection.execute('UPDATE items SET r06_model_text=NULL,r06_first_seen_ms=NULL,is_active=false')
            connection.execute('UPDATE catalog_control SET admission_open=false')
        assert await application.evaluation_jobs.run_next()
        result=await application.evaluation_jobs.result(key,payload.session_id,token)
        assert result['configuration']['bundle_id']==str(identity)
        assert result['configuration']['manifest_sha256']==manifest
        assert result['configuration']['requested_k']==20
        assert result['cases'][0]['target_training_item'] is False
        assert result['groups']['model_cold_available']['dense']['cases']==1
        assert result['groups']['all_cases']['popular']['metrics']['recall@20']==0
        assert 'ndcg@10' in result['groups']['all_cases']['dense']['metrics']
        assert all(entry['status']=='completed' for entry in result['cases'][0]['strategies'].values())
    asyncio.run(exercise())


def test_api_submission_validation_status_cancel_and_replay(isolated_database,monkeypatch,tmp_path):
    application,_=published_application(monkeypatch,tmp_path)
    async def exercise():
        token,payload=await inputs(application,1)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(demo_application=application)),base_url='http://test') as client:
            key=str(uuid4());headers={'X-Session-Token':token,'Idempotency-Key':key}
            data=payload.model_dump(mode='json');root='/api/v1/evaluation-jobs'
            assert (await client.post(root,json=data,headers={**headers,'X-Session-Token':'wrong'})).status_code==401
            assert (await client.post(root,json={**data,'k':True},headers=headers)).status_code==422
            assert (await client.post(root,json={**data,'k':51},headers=headers)).status_code==422
            queued=await client.post(root,json=data,headers=headers)
            assert queued.status_code==202 and queued.json()['job_id']==key
            assert (await client.post(root,json=data,headers=headers)).json()==queued.json()
            assert (await client.post(root,json={**data,'label_origin':'manual'},headers=headers)).status_code==409
            query=f'?session_id={payload.session_id}'
            assert (await client.get(root+query+'&limit=0',headers=headers)).status_code==422
            assert (await client.get(root+'/'+key+'/report'+query,headers=headers)).status_code==409
            cancelled=await client.post(root+'/'+key+'/cancel'+query,headers=headers)
            assert cancelled.json()['status']=='cancelled'
            new=str(uuid4())
            replay=await client.post(root+'/'+key+'/replay'+query,headers={**headers,'Idempotency-Key':new})
            assert replay.status_code==202 and replay.json()['replay_of']==key
            assert await application.evaluation_jobs.run_next()
            result=await client.get(root+'/'+new+'/report'+query,headers=headers)
            assert result.status_code==200
            assert (await client.get(root+query,headers=headers)).json()['items'][0]['job_id']==new
    asyncio.run(exercise())


def test_queue_admission_limit_and_retry_bypasses_limit(isolated_database,monkeypatch,tmp_path):
    application,_=published_application(monkeypatch,tmp_path)
    async def exercise():
        token,payload=await inputs(application,1)
        keys=[uuid4() for _ in range(5)]
        for key in keys: await application.evaluation_jobs.enqueue(key,token,payload)
        with pytest.raises(ManagementError) as error:
            await application.evaluation_jobs.enqueue(uuid4(),token,payload)
        assert error.value.code=='evaluation_queue_full' and error.value.status_code==429
        assert (await application.evaluation_jobs.enqueue(keys[0],token,payload))['job_id']==keys[0]
    asyncio.run(exercise())
