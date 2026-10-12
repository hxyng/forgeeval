"""SQS queue and S3 payload offload against local emulators (ElasticMQ, S3Mock).

Locally these skip when the emulators aren't running; CI sets REQUIRE_AWS_EMULATORS=1 so they
can never silently skip there.
"""

import os
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any

import boto3
import httpx
import pytest
from fastapi.testclient import TestClient
from scenarios import setup_tickets
from sqlalchemy import select
from sqlalchemy.orm import Session

from forgeeval.api.main import app
from forgeeval.db.models import Trace
from forgeeval.models_client import FakeModels
from forgeeval.queue import SqsQueue, get_queue
from forgeeval.storage import S3PayloadStore, get_store
from forgeeval.telemetry import tracer
from forgeeval.worker.main import Worker

SQS_URL = os.environ.get("TEST_SQS_ENDPOINT", "http://localhost:9324")
S3_URL = os.environ.get("TEST_S3_ENDPOINT", "http://localhost:9091")
CREDS: dict[str, Any] = {
    "region_name": "us-east-1",
    "aws_access_key_id": "test",
    "aws_secret_access_key": "test",
}
ClientFactory = Callable[..., TestClient]


def _require(url: str, name: str) -> None:
    try:
        httpx.get(url, timeout=2)
    except httpx.HTTPError:
        if os.environ.get("REQUIRE_AWS_EMULATORS") == "1":
            raise
        pytest.skip(f"{name} not running at {url}")


@pytest.fixture
def sqs_queue() -> Iterator[SqsQueue]:
    _require(SQS_URL, "ElasticMQ")
    client = boto3.client("sqs", endpoint_url=SQS_URL, **CREDS)
    url = client.create_queue(
        QueueName=f"test-{uuid.uuid4().hex[:12]}", Attributes={"VisibilityTimeout": "1"}
    )["QueueUrl"]
    yield SqsQueue(client, url)
    client.delete_queue(QueueUrl=url)


@pytest.fixture
def s3_store() -> S3PayloadStore:
    _require(S3_URL, "S3Mock")
    client = boto3.client("s3", endpoint_url=S3_URL, **CREDS)
    bucket = f"test-{uuid.uuid4().hex[:12]}"
    client.create_bucket(Bucket=bucket)
    return S3PayloadStore(client, bucket)


# ---------- SQS ----------


def test_sqs_round_trip_batches_and_trace_context(sqs_queue: SqsQueue) -> None:
    ids = [uuid.uuid4() for _ in range(23)]  # > 2 SendMessageBatch calls of 10
    with tracer.start_as_current_span("producer") as span:
        sqs_queue.enqueue(ids)
        trace_id = format(span.get_span_context().trace_id, "032x")
    assert sqs_queue.depth() == 23

    received = []
    for _ in range(10):
        received += sqs_queue.receive("w", max_messages=10, wait_seconds=1)
        if len(received) >= 23:
            break
    assert {m.run_item_id for m in received} == set(ids)
    assert all(trace_id in m.headers["traceparent"] for m in received)
    assert all(m.deliveries == 1 for m in received)
    for m in received:
        sqs_queue.ack(m)
    assert sqs_queue.depth() == 0


def test_sqs_redelivers_unacked_message_after_visibility_timeout(sqs_queue: SqsQueue) -> None:
    item = uuid.uuid4()
    sqs_queue.enqueue([item])
    first = sqs_queue.receive("crashed", max_messages=1, wait_seconds=1)
    assert [m.run_item_id for m in first] == [item]
    time.sleep(1.5)  # queue visibility timeout is 1 s
    again = sqs_queue.receive("w2", max_messages=1, wait_seconds=2)
    assert [m.run_item_id for m in again] == [item] and again[0].deliveries == 2


def test_worker_drains_a_run_through_sqs(
    make_client: ClientFactory, db: Session, sqs_queue: SqsQueue
) -> None:
    from forgeeval.db.models import EvalRun, RunStatus

    app.dependency_overrides[get_queue] = lambda: sqs_queue
    try:
        c = make_client()
        setup_tickets(c)
        c.post("/v1/prompts/oracle/versions", json={"template": "Q: {q}", "model": "fake/oracle"})
        run_id = c.post(
            "/v1/runs",
            json={
                "dataset": {"name": "tickets"},
                "prompt": {"name": "oracle"},
                "evaluators": [{"name": "label"}],
            },
        ).json()["id"]
    finally:
        app.dependency_overrides.pop(get_queue, None)
    worker = Worker(queue=sqs_queue, models=FakeModels(), worker_id="sqs", wait_seconds=1)
    for _ in range(6):
        worker.run_once()
        db.expire_all()
        if db.get_one(EvalRun, uuid.UUID(run_id)).status is RunStatus.succeeded:
            break
    assert db.get_one(EvalRun, uuid.UUID(run_id)).status is RunStatus.succeeded


# ---------- S3 offload ----------


@pytest.fixture
def client(make_client: ClientFactory, s3_store: S3PayloadStore) -> Iterator[TestClient]:
    app.dependency_overrides[get_store] = lambda: s3_store
    yield make_client()
    app.dependency_overrides.pop(get_store, None)


def trace(tid: str, text: str) -> dict[str, Any]:
    return {
        "id": tid,
        "model": "m",
        "latency_ms": 3,
        "input": {"prompt": text},
        "output": {"text": "ok"},
        "started_at": datetime.now(UTC).isoformat(),
    }


def test_large_payload_is_offloaded_and_read_back(
    client: TestClient, db: Session, s3_store: S3PayloadStore
) -> None:
    big = "x" * (300 * 1024)  # over the 256 KiB inline limit, under the 2 MiB offload limit
    out = client.post(
        "/v1/traces:batch", json={"traces": [trace("big", big), trace("small", "hi")]}
    )
    assert out.json()["accepted"] == 2, out.text

    rows = {t.idempotency_key: t for t in db.scalars(select(Trace))}
    sql_null = db.scalar(select(Trace.input.is_(None)).where(Trace.idempotency_key == "big"))
    assert sql_null is True, "offloaded payload columns should be SQL NULL, not JSON null"
    assert rows["big"].input is None and rows["big"].payload_s3_key
    assert rows["small"].input == {"prompt": "hi"} and rows["small"].payload_s3_key is None

    detail = client.get(f"/v1/traces/{rows['big'].id}").json()
    assert detail["input"]["prompt"] == big and detail["output"] == {"text": "ok"}

    client.post("/v1/traces:batch", json={"traces": [trace("big", big)]})  # retry: same key
    keys = s3_store._s3.list_objects_v2(Bucket=s3_store.bucket).get("Contents", [])
    assert len(keys) == 1


def test_dataset_promotion_reads_offloaded_payload(client: TestClient, db: Session) -> None:
    big = "y" * (100 * 1024)
    client.post("/v1/traces:batch", json={"traces": [trace("p1", big)]})
    tid = str(db.scalar(select(Trace.id).where(Trace.idempotency_key == "p1")))
    client.post("/v1/datasets", json={"name": "from-prod"})
    resp = client.post(
        "/v1/datasets/from-prod/versions",
        json={"from_traces": {"trace_ids": [tid], "use_output_as_expected": True}},
    )
    assert resp.status_code == 201, resp.text
    item = client.get("/v1/datasets/from-prod/versions/1/items").json()["items"][0]
    assert item["input"]["prompt"] == big and item["expected_output"] == {"text": "ok"}


def test_payload_limits_and_upload_failure(client: TestClient, make_client: ClientFactory) -> None:
    too_big = client.post(
        "/v1/traces:batch", json={"traces": [trace("huge", "z" * (3 * 1024 * 1024))]}
    ).json()
    assert too_big["accepted"] == 0 and "limit is 2097152" in too_big["rejected"][0]["error"]

    class Broken:
        def put(self, key: str, payload: dict[str, Any]) -> None:
            raise ConnectionError("s3 down")

        def get(self, key: str) -> dict[str, Any]:
            raise AssertionError

    app.dependency_overrides[get_store] = Broken
    out = client.post(
        "/v1/traces:batch", json={"traces": [trace("b", "w" * (100 * 1024)), trace("s", "hi")]}
    ).json()
    assert out["accepted"] == 1
    assert out["rejected"] == [
        {"index": 0, "id": "b", "error": "payload storage unavailable; retry"}
    ]

    app.dependency_overrides[get_store] = lambda: None
    inline = client.post(
        "/v1/traces:batch", json={"traces": [trace("n", "v" * (300 * 1024))]}
    ).json()
    assert "limit is 262144" in inline["rejected"][0]["error"]
