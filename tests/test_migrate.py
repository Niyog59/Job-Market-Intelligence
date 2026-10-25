from types import SimpleNamespace

import pytest

from jobmarket import db, migrate
from jobmarket.migrate import MigrationError, apply_migrations, discover, migration_status

CORE_TABLES = {
    "employers",
    "source_boards",
    "collection_runs",
    "board_scans",
    "raw_responses",
    "source_postings",
    "posting_versions",
    "posting_observations",
}


def _tables(conn, schema):
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
        (schema,),
    ).fetchall()
    return {name for (name,) in rows}


def _recorded(conn):
    return [
        name for (name,) in conn.execute("SELECT filename FROM schema_migrations ORDER BY version")
    ]


# --- discovery (no database needed) ---


def test_packaged_migrations_are_discovered_in_order():
    migrations = discover()
    assert migrations[0].filename == "0001_create_core_tables.sql"
    assert [m.version for m in migrations] == sorted(m.version for m in migrations)
    assert all(len(m.checksum) == 64 for m in migrations)


def test_non_sql_files_are_ignored(tmp_path):
    (tmp_path / "0001_first.sql").write_text("SELECT 1;")
    (tmp_path / "README.md").write_text("notes")
    assert [m.filename for m in discover(tmp_path)] == ["0001_first.sql"]


@pytest.mark.parametrize("name", ["1_short.sql", "0001-dash.sql", "0001_Upper.sql", "abcd_x.sql"])
def test_invalid_filename_is_rejected(tmp_path, name):
    (tmp_path / name).write_text("SELECT 1;")
    with pytest.raises(MigrationError, match="invalid migration filename"):
        discover(tmp_path)


def test_duplicate_version_is_rejected(tmp_path):
    (tmp_path / "0001_a.sql").write_text("SELECT 1;")
    (tmp_path / "0001_b.sql").write_text("SELECT 1;")
    with pytest.raises(MigrationError, match="duplicate migration version 0001"):
        discover(tmp_path)


def test_checksum_changes_when_content_changes(tmp_path):
    path = tmp_path / "0001_a.sql"
    path.write_text("SELECT 1;")
    before = discover(tmp_path)[0].checksum
    path.write_text("SELECT 2;")
    assert discover(tmp_path)[0].checksum != before


def test_requires_autocommit_connection():
    with pytest.raises(ValueError, match="autocommit"):
        apply_migrations(SimpleNamespace(autocommit=False), [])


# --- applying (database needed) ---


@pytest.mark.db
def test_migrate_creates_core_tables_and_is_idempotent(schema_conn, isolated_schema):
    first = apply_migrations(schema_conn)
    assert first == [m.filename for m in discover()]
    assert CORE_TABLES | {"schema_migrations"} <= _tables(schema_conn, isolated_schema)

    second = apply_migrations(schema_conn)
    assert second == []
    assert _recorded(schema_conn) == first


@pytest.mark.db
def test_failed_migration_leaves_no_partial_changes(schema_conn, isolated_schema, tmp_path):
    (tmp_path / "0001_ok.sql").write_text("CREATE TABLE first_table (id int);")
    bad = tmp_path / "0002_bad.sql"
    bad.write_text("CREATE TABLE second_table (id int);\nSELECT no_such_function();")

    with pytest.raises(MigrationError, match="0002_bad.sql"):
        apply_migrations(schema_conn, discover(tmp_path))

    tables = _tables(schema_conn, isolated_schema)
    assert "first_table" in tables
    assert "second_table" not in tables
    assert _recorded(schema_conn) == ["0001_ok.sql"]

    bad.write_text("CREATE TABLE second_table (id int);")
    assert apply_migrations(schema_conn, discover(tmp_path)) == ["0002_bad.sql"]
    assert "second_table" in _tables(schema_conn, isolated_schema)


@pytest.mark.db
def test_editing_an_applied_migration_is_refused(schema_conn, tmp_path):
    path = tmp_path / "0001_a.sql"
    path.write_text("CREATE TABLE a (id int);")
    apply_migrations(schema_conn, discover(tmp_path))

    path.write_text("CREATE TABLE a (id bigint);")
    (tmp_path / "0002_b.sql").write_text("CREATE TABLE b (id int);")
    with pytest.raises(MigrationError, match="modified after it was applied"):
        apply_migrations(schema_conn, discover(tmp_path))
    assert _recorded(schema_conn) == ["0001_a.sql"]


@pytest.mark.db
def test_missing_applied_file_is_refused(schema_conn, tmp_path):
    path = tmp_path / "0001_a.sql"
    path.write_text("CREATE TABLE a (id int);")
    apply_migrations(schema_conn, discover(tmp_path))

    path.unlink()
    with pytest.raises(MigrationError, match="its file is missing"):
        apply_migrations(schema_conn, discover(tmp_path))


@pytest.mark.db
def test_out_of_order_migration_is_refused(schema_conn, tmp_path):
    (tmp_path / "0001_a.sql").write_text("CREATE TABLE a (id int);")
    (tmp_path / "0003_c.sql").write_text("CREATE TABLE c (id int);")
    apply_migrations(schema_conn, discover(tmp_path))

    (tmp_path / "0002_b.sql").write_text("CREATE TABLE b (id int);")
    with pytest.raises(MigrationError, match="older than the latest applied"):
        apply_migrations(schema_conn, discover(tmp_path))


@pytest.mark.db
def test_status_reports_pending_then_applied(schema_conn, tmp_path):
    (tmp_path / "0001_a.sql").write_text("CREATE TABLE a (id int);")
    migrations = discover(tmp_path)

    before = migration_status(schema_conn, migrations)
    assert [(s.filename, s.applied_at) for s in before] == [("0001_a.sql", None)]

    apply_migrations(schema_conn, migrations)
    after = migration_status(schema_conn, migrations)
    assert after[0].applied_at is not None


@pytest.mark.db
def test_advisory_lock_is_released_after_failure(schema_conn, db_settings, tmp_path):
    (tmp_path / "0001_bad.sql").write_text("SELECT no_such_function();")
    with pytest.raises(MigrationError):
        apply_migrations(schema_conn, discover(tmp_path))

    with db.connect(db_settings) as other:
        acquired = other.execute(
            "SELECT pg_try_advisory_lock(%s)", (migrate.ADVISORY_LOCK_KEY,)
        ).fetchone()[0]
        assert acquired
