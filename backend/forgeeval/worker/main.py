"""Evaluation worker: pull run items from the queue and process them, one at a time.

Scale out by running more worker processes (compose runs two). Each loop iteration also gives
the reaper a chance to run; an advisory lock keeps it to one worker at a time.
"""

import logging
import os
import signal
import socket
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from types import FrameType

from opentelemetry.metrics import CallbackOptions, Observation
from opentelemetry.trace import SpanKind

from forgeeval.config import get_settings
from forgeeval.db.session import SessionLocal, engine
from forgeeval.model_cache import CachingModelClient, PgCacheStore
from forgeeval.models_client import ModelClient, default_client
from forgeeval.queue import JobQueue, get_queue
from forgeeval.telemetry import (
    configure_logging,
    extract_context,
    instrument_engine,
    meter,
    setup_telemetry,
    tracer,
)
from forgeeval.worker.processor import Outcome, process
from forgeeval.worker.reaper import reap

log = logging.getLogger("forgeeval.worker")

REAP_EVERY_SECONDS = 5.0


@dataclass
class Worker:
    queue: JobQueue
    models: ModelClient
    worker_id: str = field(default_factory=lambda: f"{socket.gethostname()}-{os.getpid()}")
    batch: int = 10
    wait_seconds: float = 2.0
    running: bool = True
    _last_reap: float = 0.0

    def run_once(self) -> dict[Outcome, int]:
        """Receive one batch, process it, and maybe reap. Returns outcome counts."""
        counts: dict[Outcome, int] = {}
        now = time.monotonic()
        if now - self._last_reap >= REAP_EVERY_SECONDS:
            self._last_reap = now
            with tracer.start_as_current_span("reaper"), SessionLocal() as db:
                reap(db, self.queue)
        for msg in self.queue.receive(self.worker_id, self.batch, self.wait_seconds):
            # Continue the trace started by the request that enqueued this item.
            consumer_span = tracer.start_as_current_span(
                "eval-jobs process",
                context=extract_context(msg.headers),
                kind=SpanKind.CONSUMER,
                attributes={
                    "messaging.system": "redis_streams",
                    "messaging.operation.type": "process",
                    "messaging.message.id": msg.receipt,
                    "messaging.message.delivery_count": msg.deliveries,
                },
            )
            with consumer_span, SessionLocal() as db:
                outcome = process(db, msg.run_item_id, self.worker_id, self.models)
            # Ack in every case: the database now records the item's state, and a retry is
            # re-enqueued by the reaper when its backoff is due.
            self.queue.ack(msg)
            counts[outcome] = counts.get(outcome, 0) + 1
        return counts

    def stop(self, signum: int, _frame: FrameType | None) -> None:
        log.info("received signal %s; finishing the current batch", signum)
        self.running = False

    def run_forever(self) -> None:
        log.info("worker %s started", self.worker_id)
        while self.running:
            try:
                self.run_once()
            except Exception:
                log.exception("worker loop error; backing off")
                time.sleep(1.0)
        log.info("worker %s stopped", self.worker_id)


def observe_queue_depth(queue: JobQueue) -> None:
    def callback(_options: CallbackOptions) -> Iterable[Observation]:
        try:
            yield Observation(queue.depth())
        except Exception:
            log.debug("queue depth unavailable", exc_info=True)

    meter.create_observable_gauge(
        "forgeeval.queue.depth",
        callbacks=[callback],
        unit="{message}",
        description="Messages enqueued but not yet acknowledged",
    )


def main() -> None:
    configure_logging()
    setup_telemetry("forgeeval-worker")
    instrument_engine(engine)
    queue = get_queue()
    observe_queue_depth(queue)
    cfg = get_settings()
    models = CachingModelClient(
        default_client(),
        PgCacheStore(SessionLocal),
        enabled=cfg.cache_enabled,
        max_age_days=cfg.cache_max_age_days,
        cache_sampled_outputs=cfg.cache_sampled_outputs,
    )
    worker = Worker(queue=queue, models=models)
    signal.signal(signal.SIGTERM, worker.stop)
    signal.signal(signal.SIGINT, worker.stop)
    worker.run_forever()


if __name__ == "__main__":
    main()
