"""Frozen-input comparison queue with leases, generation fencing and cooperative cancellation."""

import asyncio
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from evorec.application.compare import CompareStrategies, ComparisonCommand
from evorec.domain.errors import (
    IdempotencyConflict, ManagementError, ResourceNotFound, SnapshotMismatch, UnreportedFallback,
)
from evorec.domain.models import Strategy
from evorec.infrastructure.comparison_store import PostgresComparisonStore, decode_result, encode_result
from evorec.infrastructure.r06_async import _drain


class ComparisonCancelled(Exception):
    """Cancellation is acknowledged after the current ranking path has returned."""


class ComparisonLeaseLost(Exception):
    """An expired or replaced attempt must not persist output."""


class ComparisonJobService:
    LEASE_SECONDS = 30
    HEARTBEAT_SECONDS = 5
    MAX_ATTEMPTS = 3

    def __init__(self, compare: CompareStrategies, records: PostgresComparisonStore):
        self.compare, self.records = compare, records
        self.backend = records.backend

    @staticmethod
    def _public(row) -> dict:
        snapshot = row['frozen_input']
        return {
            'job_id': row['job_id'], 'session_id': row['session_id'], 'status': row['status'],
            'completed_strategies': row['completed_strategies'],
            'total_strategies': len(row['requested_strategies']), 'attempts': row['attempts'],
            'cancel_requested': row['cancel_requested'], 'error_code': row['error_code'],
            'comparison_id': row['job_id'] if row['status'] == 'completed' else None,
            'bundle_id': snapshot['catalog']['bundle_id'],
            'history_version': snapshot['session']['history_version'],
            'snapshot_at': snapshot['snapshot_at'],
            'created_at': row['created_at'], 'updated_at': row['updated_at'],
        }

    @staticmethod
    def _matching(row, command):
        if row['session_id'] != command.session_id or row['input_sha256'].strip() != command.input_sha256:
            raise IdempotencyConflict('comparison job key was used for different input')
        return row

    async def enqueue(self, command: ComparisonCommand) -> dict:
        async with asyncio.timeout(command.timeout_seconds):
            prior = await asyncio.to_thread(self._find, command)
            if prior:
                return self._public(prior)
            snapshot = await self.compare.capture(command)
            frozen = encode_result(snapshot)
            runtime = self.backend.runtimes.get(str(snapshot.binding.bundle_id))
            frozen['runtime_manifest_sha256'] = runtime.manifest_sha256 if runtime else None
            row = await asyncio.to_thread(self._enqueue, command, frozen)
            return self._public(row)

    def _find(self, command):
        with self.backend._connect() as connection:
            self.records._authorize(connection, command.session_id, command.session_token)
            row = connection.execute('SELECT * FROM strategy_comparison_jobs WHERE job_id = %s',
                                     (command.comparison_id,)).fetchone()
            return self._matching(row, command) if row else None

    def _enqueue(self, command, frozen):
        with self.backend._connect() as connection:
            self.records._authorize(connection, command.session_id, command.session_token)
            # Shared namespace with synchronous saves; the global lock also bounds queue admission.
            connection.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))',
                               (f'evorec:comparison-id:{command.comparison_id}',))
            row = connection.execute('SELECT * FROM strategy_comparison_jobs WHERE job_id = %s',
                                     (command.comparison_id,)).fetchone()
            if row:
                return self._matching(row, command)
            if connection.execute('SELECT 1 FROM strategy_comparisons WHERE comparison_id = %s',
                                  (command.comparison_id,)).fetchone():
                raise IdempotencyConflict('key belongs to a synchronous comparison')
            connection.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))',
                               ('evorec:comparison-queue-admission',))
            counts = connection.execute(
                "SELECT count(*) AS total, count(*) FILTER (WHERE session_id = %s) AS own "
                "FROM strategy_comparison_jobs WHERE status IN ('queued', 'running', 'cancelling')",
                (command.session_id,),
            ).fetchone()
            if counts['own'] >= 10 or counts['total'] >= 1000:
                raise ManagementError('comparison_queue_full', 'comparison queue is full', 429)
            return connection.execute(
                'INSERT INTO strategy_comparison_jobs(job_id, session_id, input_sha256, '
                'frozen_input, requested_strategies) VALUES (%s, %s, %s, %s, %s) RETURNING *',
                (command.comparison_id, command.session_id, command.input_sha256, Jsonb(frozen),
                 Jsonb([strategy.value for strategy in command.strategies])),
            ).fetchone()

    async def get(self, job_id: UUID, session_id: UUID, token: str) -> dict:
        return await asyncio.to_thread(self._get, job_id, session_id, token, False)

    async def cancel(self, job_id: UUID, session_id: UUID, token: str) -> dict:
        return await asyncio.to_thread(self._get, job_id, session_id, token, True)

    def _get(self, job_id, session_id, token, cancel):
        with self.backend._connect() as connection:
            self.records._authorize(connection, session_id, token)
            row = connection.execute(
                'SELECT * FROM strategy_comparison_jobs WHERE job_id = %s AND session_id = %s'
                + (' FOR UPDATE' if cancel else ''),
                (job_id, session_id),
            ).fetchone()
            if row is None:
                raise ResourceNotFound('comparison job does not exist for this session')
            if cancel and row['status'] in ('queued', 'running', 'cancelling'):
                row = connection.execute(
                    "UPDATE strategy_comparison_jobs SET cancel_requested = true, "
                    "status = CASE WHEN status = 'queued' THEN 'cancelled' ELSE 'cancelling' END, "
                    'updated_at = clock_timestamp() WHERE job_id = %s RETURNING *', (job_id,),
                ).fetchone()
            return self._public(row)

    def _claim(self):
        connection = self.backend._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM strategy_comparison_jobs WHERE status = 'queued' OR "
                "(status IN ('running', 'cancelling') AND lease_until <= clock_timestamp()) "
                'ORDER BY created_at, job_id LIMIT 20 FOR UPDATE SKIP LOCKED',
            ).fetchall()
            for row in rows:
                # Hold a session lock until underlying computation exits: a live CPU path cannot
                # be reclaimed or acknowledged cancelled merely because its heartbeat was delayed.
                locked = connection.execute('SELECT pg_try_advisory_lock(hashtextextended(%s, 0)) AS locked',
                                            (f'evorec:comparison-worker:{row["job_id"]}',)).fetchone()['locked']
                if not locked:
                    continue
                if row['attempts'] >= self.MAX_ATTEMPTS and not row['cancel_requested']:
                    connection.execute(
                        "UPDATE strategy_comparison_jobs SET status = 'failed', lease_owner = NULL, "
                        "lease_until = NULL, error_code = 'attempts_exhausted', updated_at = clock_timestamp() "
                        'WHERE job_id = %s', (row['job_id'],),
                    )
                    connection.commit()
                    connection.close()
                    return None, True
                row = connection.execute(
                    "UPDATE strategy_comparison_jobs SET status = CASE WHEN cancel_requested THEN 'cancelling' "
                    "ELSE 'running' END, attempts = LEAST(attempts + 1, 3), completed_strategies = 0, "
                    'lease_owner = %s, lease_until = clock_timestamp() + %s * interval \'1 second\', '
                    'error_code = NULL, updated_at = clock_timestamp() WHERE job_id = %s RETURNING *',
                    (uuid4(), self.LEASE_SECONDS, row['job_id']),
                ).fetchone()
                connection.commit()
                return connection, row
            connection.commit()
            connection.close()
            return None, False
        except BaseException:
            connection.close()
            raise

    def _touch(self, claim, completed=None):
        with self.backend._connect() as connection:
            row = connection.execute(
                'UPDATE strategy_comparison_jobs SET lease_until = clock_timestamp() + %s * interval \'1 second\', '
                'completed_strategies = COALESCE(%s, completed_strategies), updated_at = clock_timestamp() '
                "WHERE job_id = %s AND lease_owner = %s AND attempts = %s AND status IN ('running', 'cancelling') "
                'AND lease_until > clock_timestamp() RETURNING cancel_requested',
                (self.LEASE_SECONDS, completed, claim['job_id'], claim['lease_owner'], claim['attempts']),
            ).fetchone()
            if row is None:
                raise ComparisonLeaseLost()
            return row['cancel_requested']

    def _finish(self, claim, result=None, error_code=None):
        with self.backend._connect() as connection:
            row = connection.execute(
                "SELECT * FROM strategy_comparison_jobs WHERE job_id = %s AND lease_owner = %s "
                "AND attempts = %s AND status IN ('running', 'cancelling') "
                'AND lease_until > clock_timestamp() FOR UPDATE',
                (claim['job_id'], claim['lease_owner'], claim['attempts']),
            ).fetchone()
            if row is None:
                raise ComparisonLeaseLost()
            status = 'cancelled' if row['cancel_requested'] else 'failed' if error_code else 'completed'
            if status == 'completed':
                frozen = decode_result(row['frozen_input'])
                if (result is None or result.context != frozen.context
                        or result.snapshot_at != frozen.snapshot_at or result.requested_k != frozen.requested_k
                        or [entry.requested_strategy.value for entry in result.strategies] != row['requested_strategies']):
                    raise ValueError('complete comparison result is required')
                connection.execute(
                    'INSERT INTO strategy_comparisons(comparison_id, session_id, input_sha256, result) '
                    'VALUES (%s, %s, %s, %s)',
                    (claim['job_id'], claim['session_id'], claim['input_sha256'], Jsonb(encode_result(result))),
                )
            connection.execute(
                'UPDATE strategy_comparison_jobs SET status = %s, error_code = %s, lease_owner = NULL, '
                'lease_until = NULL, updated_at = clock_timestamp() WHERE job_id = %s',
                (status, error_code if status == 'failed' else None, claim['job_id']),
            )

    async def _execute(self, claim):
        snapshot = decode_result(claim['frozen_input'])
        command = ComparisonCommand(claim['job_id'], claim['session_id'], 'internal-worker',
                                    snapshot.binding.history_version,
                                    tuple(Strategy(s) for s in claim['requested_strategies']),
                                    snapshot.requested_k)
        expected_manifest = claim['frozen_input']['runtime_manifest_sha256']
        bundle_id = snapshot.binding.bundle_id
        if expected_manifest:
            with self.backend._connect() as connection:
                runtime = self.backend.manager._registered_runtime(
                    connection, bundle_id, frozen_snapshot=snapshot.context.model is not None,
                )
            if runtime.manifest_sha256 != expected_manifest:
                raise ManagementError('bundle_changed', 'frozen runtime manifest changed', 503)
            self.backend.runtimes[str(bundle_id)] = runtime
        else:
            self.backend.runtimes.pop(str(bundle_id), None)

        async def progress(count):
            if await asyncio.to_thread(self._touch, claim, count):
                raise ComparisonCancelled()

        await progress(0)
        return await self.compare.execute(command, snapshot, progress)

    async def run_next(self) -> bool:
        lock_connection, claim = await asyncio.to_thread(self._claim)
        if lock_connection is None:
            return bool(claim)
        async def heartbeat():
            while True:
                await asyncio.sleep(self.HEARTBEAT_SECONDS)
                await asyncio.to_thread(self._touch, claim)

        pulse = asyncio.create_task(heartbeat())
        work = None
        try:
            # Keep blocking CPU ranking off the heartbeat event loop. Never acknowledge cancellation
            # or release the advisory lock before this underlying thread actually finishes.
            try:
                if claim['cancel_requested']:
                    raise ComparisonCancelled()
                work = asyncio.create_task(asyncio.to_thread(lambda: asyncio.run(self._execute(claim))))
                result = await asyncio.shield(work)
            except asyncio.CancelledError:
                if work is not None:
                    await _drain(work)
                raise
            except ComparisonCancelled:
                await asyncio.to_thread(self._finish, claim, error_code='cancelled')
            except ComparisonLeaseLost:
                pass
            except (RuntimeError, TimeoutError, ManagementError, ValueError, SnapshotMismatch, UnreportedFallback) as exc:
                if isinstance(exc, ManagementError):
                    code = exc.code
                elif isinstance(exc, TimeoutError):
                    code = 'comparison_timeout'
                else:
                    code = 'comparison_failed'
                await asyncio.to_thread(self._finish, claim, error_code=code)
            else:
                await asyncio.to_thread(self._finish, claim, result)
        except ComparisonLeaseLost:
            pass
        finally:
            pulse.cancel()
            await asyncio.gather(pulse, return_exceptions=True)
            await asyncio.to_thread(lock_connection.close)
        return True
