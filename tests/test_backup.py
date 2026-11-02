"""Backups (jobmarket.backup): manifests and checksums, the S3 layout (moto), the refusal to
restore over existing data, and a full pg_dump / pg_restore round trip.

The round trip needs the PostgreSQL client tools and a login that may create databases (the
CI service login can; a normal local owner login cannot, so it is skipped there). With
JOBMARKET_REQUIRE_PG_TOOLS=1 (set in CI) it fails instead of skipping.
"""

import hashlib
import json
import os
import uuid
from dataclasses import replace
from datetime import UTC, datetime

import psycopg
import pytest

from jobmarket import backup, db
from jobmarket.migrate import apply_migrations
from jobmarket.owner import create_owner

NOW = datetime(2026, 11, 1, 6, 0, tzinfo=UTC)


def fake_backup(root, stamp="20261101T060000Z", applied=13):
    directory = root / stamp
    directory.mkdir(parents=True)
    parts = {}
    for part, (schema, name) in backup.PARTS.items():
        body = f"{part} dump".encode()
        (directory / name).write_bytes(body)
        parts[part] = {"schema": schema, "file": name, "bytes": len(body),
                       "sha256": hashlib.sha256(body).hexdigest()}  # fmt: skip
    manifest = {"format_version": 1, "stamp": stamp, "migrations": {"applied": applied},
                "parts": parts}  # fmt: skip
    (directory / backup.MANIFEST).write_text(json.dumps(manifest))
    return directory


def test_verify_accepts_a_complete_backup(tmp_path):
    manifest = backup.verify_backup(fake_backup(tmp_path))
    assert set(manifest["parts"]) == {"ingestion", "private"}


def test_verify_rejects_damaged_or_incomplete_backups(tmp_path):
    damaged = fake_backup(tmp_path, "20261101T060000Z")
    (damaged / "private.dump").write_bytes(b"private dumq")  # same size, other content
    with pytest.raises(backup.BackupError, match="private part is damaged"):
        backup.verify_backup(damaged)

    missing = fake_backup(tmp_path, "20261101T070000Z")
    (missing / "ingestion.dump").unlink()
    with pytest.raises(backup.BackupError, match="is missing"):
        backup.verify_backup(missing)

    unfinished = fake_backup(tmp_path, "20261101T080000Z")
    (unfinished / backup.MANIFEST).unlink()
    with pytest.raises(backup.BackupError, match="not a complete backup"):
        backup.verify_backup(unfinished)


def test_missing_client_tools_are_reported(monkeypatch, tmp_path):
    monkeypatch.setenv(backup.PG_BIN_ENV, str(tmp_path))
    with pytest.raises(backup.BackupError, match="pg_dump not found"):
        backup.tool("pg_dump")


def test_password_goes_to_the_environment_not_the_command_line(db_settings):
    settings = replace(db_settings, db_password="s3cret-for-test")
    env = backup._pg_env(settings)
    assert env["PGPASSWORD"] == "s3cret-for-test"
    assert env["PGDATABASE"] == settings.db_name
    assert "PGPASSWORD" not in backup._pg_env(replace(db_settings, db_password=""))


@pytest.mark.db
def test_restore_refuses_a_database_that_is_not_empty(db_settings, isolated_schema, tmp_path):
    # The isolated schema guarantees the configured database is not empty.
    with pytest.raises(backup.BackupError, match="is not empty") as caught:
        backup.restore_backup(db_settings, fake_backup(tmp_path))
    assert f"schema {isolated_schema}" in str(caught.value)


class TestS3:
    @pytest.fixture
    def store(self, monkeypatch):
        pytest.importorskip("boto3")
        moto = pytest.importorskip("moto")
        import boto3

        from jobmarket.s3store import S3Config, S3Store

        for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
            monkeypatch.setenv(name, "testing")
        with moto.mock_aws():
            client = boto3.client("s3", region_name="us-east-2")
            client.create_bucket(Bucket="jobmarket-backup-test", CreateBucketConfiguration={
                "LocationConstraint": "us-east-2"})  # fmt: skip
            yield S3Store(S3Config("jobmarket-backup-test", "us-east-2"), client)

    def test_parts_go_to_separate_encrypted_prefixes(self, store, tmp_path):
        keys = backup.upload_backup(store, fake_backup(tmp_path))
        assert keys == [
            "backups/ingestion/20261101T060000Z/ingestion.dump",
            "backups/ingestion/20261101T060000Z/manifest.json",
            "backups/private/20261101T060000Z/private.dump",
            "backups/private/20261101T060000Z/manifest.json",
        ]
        for key in keys:
            head = store.client.head_object(Bucket="jobmarket-backup-test", Key=key)
            assert head["ServerSideEncryption"] == "AES256"

    def test_upload_is_confirmed_before_it_is_marked(self, store, tmp_path, monkeypatch):
        directory = fake_backup(tmp_path)
        backup.upload_backup(store, directory, NOW)
        marked = json.loads((directory / backup.UPLOADED).read_text())
        assert len(marked["objects"]) == 4 and marked["bucket"] == store.bucket

        other = fake_backup(tmp_path, "20261102T060000Z")
        monkeypatch.setattr(store, "size", lambda key: 1)  # S3 reports a different size
        with pytest.raises(backup.BackupError, match="expected"):
            backup.upload_backup(store, other, NOW)
        assert not backup.is_uploaded(other)

    def test_list_and_fetch(self, store, tmp_path):
        backup.upload_backup(store, fake_backup(tmp_path / "local"))
        # An interrupted upload (dump without manifest) is not listed as a finished part.
        unfinished = "backups/private/20261102T060000Z/private.dump"
        store.client.put_object(Bucket=store.bucket, Key=unfinished, Body=b"x")
        assert backup.list_backups(store) == {"20261101T060000Z": ["ingestion", "private"]}

        fetched = backup.fetch_backup(store, "20261101T060000Z", tmp_path / "restored")
        assert sorted(p.name for p in fetched.iterdir()) == [
            "ingestion.dump", "manifest.json", "private.dump"]  # fmt: skip
        with pytest.raises(backup.BackupError, match="not a backup name"):
            backup.fetch_backup(store, "../../etc", tmp_path / "x")


@pytest.fixture
def two_databases(db_settings):
    """A migrated source database with a little data, and an empty target; both dropped."""
    names = [f"jm_backup_{uuid.uuid4().hex[:8]}_{side}" for side in ("src", "dst")]
    with db.connect(db_settings, autocommit=True) as admin:
        try:
            for name in names:
                admin.execute(f'CREATE DATABASE "{name}"')
        except psycopg.errors.InsufficientPrivilege:
            if os.environ.get("JOBMARKET_REQUIRE_PG_TOOLS") == "1":
                raise
            pytest.skip("this login may not create databases")
    try:
        yield [replace(db_settings, db_name=name) for name in names]
    finally:
        with db.connect(db_settings, autocommit=True) as admin:
            for name in names:
                admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _counts(settings):
    with db.connect(settings) as conn:
        tables = conn.execute(
            "SELECT schemaname, tablename FROM pg_tables "
            "WHERE schemaname IN ('public', 'private') ORDER BY 1, 2"
        ).fetchall()
        return {f"{s}.{t}": conn.execute(f'SELECT count(*) FROM "{s}"."{t}"').fetchone()[0]
                for s, t in tables}  # fmt: skip


@pytest.mark.db
def test_backup_and_restore_round_trip(two_databases, tmp_path):
    try:
        backup.tool("pg_dump")
        backup.tool("pg_restore")
    except backup.BackupError:
        if os.environ.get("JOBMARKET_REQUIRE_PG_TOOLS") == "1":
            raise
        pytest.skip("PostgreSQL client tools not found")
    source, target = two_databases
    with db.connect(source, autocommit=True) as conn:
        apply_migrations(conn)
        create_owner(conn, "private", "owner_test", "correct horse battery", NOW)
        conn.execute("INSERT INTO private.visits (account_id, started_at, last_activity_at) "
                     "VALUES (1, %s, %s)", (NOW, NOW))  # fmt: skip

    directory = backup.create_backup(source, tmp_path, NOW)
    assert directory.name == "20261101T060000Z"
    assert not list(tmp_path.glob("*.partial"))
    manifest = backup.verify_backup(directory)
    assert manifest["parts"]["private"]["schema"] == "private"

    result = backup.restore_backup(target, directory)
    assert result.owner_accounts == 1
    assert result.migrations_applied == result.migrations_expected == manifest["migrations"][
        "applied"]  # fmt: skip
    assert _counts(target) == _counts(source)

    # Restoring again into the now-filled database is refused.
    with pytest.raises(backup.BackupError, match="is not empty"):
        backup.restore_backup(target, directory)


def test_prune_keeps_the_newest_and_deletes_only_confirmed_older_backups(tmp_path):
    names = [f"202611{d:02}T060000Z" for d in range(1, 7)]  # oldest first
    for name in names:
        fake_backup(tmp_path, name)
    for name in names[:2]:  # the two oldest were confirmed in S3
        (tmp_path / name / backup.UPLOADED).write_text("{}")
    damaged = tmp_path / names[2]
    (damaged / "private.dump").write_bytes(b"damaged!!!!!")
    (tmp_path / "20261107T060000Z.partial").mkdir()

    result = backup.prune_backups(tmp_path, keep=2)
    assert result.kept == [names[5], names[4]]
    assert result.deleted == [names[1], names[0]]
    assert result.kept_not_uploaded == [names[3]]  # older, but S3 never confirmed it
    assert result.damaged == [names[2]]  # reported, not deleted
    assert result.partials_removed == ["20261107T060000Z.partial"]
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(names[2:])


def test_prune_never_deletes_the_only_verified_copy(tmp_path):
    only = fake_backup(tmp_path, "20261101T060000Z")
    (only / backup.UPLOADED).write_text("{}")
    assert backup.prune_backups(tmp_path, keep=1).deleted == []
    with pytest.raises(ValueError):
        backup.prune_backups(tmp_path, keep=0)
    assert only.exists()


def test_prune_of_a_missing_directory_does_nothing(tmp_path):
    assert backup.prune_backups(tmp_path / "none", keep=3).kept == []
