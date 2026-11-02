"""S3 storage (jobmarket.s3store) against moto's in-memory S3: no network, no AWS account."""

import pytest

pytest.importorskip("boto3", reason="S3 needs boto3 (pip install -e '.[aws]')")
moto = pytest.importorskip("moto", reason="the S3 tests need moto (pip install -e '.[dev]')")

import boto3  # noqa: E402

from jobmarket.config import ConfigError  # noqa: E402
from jobmarket.s3store import (  # noqa: E402
    S3Config,
    S3Store,
    S3Unavailable,
    load_s3_config,
    sync_raw,
)

BUCKET = "jobmarket-test-bucket"


@pytest.fixture
def s3(monkeypatch):
    # Fake credentials for moto only; nothing leaves the process.
    for name, value in {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
                        "AWS_DEFAULT_REGION": "us-east-2"}.items():  # fmt: skip
        monkeypatch.setenv(name, value)
    with moto.mock_aws():
        client = boto3.client("s3", region_name="us-east-2")
        client.create_bucket(
            Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "us-east-2"}
        )
        yield S3Store(S3Config(BUCKET, "us-east-2"), client)


def test_configuration_needs_a_bucket():
    with pytest.raises(ConfigError, match="JOBMARKET_S3_BUCKET"):
        load_s3_config({})
    config = load_s3_config({"JOBMARKET_S3_BUCKET": " b ", "AWS_REGION": "us-east-2"})
    assert config == S3Config("b", "us-east-2")


def test_uploads_are_encrypted(s3, tmp_path):
    path = tmp_path / "f.json"
    path.write_text("{}")
    s3.upload(path, "raw/x/f.json")
    head = s3.client.head_object(Bucket=BUCKET, Key="raw/x/f.json")
    assert head["ServerSideEncryption"] == "AES256"


def test_sync_raw_uploads_only_finished_new_files(s3, tmp_path):
    root = tmp_path / "raw"
    (root / "greenhouse" / "acme").mkdir(parents=True)
    (root / "lever" / "beta").mkdir(parents=True)
    (root / "greenhouse" / "acme" / "a-scan1.json").write_text("{}")
    (root / "lever" / "beta" / "b-scan2.json").write_text("[]")
    (root / "greenhouse" / "acme" / "c-scan3.tmp").write_text("half")  # still being written
    (root / "notes.json").write_text("{}")  # not under a source directory

    first = sync_raw(s3, root, ["greenhouse", "lever"])
    assert (first.uploaded, first.already_present) == (2, 0)
    assert sorted(s3.keys("raw/")) == ["raw/greenhouse/acme/a-scan1.json",
                                       "raw/lever/beta/b-scan2.json"]  # fmt: skip

    (root / "greenhouse" / "acme" / "d-scan4.json").write_text("{}")
    second = sync_raw(s3, root, ["greenhouse", "lever"])
    assert (second.uploaded, second.already_present) == (1, 2)


def test_listing_follows_pagination(s3, tmp_path):
    path = tmp_path / "f"
    path.write_text("x")
    for n in range(1005):  # one page holds at most 1,000 keys
        s3.client.put_object(Bucket=BUCKET, Key=f"raw/p/{n:05}.json", Body=b"x")
    assert len(s3.keys("raw/")) == 1005


def test_sdk_failures_become_clear_errors(s3, tmp_path):
    missing = S3Store(S3Config("no-such-bucket", "us-east-2"), s3.client)
    with pytest.raises(S3Unavailable, match="S3 listing of raw/ failed"):
        missing.keys("raw/")
    path = tmp_path / "f"
    path.write_text("x")
    with pytest.raises(S3Unavailable, match="S3 upload of raw/f failed"):
        missing.upload(path, "raw/f")


def test_commands_explain_a_missing_bucket(monkeypatch, capsys):
    from jobmarket.cli import main

    monkeypatch.delenv("JOBMARKET_S3_BUCKET", raising=False)
    monkeypatch.setenv("DB_NAME", "unused")
    monkeypatch.setenv("DB_USER", "unused")
    assert main(["sync-raw"]) == 2
    assert main(["backup", "list"]) == 2
    assert "JOBMARKET_S3_BUCKET is required" in capsys.readouterr().err


def test_raw_upload_confirmation_needs_the_same_size(s3, tmp_path):
    from jobmarket.s3store import raw_upload_confirmation

    root = tmp_path / "raw"
    (root / "greenhouse" / "acme").mkdir(parents=True)
    path = root / "greenhouse" / "acme" / "a-scan1.json"
    path.write_text("{}")
    sync_raw(s3, root, ["greenhouse"])
    confirmed = raw_upload_confirmation(s3)
    assert confirmed("greenhouse/acme/a-scan1.json", 2)
    assert not confirmed("greenhouse/acme/a-scan1.json", 3)
    assert not confirmed("greenhouse/acme/missing.json", 2)
