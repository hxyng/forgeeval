"""Tests run against a throwaway `forgeeval_test` database on the compose Postgres."""

import os
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url

if TYPE_CHECKING:
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import Session

    from forgeeval.db.models import Project
    from forgeeval.queue import RedisStreamQueue

ADMIN_URL = os.environ.get(
    "TEST_ADMIN_DATABASE_URL", "postgresql+psycopg://forgeeval:forgeeval@localhost:5433/forgeeval"
)
TEST_URL = make_url(ADMIN_URL).set(database="forgeeval_test").render_as_string(hide_password=False)

# Point app settings at the test database before anything imports forgeeval.db.session.
os.environ["DATABASE_URL"] = TEST_URL


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text("DROP DATABASE IF EXISTS forgeeval_test WITH (FORCE)"))
        conn.execute(text("CREATE DATABASE forgeeval_test"))
    yield TEST_URL
    with admin.connect() as conn:
        conn.execute(text("DROP DATABASE IF EXISTS forgeeval_test WITH (FORCE)"))
    admin.dispose()


@pytest.fixture(scope="session")
def migrated_engine(database_url: str) -> Iterator[Engine]:
    from alembic import command
    from alembic.config import Config

    cfg = Config(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option(
        "script_location", os.path.join(os.path.dirname(__file__), "..", "migrations")
    )
    command.upgrade(cfg, "head")
    engine = create_engine(database_url)
    yield engine
    engine.dispose()


@pytest.fixture
def db(migrated_engine: Engine) -> Iterator["Session"]:
    from sqlalchemy.orm import Session

    with Session(migrated_engine, expire_on_commit=False) as s:
        yield s


@pytest.fixture
def project(db: "Session") -> "Project":
    import uuid

    from forgeeval import admin

    p = admin.get_or_create_project(db, f"org-{uuid.uuid4().hex[:6]}", "bot")
    db.commit()
    return p


@pytest.fixture
def make_client(db: "Session", project: "Project") -> "Callable[..., TestClient]":
    """Build a TestClient authenticated with a fresh key for `project` (all scopes by default)."""
    from fastapi.testclient import TestClient

    from forgeeval import admin
    from forgeeval.api.main import app
    from forgeeval.auth import ALL_SCOPES

    def build(scopes: list[str] | None = None) -> TestClient:
        key = admin.create_key(db, project, scopes or ALL_SCOPES)
        db.commit()
        return TestClient(app, headers={"Authorization": f"Bearer {key}"})

    return build


@pytest.fixture
def redis_queue() -> Iterator["RedisStreamQueue"]:
    """A real Redis Streams queue on a throwaway stream (compose Redis on localhost:6379)."""
    import uuid

    import redis

    from forgeeval.queue import RedisStreamQueue

    client = redis.Redis.from_url(os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15"))
    q = RedisStreamQueue(client, stream=f"test:{uuid.uuid4().hex}", visibility_timeout=0.2)
    yield q
    client.delete(q.stream)
    client.close()
