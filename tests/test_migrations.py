from pathlib import Path

import pytest

from scripts.migrate_database import load_migrations


def test_repository_migrations_have_unique_ordered_versions_and_checksums():
    migrations = load_migrations()
    assert [migration.version for migration in migrations] == sorted(
        migration.version for migration in migrations
    )
    assert len({migration.version for migration in migrations}) == len(migrations)
    assert all(len(migration.sha256) == 64 for migration in migrations)
    assert migrations[0].path.name == "0001_m1_core.sql"


def test_invalid_or_duplicate_migration_names_are_rejected(tmp_path: Path):
    (tmp_path / "bad-name.sql").write_text("SELECT 1", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid migration filename"):
        load_migrations(tmp_path)

    (tmp_path / "bad-name.sql").unlink()
    (tmp_path / "0001_first.sql").write_text("SELECT 1", encoding="utf-8")
    (tmp_path / "0001_second.sql").write_text("SELECT 2", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate migration version"):
        load_migrations(tmp_path)
