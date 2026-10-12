"""RDS password rotation, simulated against the real compose Postgres."""

import json
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import NullPool

from forgeeval.db.credentials import RotatingCredentials, use_rotating_credentials


class FakeSecrets:
    """Stands in for Secrets Manager: returns whatever the secret currently holds."""

    def __init__(self, username: str, password: str) -> None:
        self.username, self.password = username, password
        self.calls = 0

    def get_secret_value(self, *, SecretId: str) -> Any:
        self.calls += 1
        return {"SecretString": json.dumps({"username": self.username, "password": self.password})}


@pytest.fixture
def role(migrated_engine: Engine) -> Iterator[str]:
    name = f"rot_{uuid.uuid4().hex[:8]}"
    with migrated_engine.begin() as c:
        c.execute(text(f"CREATE ROLE {name} LOGIN PASSWORD 'first-pass'"))
    yield name
    with migrated_engine.begin() as c:
        c.execute(text(f"DROP ROLE IF EXISTS {name}"))


def rotating_engine(admin: Engine, secrets: FakeSecrets) -> tuple[Engine, RotatingCredentials]:
    url = admin.url.set(username=None, password=None)
    engine = create_engine(url, poolclass=NullPool)  # every checkout opens a new connection
    creds = RotatingCredentials(secrets, "arn:aws:secretsmanager:test", ttl=3600)
    use_rotating_credentials(engine, creds)
    return engine, creds


def whoami(engine: Engine) -> str:
    with engine.connect() as c:
        return str(c.scalar(text("SELECT current_user")))


def test_new_connections_recover_after_password_rotation(
    migrated_engine: Engine, role: str
) -> None:
    secrets = FakeSecrets(role, "first-pass")
    engine, _ = rotating_engine(migrated_engine, secrets)
    assert whoami(engine) == role
    assert whoami(engine) == role
    assert secrets.calls == 1  # cached between connections

    # Rotation: the database and the secret change; the worker's cache still holds the old one.
    with migrated_engine.begin() as c:
        c.execute(text(f"ALTER ROLE {role} PASSWORD 'second-pass'"))
    secrets.password = "second-pass"

    assert whoami(engine) == role  # first attempt is rejected, secret re-read, retried
    assert secrets.calls == 2
    assert whoami(engine) == role
    assert secrets.calls == 2  # the refreshed value is cached again


def test_open_connection_survives_rotation(migrated_engine: Engine, role: str) -> None:
    secrets = FakeSecrets(role, "first-pass")
    engine, _ = rotating_engine(migrated_engine, secrets)
    with engine.connect() as conn:
        with migrated_engine.begin() as c:
            c.execute(text(f"ALTER ROLE {role} PASSWORD 'second-pass'"))
        assert conn.scalar(text("SELECT 1")) == 1


def test_wrong_secret_still_fails_after_one_refresh(migrated_engine: Engine, role: str) -> None:
    secrets = FakeSecrets(role, "not-the-password")
    engine, _ = rotating_engine(migrated_engine, secrets)
    with pytest.raises(OperationalError, match="password authentication failed"):
        whoami(engine)
    assert secrets.calls == 2  # one refresh, no retry loop
