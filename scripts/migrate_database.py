"""Apply immutable PostgreSQL migrations with checksums and an advisory lock."""

import argparse
import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

import psycopg


ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "db" / "migrations"
NAME = re.compile(r"^(?P<version>[0-9]{4})_[a-z0-9_]+\.sql$")
LOCK_NAME = "evorec:schema-migrations"


@dataclass(frozen=True)
class Migration:
    version: str
    path: Path
    sql: str
    sha256: str


def load_migrations(directory: Path = MIGRATIONS) -> tuple[Migration, ...]:
    migrations: list[Migration] = []
    versions: set[str] = set()
    for path in sorted(directory.glob("*.sql")):
        match = NAME.fullmatch(path.name)
        if match is None:
            raise ValueError(f"invalid migration filename: {path.name}")
        version = match.group("version")
        if version in versions:
            raise ValueError(f"duplicate migration version: {version}")
        versions.add(version)
        raw = path.read_bytes()
        migrations.append(Migration(
            version=version,
            path=path,
            sql=raw.decode("utf-8"),
            sha256=hashlib.sha256(raw).hexdigest(),
        ))
    if not migrations:
        raise ValueError(f"no migrations found in {directory}")
    return tuple(migrations)


def migrate(database_url: str, *, check_only: bool = False) -> tuple[str, ...]:
    migrations = load_migrations()
    applied_now: list[str] = []
    with psycopg.connect(database_url, autocommit=True) as connection:
        connection.execute("SELECT pg_advisory_lock(hashtext(%s))", (LOCK_NAME,))
        try:
            with connection.transaction():
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS schema_migrations (
                        version varchar(4) PRIMARY KEY,
                        filename text NOT NULL UNIQUE,
                        sha256 char(64) NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
                        applied_at timestamptz NOT NULL DEFAULT now()
                    )
                """)
            rows = connection.execute(
                "SELECT version, filename, sha256 FROM schema_migrations ORDER BY version"
            ).fetchall()
            applied = {row[0]: (row[1], row[2].strip()) for row in rows}

            for migration in migrations:
                previous = applied.get(migration.version)
                if previous is not None:
                    if previous != (migration.path.name, migration.sha256):
                        raise RuntimeError(
                            f"applied migration {migration.version} no longer matches its checksum"
                        )
                    continue
                if check_only:
                    applied_now.append(migration.path.name)
                    continue
                with connection.transaction():
                    connection.execute(migration.sql)
                    connection.execute(
                        "INSERT INTO schema_migrations (version, filename, sha256) VALUES (%s, %s, %s)",
                        (migration.version, migration.path.name, migration.sha256),
                    )
                applied_now.append(migration.path.name)
        finally:
            connection.execute("SELECT pg_advisory_unlock(hashtext(%s))", (LOCK_NAME,))
    return tuple(applied_now)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="report pending migrations without applying")
    args = parser.parse_args()
    database_url = os.getenv("EVOREC_DATABASE_URL")
    if not database_url:
        raise SystemExit("EVOREC_DATABASE_URL is required")
    changed = migrate(database_url, check_only=args.check)
    if args.check:
        if changed:
            raise SystemExit("pending migrations: " + ", ".join(changed))
        print("database schema is current")
    elif changed:
        print("applied migrations: " + ", ".join(changed))
    else:
        print("database schema already current")


if __name__ == "__main__":
    main()
