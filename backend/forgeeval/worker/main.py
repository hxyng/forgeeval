"""Evaluation worker: pull run items from the queue and process them, one at a time.

Scale out by running more worker processes (compose runs two). Each loop iteration also gives
the reaper a chance to run; an advisory lock keeps it to one worker at a time.
"""

import logging
import os
import signal
import socket
import time
from dataclasses import dataclass, field
from types import FrameType

from forgeeval.db.session import SessionLocal
from forgeeval.models_client import ModelClient, default_client
from forgeeval.queue import JobQueue, get_queue
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
            with SessionLocal() as db:
                reap(db, self.queue)
        for msg in self.queue.receive(self.worker_id, self.batch, self.wait_seconds):
            with SessionLocal() as db:
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


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    worker = Worker(queue=get_queue(), models=default_client())
    signal.signal(signal.SIGTERM, worker.stop)
    signal.signal(signal.SIGINT, worker.stop)
    worker.run_forever()


if __name__ == "__main__":
    main()
