"""Database tests use a fresh migrated schema and fail if PostgreSQL is absent."""

import os
from uuid import uuid4

import pytest


@pytest.fixture
def isolated_database(monkeypatch):
    database_url = os.getenv("EVOREC_DATABASE_URL")
    if not database_url:
        pytest.fail("EVOREC_DATABASE_URL is required for database integration tests")
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    from scripts.migrate_database import migrate
    schema = f"test_evorec_{uuid4().hex}"
    try:
        with psycopg.connect(database_url) as connection:
            connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    except psycopg.Error as exc:
        pytest.fail(f"PostgreSQL integration database is unavailable: {type(exc).__name__}")
    test_url = make_conninfo(**{
        **conninfo_to_dict(database_url),
        "options": f"-c search_path={schema}",
    })
    try:
        migrate(test_url)
        monkeypatch.setenv("EVOREC_DATABASE_URL", test_url)
        yield test_url
    finally:
        with psycopg.connect(database_url) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
