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
    assert migrations[1].path.name == "0002_m23_publication.sql"
    assert migrations[2].path.name == "0003_catalog_builds.sql"
    assert migrations[3].path.name == "0004_catalog_build_queue.sql"
    assert migrations[4].path.name == "0005_catalog_file_jobs.sql"
    assert migrations[5].path.name == "0006_strategy_comparisons.sql"
    assert migrations[6].path.name == "0007_comparison_history_index.sql"
    assert migrations[7].path.name == "0008_comparison_jobs.sql"
    assert migrations[8].path.name == "0009_r06_catalog_preparation.sql"


def test_invalid_or_duplicate_migration_names_are_rejected(tmp_path: Path):
    (tmp_path / "bad-name.sql").write_text("SELECT 1", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid migration filename"):
        load_migrations(tmp_path)

    (tmp_path / "bad-name.sql").unlink()
    (tmp_path / "0001_first.sql").write_text("SELECT 1", encoding="utf-8")
    (tmp_path / "0001_second.sql").write_text("SELECT 2", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate migration version"):
        load_migrations(tmp_path)
