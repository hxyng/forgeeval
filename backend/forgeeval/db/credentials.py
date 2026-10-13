"""Database credentials that survive RDS password rotation.

RDS-managed master passwords rotate (every 7 days by default), but ECS injects secrets only when a
container starts, so a long-running task would keep a stale password and its *new* connections
would start failing. With ``DB_SECRET_ARN`` set, every new pooled connection takes credentials
from Secrets Manager instead: cached for a few minutes, and re-fetched immediately when Postgres
rejects the password. Connections already open keep working, since Postgres doesn't drop
established sessions when a password changes.
"""

import json
import logging
import threading
import time
from typing import Any, Protocol

import psycopg
from sqlalchemy import event
from sqlalchemy.engine import Dialect, Engine
from sqlalchemy.pool import ConnectionPoolEntry

log = logging.getLogger("forgeeval.db.credentials")

CACHE_SECONDS = 300.0


class SecretsClient(Protocol):
    def get_secret_value(self, *, SecretId: str) -> Any: ...


class RotatingCredentials:
    def __init__(self, client: SecretsClient, secret_arn: str, ttl: float = CACHE_SECONDS) -> None:
        self._client = client
        self._arn = secret_arn
        self._ttl = ttl
        self._lock = threading.Lock()
        self._cached: tuple[str, str] | None = None
        self._fetched_at = 0.0

    def get(self, *, refresh: bool = False) -> tuple[str, str]:
        with self._lock:
            stale = time.monotonic() - self._fetched_at > self._ttl
            if refresh or stale or self._cached is None:
                secret = json.loads(
                    self._client.get_secret_value(SecretId=self._arn)["SecretString"]
                )
                self._cached = (secret["username"], secret["password"])
                self._fetched_at = time.monotonic()
            return self._cached


def _is_auth_failure(exc: BaseException) -> bool:
    return isinstance(exc, psycopg.OperationalError) and "password authentication failed" in str(
        exc
    )


def use_rotating_credentials(engine: Engine, credentials: RotatingCredentials) -> None:
    """Make ``engine`` take user/password from ``credentials`` for every new connection."""

    @event.listens_for(engine, "do_connect")
    def _connect(
        dialect: Dialect, _record: ConnectionPoolEntry, cargs: Any, cparams: dict[str, Any]
    ) -> Any:
        cparams["user"], cparams["password"] = credentials.get()
        try:
            return dialect.connect(*cargs, **cparams)
        except Exception as exc:
            if not _is_auth_failure(exc):
                raise
            log.warning("database rejected cached credentials; re-reading the secret")
            cparams["user"], cparams["password"] = credentials.get(refresh=True)
            return dialect.connect(*cargs, **cparams)
