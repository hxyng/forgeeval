import uuid
from collections.abc import Callable, Iterator, Sequence
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from forgeeval.api.main import app
from forgeeval.db.models import DatasetVersion, EvalRun, ItemStatus, RunItem
from forgeeval.queue import JobQueue, Message, RedisStreamQueue, get_queue

ClientFactory = Callable[..., TestClient]


class RecordingQueue:
    def __init__(self, fail: bool = False) -> None:
        self.ids: list[uuid.UUID] = []
        self.fail = fail

    def enqueue(self, run_item_ids: Sequence[uuid.UUID]) -> None:
        if self.fail:
            raise ConnectionError("redis down")
        self.ids.extend(run_item_ids)

    def receive(self, consumer: str, max_messages: int, wait_seconds: float) -> list[Message]:
        return []

    def ack(self, message: Message) -> None: ...

    def depth(self) -> int:
        return len(self.ids)


@pytest.fixture
def queue() -> Iterator[RecordingQueue]:
    q = RecordingQueue()
    app.dependency_overrides[get_queue] = lambda: q
    yield q
    app.dependency_overrides.pop(get_queue, None)


@pytest.fixture
def client(make_client: ClientFactory) -> TestClient:
    c = make_client()
    c.post("/v1/prompts/answer/versions", json={"template": "Q: {q}", "model": "fake/echo"})
    c.post("/v1/datasets", json={"name": "faq"})
    c.post(
        "/v1/datasets/faq/versions",
        json={
            "items": [
                {"input": {"q": f"question {i}"}, "expected_output": {"a": i}} for i in range(4)
            ]
        },
    )
    c.post("/v1/evaluators", json={"name": "match", "kind": "exact_match"})
    c.post("/v1/evaluators/match/versions", json={"config": {"field": "a"}})
    c.post("/v1/evaluators", json={"name": "speed", "kind": "latency"})
    c.post("/v1/evaluators/speed/versions", json={"config": {"max_ms": 2000}})
    return c


RUN: dict[str, Any] = {
    "dataset": {"name": "faq"},
    "prompt": {"name": "answer", "version": 1},
    "evaluators": [{"name": "match"}, {"name": "speed", "version": 1}],
}


def test_create_run_fans_out_one_job_per_item(
    client: TestClient, queue: RecordingQueue, db: Session
) -> None:
    resp = client.post("/v1/runs", json=RUN)
    assert resp.status_code == 201, resp.text
    run = resp.json()
    assert run["status"] == "queued"
    assert run["total_items"] == 4 and run["progress"]["queued"] == 4
    assert run["dataset_version"] == 1 and run["prompt_version"] == 1
    assert [e["name"] for e in run["evaluators"]] == ["match", "speed"]

    stored = set(db.scalars(select(RunItem.id).where(RunItem.run_id == run["id"])))
    assert set(queue.ids) == stored and len(stored) == 4


def test_run_freezes_its_dataset_version(
    client: TestClient, queue: RecordingQueue, db: Session
) -> None:
    run = client.post("/v1/runs", json=RUN).json()
    frozen = db.scalar(
        select(DatasetVersion.frozen_at)
        .join(EvalRun, EvalRun.dataset_version_id == DatasetVersion.id)
        .where(EvalRun.id == uuid.UUID(run["id"]))
    )
    assert frozen is not None


def test_enqueue_failure_still_records_the_run(client: TestClient, db: Session) -> None:
    failing = RecordingQueue(fail=True)
    app.dependency_overrides[get_queue] = lambda: failing
    try:
        resp = client.post("/v1/runs", json=RUN)
    finally:
        app.dependency_overrides.pop(get_queue, None)
    assert resp.status_code == 201
    queued = db.scalar(
        select(func.count()).where(
            RunItem.run_id == resp.json()["id"], RunItem.status == ItemStatus.queued
        )
    )
    assert queued == 4


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ({"dataset": {"name": "nope"}}, "Dataset 'nope'"),
        ({"prompt": {"name": "answer", "version": 7}}, "Prompt 'answer' version 7"),
        ({"evaluators": [{"name": "ghost"}]}, "Evaluator 'ghost'"),
        ({"evaluators": [{"name": "match"}, {"name": "match", "version": 1}]}, "listed twice"),
        ({"baseline_run_id": str(uuid.uuid4())}, "Baseline run not found"),
    ],
)
def test_invalid_run_requests(
    client: TestClient, queue: RecordingQueue, patch: dict[str, Any], message: str
) -> None:
    resp = client.post("/v1/runs", json={**RUN, **patch})
    assert resp.status_code == 422
    assert message in resp.json()["detail"]
    assert queue.ids == []


def test_baseline_must_share_the_dataset_version(client: TestClient, queue: RecordingQueue) -> None:
    first = client.post("/v1/runs", json=RUN).json()
    client.post("/v1/datasets/faq/versions", json={"items": [{"input": {"q": "new"}}]})
    resp = client.post(
        "/v1/runs", json={**RUN, "dataset": {"name": "faq"}, "baseline_run_id": first["id"]}
    )
    assert resp.status_code == 422 and "different dataset version" in resp.json()["detail"]

    ok = client.post(
        "/v1/runs",
        json={**RUN, "dataset": {"name": "faq", "version": 1}, "baseline_run_id": first["id"]},
    )
    assert ok.status_code == 201 and ok.json()["baseline_run_id"] == first["id"]


def test_cancel_skips_pending_items(client: TestClient, queue: RecordingQueue, db: Session) -> None:
    run = client.post("/v1/runs", json=RUN).json()
    resp = client.post(f"/v1/runs/{run['id']}/cancel")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "cancelled" and body["progress"]["cancelled"] == 4
    assert body["finished_at"] is not None
    assert client.post(f"/v1/runs/{run['id']}/cancel").status_code == 409


def test_list_and_get_are_project_scoped(
    client: TestClient, queue: RecordingQueue, make_client: ClientFactory
) -> None:
    ids = [client.post("/v1/runs", json=RUN).json()["id"] for _ in range(3)]
    page = client.get("/v1/runs", params={"limit": 2}).json()
    assert [r["id"] for r in page["items"]] == ids[::-1][:2]
    rest = client.get("/v1/runs", params={"cursor": page["next_cursor"]}).json()
    assert [r["id"] for r in rest["items"]] == [ids[0]]

    assert client.get(f"/v1/runs/{ids[0]}").status_code == 200
    assert client.get(f"/v1/runs/{uuid.uuid4()}").status_code == 404


def test_runs_require_scope(make_client: ClientFactory, queue: RecordingQueue) -> None:
    reader = make_client(["project:read"])
    assert reader.post("/v1/runs", json=RUN).status_code == 403


def test_run_jobs_reach_a_real_redis_queue(
    client: TestClient, redis_queue: RedisStreamQueue
) -> None:
    q: JobQueue = redis_queue
    app.dependency_overrides[get_queue] = lambda: q
    try:
        run = client.post("/v1/runs", json=RUN).json()
    finally:
        app.dependency_overrides.pop(get_queue, None)
    msgs = redis_queue.receive("w", max_messages=10, wait_seconds=0.2)
    assert len(msgs) == 4 and redis_queue.depth() == 4
    assert run["progress"]["queued"] == 4
