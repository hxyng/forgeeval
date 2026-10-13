import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from forgeeval import admin
from forgeeval.db.models import Project, PromptVersion, Span, Trace

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def db(migrated_engine: Engine) -> Iterator[Session]:
    with Session(migrated_engine, expire_on_commit=False) as s:
        yield s


@pytest.fixture
def project(db: Session) -> Project:
    p = admin.get_or_create_project(db, f"org-{uuid.uuid4().hex[:6]}", "bot")
    db.commit()
    return p


@pytest.fixture
def client(db: Session, project: Project) -> TestClient:
    from forgeeval.api.main import app

    key = admin.create_key(db, project, ["traces:write", "project:read"])
    db.commit()
    return TestClient(app, headers={"Authorization": f"Bearer {key}"})


def trace(tid: str, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": tid,
        "model": "openai/gpt-5-mini",
        "input": {"messages": [{"role": "user", "content": "Where is my refund?"}]},
        "output": {"content": "Refunds post in 5-7 business days."},
        "latency_ms": 840,
        "input_tokens": 120,
        "output_tokens": 18,
        "cost_usd": "0.000210",
        "started_at": T0.isoformat(),
    }
    body.update(overrides)
    return body


def post(client: TestClient, *traces: dict[str, Any]) -> dict[str, Any]:
    resp = client.post("/v1/traces:batch", json={"traces": list(traces)})
    assert resp.status_code == 202, resp.text
    result: dict[str, Any] = resp.json()
    return result


def count(db: Session, project: Project) -> int:
    return db.scalar(select(func.count()).where(Trace.project_id == project.id)) or 0


def test_batch_is_stored(client: TestClient, db: Session, project: Project) -> None:
    out = post(client, trace("t1"), trace("t2"))
    assert out == {"accepted": 2, "duplicates": 0, "rejected": [], "unresolved_prompts": []}
    assert count(db, project) == 2


def test_retrying_a_batch_is_idempotent(client: TestClient, db: Session, project: Project) -> None:
    post(client, trace("t1"), trace("t2"))
    out = post(client, trace("t1"), trace("t2"), trace("t3"))
    assert out["accepted"] == 1 and out["duplicates"] == 2
    assert count(db, project) == 3


def test_duplicate_ids_within_one_batch_store_once(
    client: TestClient, db: Session, project: Project
) -> None:
    out = post(client, trace("t1"), trace("t1"))
    assert out["accepted"] == 1 and out["duplicates"] == 1
    assert count(db, project) == 1


def test_invalid_items_are_rejected_individually(
    client: TestClient, db: Session, project: Project
) -> None:
    out = post(
        client,
        trace("good"),
        trace("neg", latency_ms=-1),
        trace("naive", started_at="2026-10-01T12:00:00"),
        {"model": "x"},
    )
    assert out["accepted"] == 1
    errors = {r["index"]: r for r in out["rejected"]}
    assert set(errors) == {1, 2, 3}
    assert errors[1]["id"] == "neg" and "latency_ms" in errors[1]["error"]
    assert "timezone" in errors[2]["error"]
    assert errors[3]["id"] is None
    assert count(db, project) == 1


def test_oversized_payload_is_rejected(client: TestClient) -> None:
    out = post(client, trace("big", input={"blob": "x" * (300 * 1024)}))
    assert out["accepted"] == 0
    assert "limit is" in out["rejected"][0]["error"]


def test_spans_are_stored_with_parent_links(client: TestClient, db: Session) -> None:
    spans = [
        {
            "id": "root",
            "kind": "llm",
            "name": "answer",
            "started_at": T0.isoformat(),
            "duration_ms": 800,
        },
        {
            "id": "ret",
            "parent_id": "root",
            "kind": "retrieval",
            "name": "kb.search",
            "started_at": T0.isoformat(),
            "duration_ms": 90,
            "attributes": {"doc_ids": ["kb-12", "kb-40"]},
        },
    ]
    post(client, trace("with-spans", spans=spans))
    tid = db.scalar(select(Trace.id).where(Trace.idempotency_key == "with-spans"))
    rows = {s.name: s for s in db.scalars(select(Span).where(Span.trace_id == tid))}
    assert rows["kb.search"].parent_span_id == rows["answer"].id
    assert rows["kb.search"].attributes["doc_ids"] == ["kb-12", "kb-40"]
    assert rows["kb.search"].attributes["client_span_id"] == "ret"


def test_span_with_unknown_parent_rejects_the_trace(client: TestClient) -> None:
    spans = [
        {
            "id": "a",
            "parent_id": "missing",
            "kind": "tool",
            "name": "x",
            "started_at": T0.isoformat(),
            "duration_ms": 1,
        }
    ]
    out = post(client, trace("orphan", spans=spans))
    assert out["accepted"] == 0 and "unknown or later parent" in out["rejected"][0]["error"]


def test_retry_does_not_duplicate_spans(client: TestClient, db: Session) -> None:
    spans = [
        {"id": "a", "kind": "llm", "name": "x", "started_at": T0.isoformat(), "duration_ms": 1}
    ]
    post(client, trace("s1", spans=spans))
    post(client, trace("s1", spans=spans))
    tid = db.scalar(select(Trace.id).where(Trace.idempotency_key == "s1"))
    assert db.scalar(select(func.count()).where(Span.trace_id == tid)) == 1


def test_prompt_refs_resolve_or_are_reported(
    client: TestClient, db: Session, project: Project
) -> None:
    pv = PromptVersion(
        project_id=project.id,
        name="support-answer",
        version=3,
        template="Answer: {q}",
        model="openai/gpt-5-mini",
        content_hash="h",
    )
    db.add(pv)
    db.commit()
    out = post(
        client,
        trace("known", prompt={"name": "support-answer", "version": 3}),
        trace("unknown", prompt={"name": "support-answer", "version": 9}),
    )
    assert out["accepted"] == 2
    assert out["unresolved_prompts"] == [{"name": "support-answer", "version": 9}]
    linked = db.scalar(select(Trace.prompt_version_id).where(Trace.idempotency_key == "known"))
    assert linked == pv.id


def test_batch_limits_and_scopes(client: TestClient, db: Session, project: Project) -> None:
    assert client.post("/v1/traces:batch", json={"traces": []}).status_code == 422
    too_many = [trace(f"t{i}") for i in range(501)]
    assert client.post("/v1/traces:batch", json={"traces": too_many}).status_code == 422

    from forgeeval.api.main import app

    read_only = admin.create_key(db, project, ["project:read"])
    db.commit()
    resp = TestClient(app).post(
        "/v1/traces:batch",
        json={"traces": [trace("x")]},
        headers={"Authorization": f"Bearer {read_only}"},
    )
    assert resp.status_code == 403


def test_list_paginates_newest_first_and_filters(client: TestClient) -> None:
    post(
        client,
        *[trace(f"p{i}", started_at=(T0 + timedelta(minutes=i)).isoformat()) for i in range(5)],
        trace("err", status="error", started_at=(T0 - timedelta(hours=1)).isoformat()),
    )
    page1 = client.get("/v1/traces", params={"limit": 2}).json()
    assert [t["client_id"] for t in page1["items"]] == ["p4", "p3"]
    page2 = client.get("/v1/traces", params={"limit": 2, "cursor": page1["next_cursor"]}).json()
    assert [t["client_id"] for t in page2["items"]] == ["p2", "p1"]

    errors = client.get("/v1/traces", params={"status": "error"}).json()
    assert [t["client_id"] for t in errors["items"]] == ["err"]
    assert errors["next_cursor"] is None
    assert client.get("/v1/traces", params={"cursor": "garbage"}).status_code == 400


def test_get_trace_is_scoped_to_project(client: TestClient, db: Session) -> None:
    post(client, trace("mine"))
    tid = db.scalar(select(Trace.id).where(Trace.idempotency_key == "mine"))
    detail = client.get(f"/v1/traces/{tid}").json()
    assert detail["output"] == {"content": "Refunds post in 5-7 business days."}
    assert detail["cost_usd"] == "0.000210"

    from forgeeval.api.main import app

    other = admin.get_or_create_project(db, f"other-{uuid.uuid4().hex[:6]}", "bot")
    other_key = admin.create_key(db, other, ["project:read"])
    db.commit()
    resp = TestClient(app).get(
        f"/v1/traces/{tid}", headers={"Authorization": f"Bearer {other_key}"}
    )
    assert resp.status_code == 404
