"""API key format, hashing and the FastAPI dependency that authenticates requests.

Key format: ``fe_<prefix>_<secret>``. The 8-char prefix is stored in clear and shown in the
console so a user can tell keys apart; only an HMAC-SHA256 of the full key (peppered with a
server-side secret) is stored, so a database leak does not leak usable keys.
"""

import enum
import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from forgeeval.config import get_settings
from forgeeval.db.models import ApiKey
from forgeeval.db.session import get_session

KEY_PREFIX_LEN = 8
_ALPHABET = "abcdefghijkmnopqrstuvwxyz23456789"  # no l/1/0 lookalikes
LAST_USED_RESOLUTION = timedelta(minutes=1)


class Scope(enum.StrEnum):
    traces_write = "traces:write"
    runs_write = "runs:write"
    gates_read = "gates:read"
    project_read = "project:read"


ALL_SCOPES = [s.value for s in Scope]


@dataclass(frozen=True)
class NewKey:
    plaintext: str
    prefix: str
    hash: bytes


@dataclass(frozen=True)
class AuthContext:
    project_id: uuid.UUID
    key_id: uuid.UUID
    scopes: frozenset[str]


def _random(n: int) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(n))


def hash_key(plaintext: str, pepper: str | None = None) -> bytes:
    pepper = pepper if pepper is not None else get_settings().api_key_pepper
    return hmac.new(pepper.encode(), plaintext.encode(), hashlib.sha256).digest()


def generate_key() -> NewKey:
    prefix = _random(KEY_PREFIX_LEN)
    plaintext = f"fe_{prefix}_{_random(32)}"
    return NewKey(plaintext=plaintext, prefix=prefix, hash=hash_key(plaintext))


def parse_prefix(plaintext: str) -> str | None:
    parts = plaintext.split("_")
    if len(parts) != 3 or parts[0] != "fe" or len(parts[1]) != KEY_PREFIX_LEN:
        return None
    return parts[1]


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED, detail=detail, headers={"WWW-Authenticate": "Bearer"}
    )


def authenticate(
    db: Annotated[Session, Depends(get_session)],
    authorization: Annotated[str | None, Header()] = None,
) -> AuthContext:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise _unauthorized("Missing API key. Send 'Authorization: Bearer fe_...'.")
    plaintext = authorization[7:].strip()
    prefix = parse_prefix(plaintext)
    if prefix is None:
        raise _unauthorized("Malformed API key.")

    key = db.scalar(select(ApiKey).where(ApiKey.key_prefix == prefix))
    if key is None or not hmac.compare_digest(key.key_hash, hash_key(plaintext)):
        raise _unauthorized("Invalid API key.")
    if key.revoked_at is not None:
        raise _unauthorized("This API key has been revoked.")

    now = datetime.now(UTC)
    if key.last_used_at is None or now - key.last_used_at > LAST_USED_RESOLUTION:
        db.execute(update(ApiKey).where(ApiKey.id == key.id).values(last_used_at=now))
        db.commit()

    return AuthContext(project_id=key.project_id, key_id=key.id, scopes=frozenset(key.scopes))


def require(scope: Scope) -> object:
    """Dependency factory: authenticate, then insist the key carries `scope`."""

    def dependency(ctx: Annotated[AuthContext, Depends(authenticate)]) -> AuthContext:
        if scope.value not in ctx.scopes:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, detail=f"API key lacks the '{scope.value}' scope."
            )
        return ctx

    return Depends(dependency)
