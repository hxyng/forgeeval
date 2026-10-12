"""Object storage for large trace payloads.

Traces whose input+output exceed ``payload_offload_bytes`` are written to S3 and the row keeps
only ``payload_s3_key``, so a few huge prompts don't bloat Postgres rows, indexes and backups.
Keys are derived from (project, client trace id), so a retried upload overwrites the same object
instead of leaving an orphan.
"""

import hashlib
import json
import uuid
from typing import TYPE_CHECKING, Any, Protocol

from forgeeval.config import get_settings

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client


class PayloadStore(Protocol):
    def put(self, key: str, payload: dict[str, Any]) -> None: ...

    def get(self, key: str) -> dict[str, Any]: ...


def payload_key(project_id: uuid.UUID, client_trace_id: str) -> str:
    digest = hashlib.sha256(client_trace_id.encode()).hexdigest()
    return f"traces/{project_id}/{digest}.json"


class S3PayloadStore:
    def __init__(self, client: "S3Client", bucket: str) -> None:
        self._s3 = client
        self.bucket = bucket

    def put(self, key: str, payload: dict[str, Any]) -> None:
        self._s3.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=json.dumps(payload, default=str).encode(),
            ContentType="application/json",
        )

    def get(self, key: str) -> dict[str, Any]:
        body = self._s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        data: dict[str, Any] = json.loads(body)
        return data


_store: PayloadStore | None = None
_resolved = False


def get_store() -> PayloadStore | None:
    """The configured store, or None when S3_BUCKET is unset (payloads then stay inline and the
    smaller inline limit applies). FastAPI dependency; tests override it."""
    global _store, _resolved
    if not _resolved:
        s = get_settings()
        if s.s3_bucket:
            from forgeeval.aws import client

            _store = S3PayloadStore(client("s3"), s.s3_bucket)
        _resolved = True
    return _store
