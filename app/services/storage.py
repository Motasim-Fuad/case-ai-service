"""File storage backends. Production = AWS S3 (read-only for this service); local = dev/test."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import Settings
from app.core.errors import PermanentError, StorageError, StorageNotFoundError


@dataclass(frozen=True)
class StorageRef:
    bucket: str | None
    key: str


class Storage(Protocol):
    supports_async_ocr: bool

    def size(self, ref: StorageRef) -> int: ...

    def read(self, ref: StorageRef) -> bytes: ...


class S3Storage:
    supports_async_ocr = True

    def __init__(self, settings: Settings, client=None) -> None:
        self._settings = settings
        self._client = client or boto3.client(
            "s3",
            region_name=settings.aws_region or None,
            aws_access_key_id=settings.aws_access_key_id or None,
            aws_secret_access_key=settings.aws_secret_access_key or None,
            endpoint_url=settings.aws_endpoint_url or None,
            config=Config(
                retries={"max_attempts": 5, "mode": "adaptive"},
                connect_timeout=5,
                read_timeout=60,
            ),
        )

    def _bucket(self, ref: StorageRef) -> str:
        bucket = ref.bucket or self._settings.s3_bucket_name
        if not bucket:
            raise PermanentError("No S3 bucket provided and S3_BUCKET_NAME is not set", code="BUCKET_MISSING")
        return bucket

    def size(self, ref: StorageRef) -> int:
        try:
            head = self._client.head_object(Bucket=self._bucket(ref), Key=ref.key)
            return int(head["ContentLength"])
        except ClientError as exc:
            raise self._map(exc, ref) from exc
        except BotoCoreError as exc:
            raise StorageError(f"S3 error: {exc}") from exc

    def read(self, ref: StorageRef) -> bytes:
        try:
            obj = self._client.get_object(Bucket=self._bucket(ref), Key=ref.key)
            return obj["Body"].read()
        except ClientError as exc:
            raise self._map(exc, ref) from exc
        except BotoCoreError as exc:
            raise StorageError(f"S3 error: {exc}") from exc

    @staticmethod
    def _map(exc: ClientError, ref: StorageRef) -> Exception:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in {"404", "NoSuchKey", "NotFound", "NoSuchBucket"}:
            return StorageNotFoundError(f"File not found in storage: {ref.key}")
        if code in {"403", "AccessDenied", "Forbidden"}:
            return PermanentError(f"Access denied reading {ref.key} (check IAM permissions)", code="STORAGE_ACCESS_DENIED")
        return StorageError(f"S3 error ({code}) reading {ref.key}")


class LocalStorage:
    """Reads files from a local folder. Bucket is ignored. Protected against path traversal."""

    supports_async_ocr = False

    def __init__(self, base_dir: str | Path) -> None:
        self.base = Path(base_dir).resolve()
        self.base.mkdir(parents=True, exist_ok=True)

    def _path(self, ref: StorageRef) -> Path:
        path = (self.base / ref.key.lstrip("/")).resolve()
        if self.base != path and self.base not in path.parents:
            raise PermanentError("Invalid storage key", code="INVALID_KEY")
        return path

    def size(self, ref: StorageRef) -> int:
        path = self._path(ref)
        if not path.is_file():
            raise StorageNotFoundError(f"File not found in local storage: {ref.key}")
        return path.stat().st_size

    def read(self, ref: StorageRef) -> bytes:
        path = self._path(ref)
        if not path.is_file():
            raise StorageNotFoundError(f"File not found in local storage: {ref.key}")
        return path.read_bytes()

    def save(self, key: str, data: bytes) -> StorageRef:
        ref = StorageRef(bucket=None, key=key)
        path = self._path(ref)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return ref


def build_storage(settings: Settings) -> Storage:
    if settings.storage_backend == "local":
        return LocalStorage(settings.local_storage_dir)
    return S3Storage(settings)
