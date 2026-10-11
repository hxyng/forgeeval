import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Annotated

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from forgeeval import admin
from forgeeval.auth import AuthContext, Scope, generate_key, hash_key, parse_prefix, require
from forgeeval.db.models import ApiKey, Project


@pytest.fixture
def db(migrated_engine: Engine) -> Iterator[Session]:
    with Session(migrated_engine, expire_on_commit=False) as s:
        yield s


@pytest.fixture
def project(db: Session) -> Project:
    p = admin.get_or_create_project(db, f"org-{uuid.uuid4().hex[:6]}", "bot")
    db.commit()
    return p


def make_key(db: Session, project: Project, scopes: list[str] | None = None) -> str:
    plaintext = admin.create_key(db, project, scopes or [Scope.traces_write.value])
    db.commit()
    return plaintext


@pytest.fixture
def client() -> TestClient:
    from forgeeval.api.main import app

    return TestClient(app)


def bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def test_generated_key_round_trips_prefix_and_hash() -> None:
    key = generate_key()
    assert key.plaintext.startswith(f"fe_{key.prefix}_")
    assert parse_prefix(key.plaintext) == key.prefix
    assert key.hash == hash_key(key.plaintext)
    assert key.hash != hash_key(key.plaintext, pepper="other-pepper")


@pytest.mark.parametrize("bad", ["", "fe_short_x", "sk_abcdefgh_xyz", "fe_abcdefgh"])
def test_parse_prefix_rejects_malformed(bad: str) -> None:
    assert parse_prefix(bad) is None


def test_whoami_resolves_project(client: TestClient, db: Session, project: Project) -> None:
    key = make_key(db, project, ["traces:write", "gates:read"])
    resp = client.get("/v1/whoami", headers=bearer(key))
    assert resp.status_code == 200
    assert resp.json()["project_id"] == str(project.id)
    assert resp.json()["scopes"] == ["gates:read", "traces:write"]


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Basic abc"}, bearer("fe_notakey"), bearer("fe_abcdefgh_" + "x" * 32)],
)
def test_whoami_rejects_missing_or_unknown_keys(
    client: TestClient, headers: dict[str, str]
) -> None:
    resp = client.get("/v1/whoami", headers=headers)
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"] == "Bearer"


def test_tampered_secret_with_valid_prefix_is_rejected(
    client: TestClient, db: Session, project: Project
) -> None:
    key = make_key(db, project)
    tampered = key[:-1] + ("a" if key[-1] != "a" else "b")
    assert client.get("/v1/whoami", headers=bearer(tampered)).status_code == 401


def test_revoked_key_is_rejected(client: TestClient, db: Session, project: Project) -> None:
    key = make_key(db, project)
    assert admin.revoke_key(db, parse_prefix(key) or "")
    db.commit()
    resp = client.get("/v1/whoami", headers=bearer(key))
    assert resp.status_code == 401
    assert "revoked" in resp.json()["detail"]


def test_last_used_at_is_recorded_at_most_once_per_minute(
    client: TestClient, db: Session, project: Project
) -> None:
    key = make_key(db, project)
    prefix = parse_prefix(key)
    client.get("/v1/whoami", headers=bearer(key))
    first = db.scalar(select(ApiKey.last_used_at).where(ApiKey.key_prefix == prefix))
    assert first is not None

    client.get("/v1/whoami", headers=bearer(key))
    db.expire_all()
    assert db.scalar(select(ApiKey.last_used_at).where(ApiKey.key_prefix == prefix)) == first

    stale = datetime.now(UTC) - timedelta(minutes=5)
    db.execute(update(ApiKey).where(ApiKey.key_prefix == prefix).values(last_used_at=stale))
    db.commit()
    client.get("/v1/whoami", headers=bearer(key))
    db.expire_all()
    refreshed = db.scalar(select(ApiKey.last_used_at).where(ApiKey.key_prefix == prefix))
    assert refreshed is not None and refreshed > stale


def test_require_scope_returns_403_without_scope(db: Session, project: Project) -> None:
    app = FastAPI()

    @app.get("/gated")
    def gated(ctx: Annotated[AuthContext, require(Scope.runs_write)]) -> dict[str, str]:
        return {"project": str(ctx.project_id)}

    client = TestClient(app)
    assert client.get("/gated", headers=bearer(make_key(db, project))).status_code == 403
    ok = client.get("/gated", headers=bearer(make_key(db, project, ["runs:write"])))
    assert ok.status_code == 200


def test_bootstrap_is_idempotent_for_org_and_project(db: Session) -> None:
    slug = f"org-{uuid.uuid4().hex[:6]}"
    a = admin.get_or_create_project(db, slug, "bot")
    b = admin.get_or_create_project(db, slug, "bot")
    assert a.id == b.id


def test_create_key_rejects_unknown_scope(db: Session, project: Project) -> None:
    with pytest.raises(ValueError, match="unknown scopes"):
        admin.create_key(db, project, ["traces:delete"])


def test_admin_cli_prints_key_once(capsys: pytest.CaptureFixture[str]) -> None:
    slug = f"cli-{uuid.uuid4().hex[:6]}"
    assert admin.main(["bootstrap", "--org", slug, "--project", "bot"]) == 0
    out = capsys.readouterr().out
    plaintext = next(line.split()[-1] for line in out.splitlines() if line.startswith("api_key"))
    prefix = parse_prefix(plaintext)
    assert prefix is not None
    assert admin.main(["revoke-key", prefix]) == 0
    assert admin.main(["revoke-key", "zzzzzzzz"]) == 1
