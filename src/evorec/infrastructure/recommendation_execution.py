"""Request-scoped PostgreSQL execution leases, not timed crash detection.

A lost session lock permits terminal reconciliation, never reranking old input.
An extra connection remains owned until admission, CPU and result writes drain.
"""

from contextlib import contextmanager
from threading import Lock
from uuid import uuid4

from psycopg.pq import TransactionStatus

from evorec.domain.errors import IdempotencyInProgress, ManagementError, ResourceNotFound


class RecommendationExecution:
    def __init__(self, backend, command):
        self.backend, self.command = backend, command
        self.owner = uuid4()
        self.connection = None
        self.lock_key = None
        self.admitted = False
        self._result_lock = Lock()

    def admit(self):
        connection = self.connection = self.backend._connect()
        connection.autocommit = True
        previous = connection.execute(
            "SELECT s.owner_token_sha256, r.session_id, r.history_version, r.requested_strategy, "
            "r.requested_k, r.status, r.execution_owner, "
            "hashtextextended('evorec:recommendation:' || current_schema() || ':' || %s, 0) AS execution_key "
            "FROM sessions s LEFT JOIN recommendation_requests r ON r.request_id = %s WHERE s.session_id = %s",
            (str(self.command.request_id), self.command.request_id, self.command.session_id),
        ).fetchone()
        if previous is None:
            raise ResourceNotFound("session does not exist")
        self.backend._authorize(previous, self.command.session_token)
        if previous["status"] is not None:
            self.backend._matching_request(previous, self.command)
            if previous["status"] != "accepted":
                # Durable completed/failed rows need no execution lease to replay.
                return self.backend._admit(self.command)
            if previous["execution_owner"] is None:
                raise ManagementError("recommendation_recovery_unavailable",
                                      "legacy execution liveness is unknown; administrator reconciliation required", 409)
        self.lock_key = previous["execution_key"]
        if not connection.execute("SELECT pg_try_advisory_lock(%s) AS held", (self.lock_key,)).fetchone()["held"]:
            raise IdempotencyInProgress("recommendation is still executing")
        with self.backend._execution_lock:
            self.backend._executions[self.command.request_id] = self
        context = self.backend._admit(self.command)
        self.admitted = True
        return context

    def assert_held(self):
        # Query on the owning connection: a disconnected or explicitly unlocked
        # executor cannot write output merely because its process is still alive.
        held = self.connection.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' "
            "AND pid = pg_backend_pid() AND granted AND objsubid = 1 "
            "AND classid::bigint = %s AND objid::bigint = %s) AS held",
            ((self.lock_key >> 32) & 0xffffffff, self.lock_key & 0xffffffff),
        ).fetchone()["held"]
        if not held:
            raise ManagementError("recommendation_execution_lost", "execution lease is no longer held", 503)

    @contextmanager
    def result_transaction(self):
        """Borrow the owned session without closing it or sharing a transaction.

        Admission keeps its separate transaction (including orphan reconciliation).
        save() drains its worker before acquire() closes this session, even when
        repeatedly cancelled. The session lock survives commit and rollback.
        """
        with self._result_lock:
            if (self.connection is None or not self.connection.autocommit
                    or self.connection.info.transaction_status != TransactionStatus.IDLE):
                raise ManagementError("recommendation_execution_lost", "execution session is not idle", 503)
            with self.connection.transaction():
                yield self.connection

    def close(self):
        with self.backend._execution_lock:
            if self.backend._executions.get(self.command.request_id) is self:
                del self.backend._executions[self.command.request_id]
        if self.connection is not None:
            self.connection.close()
