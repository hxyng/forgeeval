import logging
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, StatusCode
from scenarios import setup_tickets

from forgeeval.api.main import app
from forgeeval.models_client import FakeModels, Generation, TransientError
from forgeeval.queue import RedisStreamQueue, get_queue
from forgeeval.telemetry import TraceIdFilter, tracer
from forgeeval.worker.main import Worker, observe_queue_depth

ClientFactory = Callable[..., TestClient]


@pytest.fixture
def client(make_client: ClientFactory, redis_queue: RedisStreamQueue) -> Iterator[TestClient]:
    app.dependency_overrides[get_queue] = lambda: redis_queue
    c = make_client()
    setup_tickets(c)
    c.post("/v1/prompts/oracle/versions", json={"template": "Q: {q}", "model": "fake/oracle"})
    yield c
    app.dependency_overrides.pop(get_queue, None)


def start_run(c: TestClient) -> str:
    resp = c.post(
        "/v1/runs",
        json={
            "dataset": {"name": "tickets"},
            "prompt": {"name": "oracle"},
            "evaluators": [{"name": "label"}],
        },
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def named(spans: InMemorySpanExporter, name: str) -> list[ReadableSpan]:
    return [s for s in spans.get_finished_spans() if s.name == name]


def by_id(spans: InMemorySpanExporter) -> dict[int, ReadableSpan]:
    return {s.context.span_id: s for s in spans.get_finished_spans()}


def ancestors(span: ReadableSpan, index: dict[int, ReadableSpan]) -> list[str]:
    names, current = [], span
    while current.parent is not None and current.parent.span_id in index:
        current = index[current.parent.span_id]
        names.append(current.name)
    return names


def test_run_creation_traces_request_db_and_queue_publish(
    client: TestClient, spans: InMemorySpanExporter, redis_queue: RedisStreamQueue
) -> None:
    start_run(client)
    server = [
        s
        for s in spans.get_finished_spans()
        if s.kind is SpanKind.SERVER
        and s.attributes
        and s.attributes.get("http.route") == "/v1/runs"
    ]
    assert len(server) == 1
    trace_id = server[0].context.trace_id

    publish = named(spans, f"{redis_queue.stream} publish")
    assert len(publish) == 1 and publish[0].kind is SpanKind.PRODUCER
    assert publish[0].context.trace_id == trace_id
    assert publish[0].attributes and publish[0].attributes["messaging.batch.message_count"] == 20

    db_spans = [
        s
        for s in spans.get_finished_spans()
        if s.attributes
        and s.attributes.get("db.system") == "postgresql"
        and s.context.trace_id == trace_id
    ]
    assert db_spans, "SQLAlchemy spans should join the request trace"

    msg = redis_queue.receive("probe", max_messages=1, wait_seconds=0.2)[0]
    assert f"{trace_id:032x}" in msg.headers["traceparent"]


def test_worker_continues_the_request_trace(
    client: TestClient, spans: InMemorySpanExporter, redis_queue: RedisStreamQueue
) -> None:
    start_run(client)
    api_trace = named(spans, f"{redis_queue.stream} publish")[0].context.trace_id

    worker = Worker(queue=redis_queue, models=FakeModels(), worker_id="t", wait_seconds=0.1)
    worker.run_once()

    consumers = named(spans, "eval-jobs process")
    assert consumers and all(s.context.trace_id == api_trace for s in consumers)
    assert all(s.kind is SpanKind.CONSUMER for s in consumers)
    assert consumers[0].attributes and consumers[0].attributes["forgeeval.outcome"] == "succeeded"

    index = by_id(spans)
    for child in ("claim", "generate", "score exact_match"):
        found = [s for s in named(spans, child) if s.context.trace_id == api_trace]
        assert found, child
        assert "eval-jobs process" in ancestors(found[0], index)
    generate = named(spans, "generate")[0]
    assert generate.attributes and generate.attributes["gen_ai.request.model"] == "fake/oracle"


class Flaky:
    def generate(
        self, model: str, prompt: str, params: dict[str, Any], expected: Any = None
    ) -> Generation:
        raise TransientError("HTTP 503 from model")


def test_failed_item_marks_span_as_error(
    client: TestClient, spans: InMemorySpanExporter, redis_queue: RedisStreamQueue
) -> None:
    start_run(client)
    Worker(queue=redis_queue, models=Flaky(), worker_id="t", wait_seconds=0.1).run_once()
    errored = [
        s for s in named(spans, "eval-jobs process") if s.status.status_code is StatusCode.ERROR
    ]
    assert errored
    assert any(e.name == "exception" for e in errored[0].events)
    assert errored[0].attributes and errored[0].attributes["forgeeval.outcome"] == "retrying"


def _points(reader: InMemoryMetricReader, name: str) -> list[Any]:
    data = reader.get_metrics_data()
    out: list[Any] = []
    for rm in data.resource_metrics if data else []:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                if m.name == name:
                    out.extend(m.data.data_points)
    return out


def test_metrics_record_ingest_runs_items_cost_and_queue_depth(
    client: TestClient, metric_reader: InMemoryMetricReader, redis_queue: RedisStreamQueue
) -> None:
    observe_queue_depth(redis_queue)
    start_run(client)
    depth = _points(metric_reader, "forgeeval.queue.depth")
    assert any(p.value == 20 for p in depth)

    Worker(queue=redis_queue, models=FakeModels(), worker_id="t", wait_seconds=0.1).run_once()
    items = {
        p.attributes["outcome"]: p.value for p in _points(metric_reader, "forgeeval.worker.items")
    }
    assert items.get("succeeded", 0) >= 10
    assert _points(metric_reader, "forgeeval.worker.item.duration")
    assert sum(p.value for p in _points(metric_reader, "forgeeval.eval.cost")) > 0
    assert any(
        p.attributes.get("trigger") == "manual"
        for p in _points(metric_reader, "forgeeval.runs.created")
    )

    client.post(
        "/v1/traces:batch",
        json={
            "traces": [
                {"id": "m1", "model": "m", "latency_ms": 1, "started_at": "2026-10-12T00:00:00Z"},
                {"model": "bad"},
            ]
        },
    )
    ingest = {
        p.attributes["outcome"]: p.value for p in _points(metric_reader, "forgeeval.ingest.traces")
    }
    assert ingest.get("accepted", 0) >= 1 and ingest.get("rejected", 0) >= 1


def test_log_lines_carry_the_trace_id() -> None:
    rec = logging.LogRecord("x", logging.INFO, __file__, 1, "hello", None, None)
    TraceIdFilter().filter(rec)
    assert rec.trace_id == "-"  # type: ignore[attr-defined]
    with tracer.start_as_current_span("t") as span:
        TraceIdFilter().filter(rec)
        expected = format(span.get_span_context().trace_id, "032x")
    assert rec.trace_id == expected  # type: ignore[attr-defined]
    assert trace.get_current_span().get_span_context().is_valid is False
