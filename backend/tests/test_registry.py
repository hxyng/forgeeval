from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from forgeeval.db.models import DatasetItem, DatasetVersion

ClientFactory = Callable[..., TestClient]


@pytest.fixture
def client(make_client: ClientFactory) -> TestClient:
    return make_client()


PROMPT = {
    "template": "Answer briefly: {question}",
    "model": "openai/gpt-5-mini",
    "params": {"temperature": 0.2},
}


# ---------- prompts ----------


def test_prompt_versions_increment_and_dedupe(client: TestClient) -> None:
    r1 = client.post("/v1/prompts/support-answer/versions", json=PROMPT)
    assert r1.status_code == 201 and r1.json()["version"] == 1

    same = client.post("/v1/prompts/support-answer/versions", json=PROMPT)
    assert same.status_code == 200 and same.json()["id"] == r1.json()["id"]

    changed = client.post(
        "/v1/prompts/support-answer/versions", json={**PROMPT, "params": {"temperature": 0.0}}
    )
    assert changed.status_code == 201 and changed.json()["version"] == 2

    assert client.get("/v1/prompts").json() == [
        {"name": "support-answer", "latest_version": 2, "versions": 2}
    ]
    versions = client.get("/v1/prompts/support-answer/versions").json()
    assert [v["version"] for v in versions] == [2, 1]


def test_prompt_name_must_be_slug(client: TestClient) -> None:
    assert client.post("/v1/prompts/Bad Name/versions", json=PROMPT).status_code == 422


def test_trace_ingest_links_to_registered_prompt(client: TestClient) -> None:
    pv = client.post("/v1/prompts/support-answer/versions", json=PROMPT).json()
    out = client.post(
        "/v1/traces:batch",
        json={
            "traces": [
                {
                    "id": "t1",
                    "model": "m",
                    "latency_ms": 1,
                    "started_at": datetime.now(UTC).isoformat(),
                    "prompt": {"name": "support-answer", "version": 1},
                }
            ]
        },
    ).json()
    assert out["unresolved_prompts"] == []
    listed = client.get("/v1/traces").json()["items"][0]
    assert listed["prompt_version_id"] == pv["id"]


# ---------- datasets ----------


ITEMS: list[dict[str, Any]] = [
    {"input": {"q": "Where is my refund?"}, "expected_output": {"label": "billing"}},
    {
        "input": {"q": "App crashes on login"},
        "expected_output": {"label": "bug"},
        "context": {"relevant_doc_ids": ["kb-7"]},
        "tags": ["mobile"],
    },
]


def test_dataset_lifecycle(client: TestClient) -> None:
    assert client.post("/v1/datasets", json={"name": "triage"}).status_code == 201
    assert client.post("/v1/datasets", json={"name": "triage"}).status_code == 409

    v1 = client.post("/v1/datasets/triage/versions", json={"items": ITEMS})
    assert v1.status_code == 201
    assert v1.json()["version"] == 1 and v1.json()["item_count"] == 2

    again = client.post("/v1/datasets/triage/versions", json={"items": ITEMS})
    assert again.status_code == 200 and again.json()["id"] == v1.json()["id"]

    v2 = client.post("/v1/datasets/triage/versions", json={"items": ITEMS[:1]})
    assert v2.status_code == 201 and v2.json()["version"] == 2

    listed = client.get("/v1/datasets").json()
    assert listed[0]["name"] == "triage" and listed[0]["latest_version"] == 2

    page = client.get("/v1/datasets/triage/versions/1/items", params={"limit": 1}).json()
    assert len(page["items"]) == 1 and page["next_cursor"]
    rest = client.get(
        "/v1/datasets/triage/versions/1/items", params={"cursor": page["next_cursor"]}
    ).json()
    assert len(rest["items"]) == 1 and rest["next_cursor"] is None
    tags = [i["tags"] for i in page["items"] + rest["items"]]
    assert ["mobile"] in tags


def test_dataset_version_requires_items(client: TestClient) -> None:
    client.post("/v1/datasets", json={"name": "empty"})
    assert client.post("/v1/datasets/empty/versions", json={}).status_code == 422
    assert client.post("/v1/datasets/missing/versions", json={"items": ITEMS}).status_code == 404


def test_dataset_from_production_traces(client: TestClient) -> None:
    client.post(
        "/v1/traces:batch",
        json={
            "traces": [
                {
                    "id": f"p{i}",
                    "model": "m",
                    "latency_ms": 5,
                    "input": {"q": f"question {i}"},
                    "output": {"a": f"answer {i}"},
                    "started_at": datetime.now(UTC).isoformat(),
                }
                for i in range(3)
            ]
        },
    )
    trace_ids = [t["id"] for t in client.get("/v1/traces").json()["items"]]
    client.post("/v1/datasets", json={"name": "replay"})
    resp = client.post(
        "/v1/datasets/replay/versions",
        json={
            "from_traces": {
                "trace_ids": trace_ids,
                "use_output_as_expected": True,
                "tags": ["prod"],
            }
        },
    )
    assert resp.status_code == 201 and resp.json()["item_count"] == 3
    items = client.get("/v1/datasets/replay/versions/1/items").json()["items"]
    assert {i["source_trace_id"] for i in items} == set(trace_ids)
    assert all(i["expected_output"]["a"].startswith("answer") for i in items)
    assert all(i["tags"] == ["prod"] for i in items)


def test_dataset_from_unknown_or_foreign_traces_is_rejected(
    client: TestClient, make_client: ClientFactory, db: Session
) -> None:
    import uuid

    client.post("/v1/datasets", json={"name": "replay"})
    resp = client.post(
        "/v1/datasets/replay/versions",
        json={"from_traces": {"trace_ids": [str(uuid.uuid4())]}},
    )
    assert resp.status_code == 422 and "Unknown trace ids" in resp.json()["detail"]


def test_frozen_version_items_cannot_change(client: TestClient, db: Session) -> None:
    client.post("/v1/datasets", json={"name": "frozen"})
    dv_id = client.post("/v1/datasets/frozen/versions", json={"items": ITEMS}).json()["id"]
    db.execute(text("UPDATE dataset_versions SET frozen_at = now() WHERE id = :id"), {"id": dv_id})
    db.commit()
    item = db.scalar(select(DatasetItem).where(DatasetItem.dataset_version_id == dv_id))
    assert item is not None
    with pytest.raises(DBAPIError, match="is frozen"):
        db.execute(text("DELETE FROM dataset_items WHERE id = :id"), {"id": item.id})
    db.rollback()
    assert db.get(DatasetVersion, dv_id) is not None


# ---------- evaluators ----------


@pytest.mark.parametrize(
    ("kind", "config", "judge"),
    [
        ("exact_match", {"field": "label", "case_sensitive": False}, None),
        ("json_schema", {"schema": {"type": "object", "required": ["label"]}}, None),
        ("latency", {"max_ms": 1500}, None),
        ("cost", {"max_usd": "0.01"}, None),
        ("retrieval_relevance", {"k": 3}, None),
        (
            "llm_rubric",
            {"rubric": "Is the answer polite and correct?", "pass_threshold": 4},
            "openai/gpt-5",
        ),
    ],
)
def test_each_evaluator_kind_accepts_a_valid_config(
    client: TestClient, kind: str, config: dict[str, Any], judge: str | None
) -> None:
    name = kind.replace("_", "-")
    assert client.post("/v1/evaluators", json={"name": name, "kind": kind}).status_code == 201
    resp = client.post(
        f"/v1/evaluators/{name}/versions", json={"config": config, "judge_model": judge}
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["kind"] == kind and resp.json()["version"] == 1


@pytest.mark.parametrize(
    ("kind", "config", "judge", "message"),
    [
        ("latency", {"max_ms": 0}, None, "max_ms"),
        ("latency", {"max_ms": 10, "typo": 1}, None, "typo"),
        ("json_schema", {"schema": {"type": "not-a-type"}}, None, "valid JSON Schema"),
        ("llm_rubric", {"rubric": "Rate helpfulness carefully"}, None, "judge_model"),
        (
            "llm_rubric",
            {"rubric": "Rate helpfulness carefully", "pass_threshold": 9},
            "openai/gpt-5",
            "within the scale",
        ),
        ("exact_match", {}, "openai/gpt-5", "only to llm_rubric"),
    ],
)
def test_invalid_evaluator_configs_are_rejected(
    client: TestClient, kind: str, config: dict[str, Any], judge: str | None, message: str
) -> None:
    client.post("/v1/evaluators", json={"name": "ev", "kind": kind})
    resp = client.post("/v1/evaluators/ev/versions", json={"config": config, "judge_model": judge})
    assert resp.status_code == 422
    assert message in resp.json()["detail"]


def test_evaluator_versions_dedupe_and_defaults(client: TestClient) -> None:
    client.post("/v1/evaluators", json={"name": "speed", "kind": "latency"})
    a = client.post("/v1/evaluators/speed/versions", json={"config": {"max_ms": 1000}})
    b = client.post("/v1/evaluators/speed/versions", json={"config": {"max_ms": 1000}})
    c = client.post("/v1/evaluators/speed/versions", json={"config": {"max_ms": 800}})
    assert (a.status_code, b.status_code, c.status_code) == (201, 200, 201)
    assert c.json()["version"] == 2

    listed = {e["name"]: e for e in client.get("/v1/evaluators").json()}
    assert listed["speed"]["higher_is_better"] is False
    assert listed["speed"]["latest_version"] == 2


def test_registry_writes_need_scope(make_client: ClientFactory) -> None:
    reader = make_client(["project:read"])
    assert reader.post("/v1/datasets", json={"name": "x"}).status_code == 403
    assert reader.get("/v1/datasets").status_code == 200
