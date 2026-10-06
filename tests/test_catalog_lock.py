"""Publication locks must serialize one physical catalog, not every schema."""

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
import pytest

from evorec.infrastructure.management import CatalogManager
from evorec.infrastructure.postgres import PostgresDemoBackend


def _lock_statement(url, monkeypatch, operation="recover"):
    """Capture the production SQL before execution; no wall-clock timeout oracle."""
    class Captured(Exception):
        pass

    statements = []
    class Probe:
        def execute(self, statement, parameters):
            statements.append((statement, parameters))
            raise Captured

    @contextmanager
    def connect(**kwargs):
        yield Probe()

    manager = CatalogManager(PostgresDemoBackend(url, r06_enabled=False), None)
    monkeypatch.setattr(manager, "_connect", connect)
    with pytest.raises(Captured):
        if operation == "recover":
            manager.recover()
        elif operation == "import":
            manager.import_items(SimpleNamespace(model_dump=lambda **kwargs: {"items": []}))
        elif operation == "enqueue":
            manager.file_jobs.enqueue(uuid4(), b"item_id,title\na,A\n", "text/csv")
        elif operation == "prepare":
            from evorec.infrastructure import r06_catalog
            monkeypatch.setattr(manager.r06, "_load", lambda *args: object())
            monkeypatch.setattr(r06_catalog, "source_records", lambda *args: ())
            manager.managed_root = Path("unused-controlled-source")
            manager.prepare_r06_bundle(uuid4(), "a" * 64)
        else:
            pytest.fail("unsupported lock probe")
    assert len(statements) == 1
    statement, parameters = statements[0]
    assert "pg_advisory" in statement and "lock(" in statement
    return statement, parameters


@pytest.mark.parametrize("same_catalog", [False, True])
def test_actual_recovery_lock_is_scoped_to_physical_catalog(isolated_database, monkeypatch, same_catalog):
    statement, parameters = _lock_statement(isolated_database, monkeypatch)
    other_schema = "test_evorec_" + uuid4().hex
    created = False
    try:
        with psycopg.connect(isolated_database) as connection:
            schema = connection.execute("SELECT current_schema()").fetchone()[0]
            connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(other_schema)))
            # Only the resource identity is needed; no model or business rows.
            connection.execute(sql.SQL("CREATE TABLE {}.catalog_control (singleton integer)").format(
                sql.Identifier(other_schema)))
        created = True
        target = schema if same_catalog else other_schema
        other_url = make_conninfo(**{
            **conninfo_to_dict(isolated_database),
            # A different first namespace still resolves the same physical
            # relation in the same-catalog case; current_schema() alone fails.
            "options": f"-c search_path={other_schema},{schema}" if same_catalog else f"-c search_path={target}",
        })
        with psycopg.connect(isolated_database, autocommit=True) as held:
            held.execute(statement, parameters)
            if same_catalog:
                # Remove the shadow table so both search paths resolve the
                # original physical control relation, without changing it.
                held.execute(sql.SQL("DROP TABLE {}.catalog_control").format(sql.Identifier(other_schema)))
            with psycopg.connect(other_url, autocommit=True) as contender:
                acquired = contender.execute(statement.replace("pg_advisory_lock(", "pg_try_advisory_lock("),
                                              parameters).fetchone()[0]
                assert acquired is (not same_catalog)
                if acquired:
                    assert contender.execute(statement.replace("pg_advisory_lock(", "pg_advisory_unlock("),
                                             parameters).fetchone()[0]
            assert held.execute(statement.replace("pg_advisory_lock(", "pg_advisory_unlock("),
                                parameters).fetchone()[0]
    finally:
        if created:
            with psycopg.connect(isolated_database) as connection:
                connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(other_schema)))


@pytest.mark.parametrize("operation", ["import", "enqueue", "prepare"])
def test_ingestion_transaction_locks_interlock_with_recovery(isolated_database, monkeypatch, operation):
    session_statement, session_parameters = _lock_statement(isolated_database, monkeypatch)
    transaction_statement, transaction_parameters = _lock_statement(isolated_database, monkeypatch, operation)
    assert "pg_advisory_xact_lock(" in transaction_statement
    probe = transaction_statement.replace("pg_advisory_xact_lock(", "pg_try_advisory_xact_lock(")
    with psycopg.connect(isolated_database, autocommit=True) as held:
        held.execute(session_statement, session_parameters)
        try:
            with psycopg.connect(isolated_database) as contender:
                assert contender.execute(probe, transaction_parameters).fetchone()[0] is False
        finally:
            assert held.execute(session_statement.replace("pg_advisory_lock(", "pg_advisory_unlock("),
                                session_parameters).fetchone()[0]
    # Connection close releases the session lease, and transaction exit
    # releases the ingestion lock; neither leaves a silent held lease.
    with psycopg.connect(isolated_database) as contender:
        assert contender.execute(probe, transaction_parameters).fetchone()[0] is True
