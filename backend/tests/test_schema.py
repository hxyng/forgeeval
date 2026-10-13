import os
import uuid
from datetime import UTC, datetime

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from forgeeval.db import models as m
from forgeeval.db.base import Base, uuid7

HERE = os.path.dirname(__file__)


def alembic_cfg() -> Config:
    cfg = Config(os.path.join(HERE, "..", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(HERE, "..", "migrations"))
    return cfg


def test_all_21_tables_exist(migrated_engine: Engine) -> None:
    tables = set(inspect(migrated_engine).get_table_names()) - {"alembic_version"}
    assert tables == set(Base.metadata.tables)
    assert len(tables) == 21


def test_models_match_migration(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == [], (
        f"models drifted from migrations; run alembic revision --autogenerate: {diff}"
    )


def test_downgrade_then_upgrade_round_trips(migrated_engine: Engine) -> None:
    command.downgrade(alembic_cfg(), "base")
    assert set(inspect(migrated_engine).get_table_names()) <= {"alembic_version"}
    command.upgrade(alembic_cfg(), "head")
    assert len(inspect(migrated_engine).get_table_names()) == 22


@pytest.fixture
def project(migrated_engine: Engine) -> m.Project:
    with Session(migrated_engine, expire_on_commit=False) as s:
        org = m.Organization(name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
        s.add(org)
        s.flush()
        proj = m.Project(org_id=org.id, name="Support bot", slug="support-bot")
        s.add(proj)
        s.commit()
        return proj


def test_duplicate_trace_idempotency_key_is_rejected(
    migrated_engine: Engine, project: m.Project
) -> None:
    def trace() -> m.Trace:
        return m.Trace(
            project_id=project.id,
            idempotency_key="sdk-trace-1",
            model="openai/gpt-5-mini",
            status=m.TraceStatus.ok,
            latency_ms=420,
            started_at=datetime.now(UTC),
        )

    with Session(migrated_engine) as s:
        s.add(trace())
        s.commit()
        s.add(trace())
        with pytest.raises(IntegrityError):
            s.commit()


def test_frozen_dataset_version_rejects_item_changes(
    migrated_engine: Engine, project: m.Project
) -> None:
    with Session(migrated_engine) as s:
        ds = m.Dataset(project_id=project.id, name="refunds")
        s.add(ds)
        s.flush()
        dv = m.DatasetVersion(dataset_id=ds.id, version=1, content_hash="abc")
        s.add(dv)
        s.flush()
        item = m.DatasetItem(dataset_version_id=dv.id, input={"q": "Can I get a refund?"})
        s.add(item)
        s.commit()

        s.execute(
            text("UPDATE dataset_versions SET frozen_at = now() WHERE id = :id"), {"id": dv.id}
        )
        s.commit()

        with pytest.raises(DBAPIError, match="is frozen"):
            s.execute(
                text("UPDATE dataset_items SET tags = '{edited}' WHERE id = :id"), {"id": item.id}
            )
        s.rollback()


def test_uuid7_is_version_7_and_time_ordered() -> None:
    ids = [uuid7() for _ in range(50)]
    assert all(i.version == 7 for i in ids)
    timestamps = [i.int >> 80 for i in ids]
    assert timestamps == sorted(timestamps)
