from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine


def test_healthz() -> None:
    from forgeeval.api.main import app

    assert TestClient(app).get("/healthz").json() == {"status": "ok"}


def test_readyz_reports_database_up(migrated_engine: Engine) -> None:
    from forgeeval.api.main import app

    resp = TestClient(app).get("/readyz")
    assert resp.status_code == 200
    assert resp.json()["database"] == "up"
