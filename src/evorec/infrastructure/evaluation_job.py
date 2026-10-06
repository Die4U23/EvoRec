"""Persistent batch evaluation over immutable owned comparison snapshots.

Reuses the comparison worker's CPU drain/heartbeat lifecycle, not its SQL tables.
Labels are never supplied to ranking. An unverified label upload is not R06 research replication.
"""

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
from uuid import uuid4

from psycopg.types.json import Jsonb

from evorec.application.compare import ComparisonCommand
from evorec.application.evaluation import aggregate, digest, metrics
from evorec.domain.errors import IdempotencyConflict, ManagementError, ResourceNotFound, SnapshotMismatch, UnreportedFallback
from evorec.domain.models import Strategy
from evorec.infrastructure.comparison_job import (
    ComparisonCancelled, ComparisonJobService, ComparisonLeaseLost,
)
from evorec.infrastructure.comparison_store import decode_result


class EvaluationJobService(ComparisonJobService):
    ERROR_PREFIX = 'evaluation'
    # Poll/list must not deserialize the potentially 20 MiB frozen catalog.
    SUMMARY_SQL = ("job_id,session_id,status,completed_cases,total_cases,attempts,cancel_requested,"
        "error_code,replay_of,created_at,updated_at,jsonb_build_object('request',jsonb_build_object("
        "'dataset_name',frozen_input #> '{request,dataset_name}','label_origin',frozen_input #> '{request,label_origin}'),"
        "'dataset_sha256',frozen_input -> 'dataset_sha256') AS frozen_input")

    @staticmethod
    def _public(row):
        frozen = row['frozen_input']
        return dict(job_id=row['job_id'], session_id=row['session_id'], status=row['status'],
            completed_cases=row['completed_cases'], total_cases=row['total_cases'], attempts=row['attempts'],
            cancel_requested=row['cancel_requested'], error_code=row['error_code'], replay_of=row['replay_of'],
            dataset_name=frozen['request']['dataset_name'], label_origin=frozen['request']['label_origin'],
            dataset_sha256=frozen['dataset_sha256'], protocol='held-out-saved-snapshot-v1',
            created_at=row['created_at'], updated_at=row['updated_at'])

    async def enqueue(self, job_id, token, payload):
        request = payload.model_dump(mode='json')
        return await asyncio.to_thread(self._enqueue_evaluation, job_id, payload.session_id, token, request)

    def _insert(self, connection, job_id, session_id, frozen, replay_of=None):
        key = digest(dict(request=frozen['request'], replay_of=str(replay_of) if replay_of else None))
        connection.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))',
                           (f'evorec:evaluation:{job_id}',))
        existing = connection.execute('SELECT '+self.SUMMARY_SQL+',input_sha256 FROM evaluation_jobs WHERE job_id=%s', (job_id,)).fetchone()
        if existing:
            if existing['session_id'] != session_id or existing['input_sha256'].strip() != key:
                raise IdempotencyConflict('evaluation key belongs to different input')
            return self._public(existing)
        connection.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))',
                           ('evorec:evaluation-admission',))
        counts = connection.execute("SELECT count(*) AS total, count(*) FILTER (WHERE session_id=%s) AS own "
            "FROM evaluation_jobs WHERE status IN ('queued','running','cancelling')", (session_id,)).fetchone()
        if counts['own'] >= 5 or counts['total'] >= 100:
            raise ManagementError('evaluation_queue_full', 'evaluation queue is full', 429)
        if len(json.dumps(frozen, ensure_ascii=False).encode('utf-8')) > 20*1024*1024:
            raise ManagementError('evaluation_input_too_large', 'frozen input exceeds 20 MiB', 422)
        row = connection.execute('INSERT INTO evaluation_jobs(job_id,session_id,input_sha256,frozen_input,'
            'replay_of,total_cases) VALUES (%s,%s,%s,%s,%s,%s) RETURNING '+self.SUMMARY_SQL,
            (job_id, session_id, key, Jsonb(frozen), replay_of, len(frozen['cases']))).fetchone()
        return self._public(row)

    def _enqueue_evaluation(self, job_id, session_id, token, request):
        with self.backend._connect() as connection:
            self.records._authorize(connection, session_id, token)
            existing = connection.execute('SELECT 1 FROM evaluation_jobs WHERE job_id=%s', (job_id,)).fetchone()
            if existing:
                return self._insert(connection, job_id, session_id,
                    dict(request=request, cases=[], dataset_sha256=digest(request)))
            catalogs, cases = {}, []
            signature = None
            for label in request['cases']:
                row = connection.execute('SELECT result FROM strategy_comparisons WHERE comparison_id=%s '
                    'AND session_id=%s', (label['comparison_id'], session_id)).fetchone()
                if row is None:
                    raise ResourceNotFound('a completed owned comparison is required for each case')
                value = dict(row['result'])
                snapshot = decode_result(value)
                seen = snapshot.context.model.full_seen if snapshot.context.model else frozenset(
                    snapshot.context.session.history) | snapshot.context.session.hidden_items
                if label['target_item_id'] in seen:
                    raise ManagementError('evaluation_label_leakage', 'target already appears in input/seen', 422)
                if datetime.fromisoformat(label['target_at']) <= snapshot.snapshot_at:
                    raise ManagementError('evaluation_label_time', 'target event must follow captured input', 422)
                strategies = [entry.requested_strategy.value for entry in snapshot.strategies]
                identity = dict(bundle_id=str(snapshot.binding.bundle_id),
                    manifest_sha256=value['model_snapshot']['manifest_sha256'] if value['model_snapshot'] else None,
                    model_version=value['model_snapshot']['model_version'] if value['model_snapshot'] else None,
                    requested_k=request['k'], strategies=strategies,
                    catalog_sha256=digest(value['catalog']))
                if signature is not None and identity != signature:
                    raise ManagementError('evaluation_protocol_mismatch', 'cases must share bundle, model, K and strategies', 422)
                signature = identity
                catalog = value.pop('catalog')
                catalog_key = digest(catalog)
                catalogs[catalog_key] = catalog
                value['strategies'], value['common_item_ids'] = [], []
                value['runtime_manifest_sha256'] = connection.execute(
                    'SELECT manifest_sha256 FROM bundle_versions WHERE bundle_id=%s',
                    (snapshot.binding.bundle_id,)).fetchone()['manifest_sha256']
                cases.append(dict(label=label, snapshot=value, catalog_key=catalog_key,
                                  input_snapshot_sha256=digest(dict(snapshot=value, catalog=catalog))))
            return self._insert(connection, job_id, session_id, dict(request=request,
                dataset_sha256=digest(request), catalogs=catalogs, cases=cases, identity=signature))

    async def replay(self, original_id, job_id, session_id, token):
        def submit():
            with self.backend._connect() as connection:
                self.records._authorize(connection, session_id, token)
                row = connection.execute('SELECT * FROM evaluation_jobs WHERE job_id=%s AND session_id=%s',
                                         (original_id, session_id)).fetchone()
                if row is None:
                    raise ResourceNotFound('evaluation does not exist for this session')
                return self._insert(connection, job_id, session_id, row['frozen_input'], original_id)
        return await asyncio.to_thread(submit)

    def _get(self, job_id, session_id, token, cancel):
        with self.backend._connect() as connection:
            self.records._authorize(connection, session_id, token)
            row = connection.execute('SELECT '+self.SUMMARY_SQL+' FROM evaluation_jobs WHERE job_id=%s AND session_id=%s'
                + (' FOR UPDATE' if cancel else ''), (job_id, session_id)).fetchone()
            if row is None:
                raise ResourceNotFound('evaluation does not exist for this session')
            if cancel and row['status'] in ('queued','running','cancelling'):
                row = connection.execute("UPDATE evaluation_jobs SET cancel_requested=true, "
                    "status=CASE WHEN status='queued' THEN 'cancelled' ELSE 'cancelling' END, "
                    'updated_at=clock_timestamp() WHERE job_id=%s RETURNING '+self.SUMMARY_SQL, (job_id,)).fetchone()
            return self._public(row)

    async def list(self, session_id, token, offset=0, limit=20):
        def read():
            with self.backend._connect() as connection:
                self.records._authorize(connection, session_id, token)
                rows = connection.execute('SELECT '+self.SUMMARY_SQL+' FROM evaluation_jobs WHERE session_id=%s '
                    'ORDER BY created_at DESC,job_id DESC OFFSET %s LIMIT %s',
                    (session_id, offset, limit+1)).fetchall()
                return dict(items=[self._public(row) for row in rows[:limit]],
                            offset=offset, limit=limit, has_more=len(rows)>limit)
        return await asyncio.to_thread(read)

    async def result(self, job_id, session_id, token):
        def read():
            with self.backend._connect() as connection:
                self.records._authorize(connection, session_id, token)
                row = connection.execute('SELECT result FROM evaluation_jobs WHERE job_id=%s AND session_id=%s',
                                         (job_id, session_id)).fetchone()
                if row is None:
                    raise ResourceNotFound('evaluation does not exist for this session')
                if row['result'] is None:
                    raise ManagementError('evaluation_result_not_ready', 'only complete reports can be exported', 409)
                return row['result']
        return await asyncio.to_thread(read)

    def _claim(self):
        connection = self.backend._connect()
        try:
            rows = connection.execute("SELECT * FROM evaluation_jobs WHERE status='queued' OR "
                "(status IN ('running','cancelling') AND lease_until <= clock_timestamp()) "
                'ORDER BY created_at,job_id LIMIT 20 FOR UPDATE SKIP LOCKED').fetchall()
            for row in rows:
                if not connection.execute('SELECT pg_try_advisory_lock(hashtextextended(%s,0)) AS locked',
                    (f'evorec:evaluation-worker:{row["job_id"]}',)).fetchone()['locked']:
                    continue
                if row['attempts'] >= self.MAX_ATTEMPTS and not row['cancel_requested']:
                    connection.execute("UPDATE evaluation_jobs SET status='failed',lease_owner=NULL,lease_until=NULL,"
                        "error_code='attempts_exhausted',updated_at=clock_timestamp() WHERE job_id=%s", (row['job_id'],))
                    connection.commit()
                    connection.close()
                    return None, True
                claim = connection.execute("UPDATE evaluation_jobs SET status=CASE WHEN cancel_requested "
                    "THEN 'cancelling' ELSE 'running' END,attempts=LEAST(attempts+1,3),completed_cases=0,"
                    "lease_owner=%s,lease_until=clock_timestamp()+%s*interval '1 second',error_code=NULL,"
                    'updated_at=clock_timestamp() WHERE job_id=%s RETURNING *',
                    (uuid4(),self.LEASE_SECONDS,row['job_id'])).fetchone()
                connection.commit()
                return connection, claim
            connection.commit()
            connection.close()
            return None, False
        except BaseException:
            connection.close()
            raise

    def _touch(self, claim, completed=None):
        with self.backend._connect() as connection:
            row = connection.execute("UPDATE evaluation_jobs SET lease_until=clock_timestamp()+%s*interval '1 second',"
                'completed_cases=COALESCE(%s,completed_cases),updated_at=clock_timestamp() '
                "WHERE job_id=%s AND lease_owner=%s AND attempts=%s AND status IN ('running','cancelling') "
                'AND lease_until>clock_timestamp() RETURNING cancel_requested',
                (self.LEASE_SECONDS,completed,claim['job_id'],claim['lease_owner'],claim['attempts'])).fetchone()
            if row is None:
                raise ComparisonLeaseLost()
            return row['cancel_requested']

    def _finish(self, claim, result=None, error_code=None):
        with self.backend._connect() as connection:
            row = connection.execute("SELECT * FROM evaluation_jobs WHERE job_id=%s AND lease_owner=%s "
                "AND attempts=%s AND status IN ('running','cancelling') AND lease_until>clock_timestamp() FOR UPDATE",
                (claim['job_id'],claim['lease_owner'],claim['attempts'])).fetchone()
            if row is None:
                raise ComparisonLeaseLost()
            status = 'cancelled' if row['cancel_requested'] else 'failed' if error_code else 'completed'
            if status == 'completed' and (result is None or len(result['cases']) != row['total_cases']):
                raise ValueError('complete evaluation report required')
            connection.execute('UPDATE evaluation_jobs SET status=%s,result=%s,error_code=%s,lease_owner=NULL,'
                'lease_until=NULL,updated_at=clock_timestamp() WHERE job_id=%s',
                (status, Jsonb(result) if status == 'completed' else None,
                 error_code if status == 'failed' else None,claim['job_id']))

    async def _execute(self, claim):
        frozen, rows = claim['frozen_input'], []
        identity = frozen['identity']
        bundle_id = identity['bundle_id']
        # Load once per attempt, pinned to registration. Mutable online admission is not consulted.
        with self.backend._connect() as connection:
            runtime = self.backend.manager._registered_runtime(connection, bundle_id,
                frozen_snapshot=identity['manifest_sha256'] is not None)
        expected = frozen['cases'][0]['snapshot']['runtime_manifest_sha256']
        if runtime.manifest_sha256 != expected or (identity['manifest_sha256'] is not None
                and expected != identity['manifest_sha256']):
            raise ManagementError('evaluation_bundle_changed', 'registered runtime changed', 503)
        self.backend.runtimes[bundle_id] = runtime
        strategies = tuple(Strategy(name) for name in identity['strategies'])
        for index, case in enumerate(frozen['cases']):
            if await asyncio.to_thread(self._touch, claim, index):
                raise ComparisonCancelled()
            value = {**case['snapshot'], 'catalog': frozen['catalogs'][case['catalog_key']]}
            snapshot = decode_result(value)
            if digest(dict(snapshot=case['snapshot'],catalog=value['catalog'])) != case['input_snapshot_sha256']:
                raise SnapshotMismatch('frozen evaluation input changed')
            snapshot = replace(snapshot, requested_k=identity['requested_k'])
            label = case['label']
            target = label['target_item_id']
            groups = ['cohort:'+label['cohort'], 'history_present' if snapshot.context.session.history else 'cold_user']
            available = target in snapshot.context.catalog.eligible_items
            training_item = None
            if snapshot.context.model and target in runtime.bundle.adapter.features._indices:
                metadata = runtime.bundle.adapter.features._metadata[runtime.bundle.adapter.features._indices[target]]
                available = available and metadata.first_seen_ms < snapshot.context.model.timestamp_ms
                training_item = metadata.training_item
            if available:
                groups.append('available_target')
                if training_item is False:
                    groups.append('model_cold_available')
            command = ComparisonCommand(snapshot.binding.request_id, claim['session_id'], 'internal-worker',
                snapshot.binding.history_version, strategies, identity['requested_k'])
            entries = {}
            try:
                result = await self.compare.execute(command, snapshot)
                for entry in result.strategies:
                    items = [item.item_id for item in entry.items]
                    entries[entry.requested_strategy.value] = dict(status='completed', actual_strategy=entry.actual_strategy.value,
                        fallback_reason=entry.fallback_reason, items=items, ranking_elapsed_ms=entry.elapsed_ms,
                        metrics=metrics(items,target,identity['requested_k']), error_code=None)
            except (RuntimeError, TimeoutError, ManagementError, ValueError, SnapshotMismatch, UnreportedFallback) as error:
                # A partial strategy sequence is not comparable; mark every method unknown for this case.
                code = error.code if isinstance(error,ManagementError) else 'evaluation_case_timeout' if isinstance(error,TimeoutError) else 'evaluation_case_failed'
                entries = {strategy.value: dict(status='failed',actual_strategy=None,fallback_reason=None,
                    items=None,ranking_elapsed_ms=None,metrics=None,error_code=code) for strategy in strategies}
            rows.append(dict(comparison_id=label['comparison_id'],target_item_id=target,target_at=label['target_at'],
                input_snapshot_sha256=case['input_snapshot_sha256'],snapshot_at=snapshot.snapshot_at.isoformat(),
                history_version=snapshot.binding.history_version,groups=groups,target_available=available,
                target_training_item=training_item,strategies=entries))
        if await asyncio.to_thread(self._touch, claim, len(rows)):
            raise ComparisonCancelled()
        stable = [{**row,'strategies':{name:{key:value for key,value in entry.items() if key!='ranking_elapsed_ms'}
                  for name,entry in row['strategies'].items()}} for row in rows]
        return dict(protocol='held-out-saved-snapshot-v1',job_id=str(claim['job_id']),
            dataset_name=frozen['request']['dataset_name'],label_origin=frozen['request']['label_origin'],
            source_description=frozen['request']['source_description'],label_provenance_verified=False,
            dataset_sha256=frozen['dataset_sha256'],configuration=identity,
            frozen_inputs_sha256=digest(frozen),semantic_result_sha256=digest(stable),
            completed_at=datetime.now(timezone.utc).isoformat(),cases=rows,
            groups=aggregate(rows,identity['strategies'],identity['requested_k']),
            limits=['single_positive_label','not_original_r06_protocol_replication','ranking_time_not_http_latency',
                    'failed_cases_unknown_not_zero','cold_user_means_empty_frozen_history',
                    'adaptive_may_alias_dense','not_online_ctr_or_business_gain'])
