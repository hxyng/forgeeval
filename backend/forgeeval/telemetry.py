"""OpenTelemetry setup shared by the API and the worker.

One trace follows an evaluation end to end: the HTTP request that creates a run, the
``queue.publish`` span that enqueues its items, and every worker span that processes them. The
queue carries W3C ``traceparent`` in each message (see queue.py), and the worker continues that
context, so Jaeger shows API, Redis, worker and Postgres spans in one tree.

Exporting is enabled by ``OTEL_EXPORTER_OTLP_ENDPOINT`` (compose sets it to the collector).
Without it, spans and metrics are still created, so tests can read them in memory, but nothing
leaves the process.
"""

import logging
import os
from collections.abc import Callable, Mapping
from typing import Any

from fastapi import FastAPI
from opentelemetry import metrics, propagate, trace
from opentelemetry.context import Context
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from sqlalchemy.engine import Engine

_providers_installed = False
_instrumented_engines: set[int] = set()
_libraries_instrumented = False

tracer = trace.get_tracer("forgeeval")
meter = metrics.get_meter("forgeeval")

# ---------- metrics (names follow OpenTelemetry semantic-convention style) ----------

ingested_traces = meter.create_counter(
    "forgeeval.ingest.traces", unit="{trace}", description="Traces received, by outcome"
)
worker_items = meter.create_counter(
    "forgeeval.worker.items", unit="{item}", description="Run items processed, by outcome"
)
worker_item_duration = meter.create_histogram(
    "forgeeval.worker.item.duration", unit="ms", description="Wall time to process one run item"
)
eval_cost = meter.create_counter(
    "forgeeval.eval.cost", unit="USD", description="Model and judge cost of evaluated items"
)
runs_created = meter.create_counter(
    "forgeeval.runs.created", unit="{run}", description="Evaluation runs created, by trigger"
)


def setup_telemetry(
    service_name: str,
    *,
    span_processors: list[SpanProcessor] | None = None,
    metric_readers: list[MetricReader] | None = None,
) -> None:
    """Install tracer and meter providers once per process. The first caller wins, so tests can
    install in-memory exporters before the app module calls this at import time."""
    global _providers_installed, _libraries_instrumented
    if not _providers_installed:
        resource = Resource.create(
            {
                "service.name": os.environ.get("OTEL_SERVICE_NAME", service_name),
                "service.namespace": "forgeeval",
                "deployment.environment": os.environ.get("FORGEEVAL_ENV", "local"),
            }
        )
        processors = list(span_processors or [])
        readers = list(metric_readers or [])
        if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            exporter: SpanExporter = OTLPSpanExporter()
            processors.append(BatchSpanProcessor(exporter))
            readers.append(
                PeriodicExportingMetricReader(OTLPMetricExporter(), export_interval_millis=10_000)
            )
        provider = TracerProvider(resource=resource)
        for p in processors:
            provider.add_span_processor(p)
        trace.set_tracer_provider(provider)
        metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=readers))
        _providers_installed = True

    if not _libraries_instrumented:
        RedisInstrumentor().instrument()
        HTTPXClientInstrumentor().instrument()
        _libraries_instrumented = True


def instrument_app(app: FastAPI) -> None:
    if not getattr(app.state, "otel_instrumented", False):
        FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,readyz")
        app.state.otel_instrumented = True


def instrument_engine(engine: Engine) -> None:
    if id(engine) not in _instrumented_engines:
        SQLAlchemyInstrumentor().instrument(engine=engine)
        _instrumented_engines.add(id(engine))


# ---------- context propagation through the queue ----------


def inject_headers() -> dict[str, str]:
    """W3C trace context for the current span, to store alongside a queued message."""
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier


def extract_context(headers: Mapping[str, str]) -> Context:
    return propagate.extract(dict(headers))


# ---------- log correlation ----------


class TraceIdFilter(logging.Filter):
    """Adds ``trace_id``/``span_id`` to every record so a log line can be found in Jaeger."""

    def filter(self, record: logging.LogRecord) -> bool:
        ctx = trace.get_current_span().get_span_context()
        record.trace_id = format(ctx.trace_id, "032x") if ctx.is_valid else "-"
        record.span_id = format(ctx.span_id, "016x") if ctx.is_valid else "-"
        return True


LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s trace_id=%(trace_id)s %(message)s"


def configure_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    handler.addFilter(TraceIdFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)


def record(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
    """Record a metric without ever letting telemetry break the request or job."""
    try:
        fn(*args, **kwargs)
    except Exception:  # pragma: no cover - defensive
        logging.getLogger("forgeeval.telemetry").debug("metric record failed", exc_info=True)
