"""Private S3 storage for raw responses and database backups.

Credentials come from the AWS SDK's default chain. On the server that is the EC2 instance
role, so no access keys are stored anywhere; on a developer machine it is whatever profile
the AWS CLI uses. The bucket name and region come from JOBMARKET_S3_BUCKET and AWS_REGION.

Every upload asks for server-side encryption with S3-managed keys (SSE-S3). Keys are laid out
by purpose so IAM policies and lifecycle rules can target each one separately:

    raw/<source>/<board>/<file>.json        raw responses (same path as under data/raw)
    backups/ingestion/<stamp>/<file>        ingestion backup and its manifest
    backups/private/<stamp>/<file>          private-workspace backup and its manifest

Retention is set by bucket lifecycle rules, not by this code: nothing here deletes objects.
"""

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from jobmarket.config import ConfigError

BUCKET_ENV = "JOBMARKET_S3_BUCKET"
RAW_PREFIX = "raw/"
BACKUP_PREFIXES = {"ingestion": "backups/ingestion/", "private": "backups/private/"}
ENCRYPTION = {"ServerSideEncryption": "AES256"}


class S3Unavailable(Exception):
    """boto3 is not installed (pip install -e '.[aws]'), or S3 refused or failed a request."""


def _sdk_errors() -> tuple[type[Exception], ...]:
    try:
        from boto3.exceptions import Boto3Error
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError:
        return ()
    # Boto3Error covers the transfer manager's own errors (S3UploadFailedError).
    return (BotoCoreError, ClientError, Boto3Error)


def _calling(action: str, call, *args, **kwargs):
    """Run an SDK call; SDK failures (no credentials, access denied, network) become
    S3Unavailable with the SDK's own message, which never contains credentials."""
    try:
        return call(*args, **kwargs)
    except _sdk_errors() as exc:
        raise S3Unavailable(f"S3 {action} failed: {exc}") from exc


@dataclass(frozen=True)
class S3Config:
    bucket: str
    region: str | None = None


def load_s3_config(environ: Mapping[str, str] | None = None) -> S3Config:
    env = os.environ if environ is None else environ
    bucket = env.get(BUCKET_ENV, "").strip()
    if not bucket:
        raise ConfigError(f"{BUCKET_ENV} is required for S3 uploads")
    region = (env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION") or "").strip() or None
    return S3Config(bucket, region)


class S3Store:
    def __init__(self, config: S3Config, client=None):
        self.bucket = config.bucket
        if client is None:
            try:
                import boto3
            except ImportError as exc:
                raise S3Unavailable('S3 needs boto3: pip install -e ".[aws]"') from exc
            client = boto3.client("s3", region_name=config.region)
        self.client = client

    def upload(self, path: Path, key: str) -> None:
        _calling(f"upload of {key}", self.client.upload_file, str(path), self.bucket, key,
                 ExtraArgs=dict(ENCRYPTION))  # fmt: skip

    def download(self, key: str, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        _calling(f"download of {key}", self.client.download_file, self.bucket, key, str(path))

    def keys(self, prefix: str) -> list[str]:
        """Every key under prefix (follows pagination)."""
        return list(self.objects(prefix))

    def objects(self, prefix: str) -> dict[str, int]:
        """Every key under prefix with its size in bytes (follows pagination)."""

        def collect():
            found = {}
            pages = self.client.get_paginator("list_objects_v2").paginate(
                Bucket=self.bucket, Prefix=prefix
            )
            for page in pages:
                found.update((item["Key"], item["Size"]) for item in page.get("Contents", []))
            return found

        return _calling(f"listing of {prefix}", collect)

    def size(self, key: str) -> int | None:
        """The stored object's size, or None if it does not exist."""
        objects = self.objects(key)
        return objects.get(key)


@dataclass(frozen=True)
class SyncResult:
    uploaded: int
    already_present: int


def _raw_files(root: Path, sources: Iterable[str]) -> list[Path]:
    files = []
    for source in sources:
        base = root / source
        if base.exists():
            # Only finished files: store_body writes "*.tmp" first and renames it.
            files.extend(p for p in base.rglob("*.json") if p.is_file())
    return sorted(files)


def sync_raw(store: S3Store, root: Path, sources: Iterable[str]) -> SyncResult:
    """Upload raw response files that are not in S3 yet.

    Raw files are written once and never changed, so a file whose key already exists with
    the same size is skipped. Run it before the local cleanup, which deletes only files S3
    confirms (raw_upload_confirmation).
    """
    present = store.objects(RAW_PREFIX)
    uploaded = skipped = 0
    for path in _raw_files(root, sources):
        key = RAW_PREFIX + path.relative_to(root).as_posix()
        if present.get(key) == path.stat().st_size:
            skipped += 1
            continue
        store.upload(path, key)
        uploaded += 1
    return SyncResult(uploaded, skipped)


def raw_upload_confirmation(store: S3Store):
    """A check for cleanup_raw: is this raw file (path relative to the raw directory) in S3
    with the same size? Lists the raw prefix once."""
    present = store.objects(RAW_PREFIX)
    return lambda relative, size: present.get(RAW_PREFIX + relative) == size
