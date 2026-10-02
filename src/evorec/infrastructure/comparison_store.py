"""Immutable completed comparisons with ownership checks and atomic replay."""

import asyncio
from datetime import datetime
from uuid import UUID

from psycopg.types.json import Jsonb

from evorec.application.compare import (
    ComparedStrategy, ComparisonCommand, ComparisonPreview, ComparisonSummary,
)
from evorec.domain.errors import IdempotencyConflict, ResourceNotFound, SnapshotMismatch
from evorec.domain.models import (
    CatalogSnapshot, RequestContext, ScoredCandidate, SessionSnapshot, Strategy,
)
from evorec.infrastructure.postgres import PostgresDemoBackend


def encode_result(result: ComparisonPreview) -> dict:
    session, catalog = result.context.session, result.context.catalog
    return {
        "comparison_id": str(result.binding.request_id),
        "snapshot_at": result.snapshot_at.isoformat(), "requested_k": result.requested_k,
        "session": {"session_id": str(session.session_id), "epoch": session.epoch,
                    "history_version": session.history_version, "history": list(session.history),
                    "hidden_items": sorted(session.hidden_items),
                    "favorite_items": sorted(session.favorite_items), "profile_id": session.profile_id},
        "catalog": {"bundle_id": str(catalog.bundle_id), "exclusion_version": catalog.exclusion_version,
                    "eligible_items": sorted(catalog.eligible_items)},
        "common_item_ids": list(result.common_item_ids),
        "strategies": [{"requested_strategy": entry.requested_strategy.value,
                        "actual_strategy": entry.actual_strategy.value,
                        "fallback_reason": entry.fallback_reason, "elapsed_ms": entry.elapsed_ms,
                        "unique_item_ids": list(entry.unique_item_ids),
                        "items": [{"item_id": item.item_id, "score": item.score, "source": item.source}
                                  for item in entry.items]} for entry in result.strategies],
    }


def decode_result(data: dict) -> ComparisonPreview:
    session = SessionSnapshot(**{**data["session"], "session_id": UUID(data["session"]["session_id"])})
    catalog = CatalogSnapshot(**{**data["catalog"], "bundle_id": UUID(data["catalog"]["bundle_id"])})
    entries = tuple(ComparedStrategy(
        Strategy(entry["requested_strategy"]), Strategy(entry["actual_strategy"]),
        entry["fallback_reason"], entry["elapsed_ms"],
        tuple(ScoredCandidate(**item) for item in entry["items"]), tuple(entry["unique_item_ids"]),
    ) for entry in data["strategies"])
    return ComparisonPreview(RequestContext(UUID(data["comparison_id"]), session, catalog),
                             datetime.fromisoformat(data["snapshot_at"]),
                             tuple(data["common_item_ids"]), entries, data["requested_k"])


class PostgresComparisonStore:
    def __init__(self, backend: PostgresDemoBackend):
        self.backend = backend

    def _authorize(self, connection, session_id: UUID, token: str) -> None:
        row = connection.execute(
            "SELECT owner_token_sha256 FROM sessions WHERE session_id = %s", (session_id,),
        ).fetchone()
        if row is None:
            raise ResourceNotFound("session does not exist")
        self.backend._authorize(row, token)

    @staticmethod
    def _matching(row, command: ComparisonCommand) -> ComparisonPreview:
        if row["session_id"] != command.session_id or row["input_sha256"].strip() != command.input_sha256:
            raise IdempotencyConflict("comparison key was used for different input")
        return decode_result(row["result"])

    async def find(self, command: ComparisonCommand) -> ComparisonPreview | None:
        return await asyncio.to_thread(self._find, command)

    def _find(self, command: ComparisonCommand) -> ComparisonPreview | None:
        with self.backend._connect() as connection:
            self._authorize(connection, command.session_id, command.session_token)
            row = connection.execute(
                "SELECT session_id, input_sha256, result FROM strategy_comparisons WHERE comparison_id = %s",
                (command.comparison_id,),
            ).fetchone()
            if row is None and connection.execute(
                'SELECT 1 FROM strategy_comparison_jobs WHERE job_id = %s', (command.comparison_id,),
            ).fetchone():
                raise IdempotencyConflict('key belongs to a background comparison job')
            return self._matching(row, command) if row else None

    async def save(self, command: ComparisonCommand, result: ComparisonPreview) -> ComparisonPreview:
        if (result.binding.request_id != command.comparison_id
                or result.binding.session_id != command.session_id
                or result.binding.history_version != command.expected_history_version
                or result.requested_k != command.k
                or tuple(entry.requested_strategy for entry in result.strategies) != command.strategies):
            raise SnapshotMismatch("comparison result differs from its command")
        return await asyncio.to_thread(self._save, command, result)

    def _save(self, command: ComparisonCommand, result: ComparisonPreview) -> ComparisonPreview:
        with self.backend._connect() as connection:
            self._authorize(connection, command.session_id, command.session_token)
            connection.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))',
                               (f'evorec:comparison-id:{command.comparison_id}',))
            if connection.execute('SELECT 1 FROM strategy_comparison_jobs WHERE job_id = %s',
                                  (command.comparison_id,)).fetchone():
                raise IdempotencyConflict('key belongs to a background comparison job')
            connection.execute(
                "INSERT INTO strategy_comparisons(comparison_id, session_id, input_sha256, result) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (comparison_id) DO NOTHING",
                (command.comparison_id, command.session_id, command.input_sha256, Jsonb(encode_result(result))),
            )
            row = connection.execute(
                "SELECT session_id, input_sha256, result FROM strategy_comparisons WHERE comparison_id = %s",
                (command.comparison_id,),
            ).fetchone()
            return self._matching(row, command)

    async def get(self, comparison_id: UUID, session_id: UUID, session_token: str) -> ComparisonPreview:
        return await asyncio.to_thread(self._get, comparison_id, session_id, session_token)

    def _get(self, comparison_id: UUID, session_id: UUID, session_token: str) -> ComparisonPreview:
        with self.backend._connect() as connection:
            self._authorize(connection, session_id, session_token)
            row = connection.execute(
                "SELECT result FROM strategy_comparisons WHERE comparison_id = %s AND session_id = %s",
                (comparison_id, session_id),
            ).fetchone()
            if row is None:
                raise ResourceNotFound("comparison does not exist for this session")
            return decode_result(row["result"])

    async def list(self, session_id: UUID, session_token: str,
                   offset: int, limit: int) -> tuple[ComparisonSummary, ...]:
        return await asyncio.to_thread(self._list, session_id, session_token, offset, limit)

    def _list(self, session_id: UUID, session_token: str,
              offset: int, limit: int) -> tuple[ComparisonSummary, ...]:
        with self.backend._connect() as connection:
            self._authorize(connection, session_id, session_token)
            rows = connection.execute(
                "SELECT comparison_id, result #>> '{session,history_version}' AS history_version, "
                "result #>> '{catalog,bundle_id}' AS bundle_id, result ->> 'snapshot_at' AS snapshot_at, "
                "result ->> 'requested_k' AS requested_k, result -> 'strategies' AS strategies "
                "FROM strategy_comparisons WHERE session_id = %s "
                "ORDER BY created_at DESC, comparison_id DESC OFFSET %s LIMIT %s",
                (session_id, offset, limit),
            ).fetchall()
        return tuple(ComparisonSummary(
            row["comparison_id"], session_id, int(row["history_version"]), UUID(row["bundle_id"]),
            datetime.fromisoformat(row["snapshot_at"]), int(row["requested_k"]),
            tuple(Strategy(entry["requested_strategy"]) for entry in row["strategies"]),
            tuple(Strategy(entry["actual_strategy"]) for entry in row["strategies"]),
        ) for row in rows)
