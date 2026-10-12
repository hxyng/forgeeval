from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient
from scenarios import N, NullQueue, completed_run, setup_tickets
from sqlalchemy.orm import Session

from forgeeval.api.main import app
from forgeeval.queue import get_queue

ClientFactory = Callable[..., TestClient]


@pytest.fixture
def client(make_client: ClientFactory) -> Iterator[TestClient]:
    app.dependency_overrides[get_queue] = NullQueue
    c = make_client()
    setup_tickets(c)
    yield c
    app.dependency_overrides.pop(get_queue, None)


def test_items_include_case_output_and_scores(client: TestClient, db: Session) -> None:
    run = completed_run(client, db, {"wrong_first": 3})
    page = client.get(f"/v1/runs/{run}/items", params={"limit": 200}).json()
    assert len(page["items"]) == N and page["next_cursor"] is None
    item = page["items"][0]
    assert item["status"] == "succeeded" and item["attempt"] == 1
    assert item["input"]["q"].startswith("ticket ")
    assert {s["evaluator"] for s in item["scores"]} == {"label", "speed"}


def test_filter_to_items_that_failed_an_evaluator(client: TestClient, db: Session) -> None:
    run = completed_run(client, db, {"wrong_first": 3})
    failed = client.get(f"/v1/runs/{run}/items", params={"failed_evaluator": "label"}).json()
    assert len(failed["items"]) == 3
    for item in failed["items"]:
        assert item["output"] == {"label": "WRONG"}
        label = next(s for s in item["scores"] if s["evaluator"] == "label")
        assert label["passed"] is False and "expected" in label["rationale"]
    none = client.get(f"/v1/runs/{run}/items", params={"failed_evaluator": "speed"}).json()
    assert none["items"] == []


def test_pagination_and_status_filter(client: TestClient, db: Session) -> None:
    run = completed_run(client, db, {})
    first = client.get(f"/v1/runs/{run}/items", params={"limit": 15}).json()
    rest = client.get(
        f"/v1/runs/{run}/items", params={"limit": 15, "cursor": first["next_cursor"]}
    ).json()
    ids = [i["id"] for i in first["items"] + rest["items"]]
    assert len(ids) == N == len(set(ids)) and rest["next_cursor"] is None
    queued = client.get(f"/v1/runs/{run}/items", params={"status": "queued"}).json()
    assert queued["items"] == []


def test_items_are_project_scoped(
    client: TestClient, db: Session, make_client: ClientFactory
) -> None:
    import uuid

    run = completed_run(client, db, {})
    assert client.get(f"/v1/runs/{uuid.uuid4()}/items").status_code == 404
    assert client.get(f"/v1/runs/{run}/items").status_code == 200


def test_tag_filter(client: TestClient, db: Session) -> None:
    client.post(
        "/v1/datasets/tickets/versions",
        json={
            "items": [
                {
                    "input": {"q": "ticket 0"},
                    "expected_output": {"label": "billing"},
                    "tags": ["vip"],
                },
                {"input": {"q": "ticket 1"}, "expected_output": {"label": "bug"}},
            ]
        },
    )
    run = completed_run(client, db, {})
    vip = client.get(f"/v1/runs/{run}/items", params={"tag": "vip"}).json()["items"]
    assert [i["tags"] for i in vip] == [["vip"]]
