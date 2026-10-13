"""Tests run against a throwaway `forgeeval_test` database on the compose Postgres."""

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url

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
