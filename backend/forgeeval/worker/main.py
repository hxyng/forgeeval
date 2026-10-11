"""Evaluation worker entrypoint.

Job consumption lands in a later layer; for now the worker proves it can reach Redis.
"""

import logging
import signal
import time
from types import FrameType

import redis

from forgeeval.config import get_settings

log = logging.getLogger("forgeeval.worker")
_running = True


def _stop(signum: int, _frame: FrameType | None) -> None:
    global _running
    log.info("received signal %s, shutting down", signum)
    _running = False


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    client = redis.Redis.from_url(get_settings().redis_url)
    client.ping()
    log.info("worker connected to redis, idle until the job consumer is implemented")
    while _running:
        time.sleep(1)


if __name__ == "__main__":
    main()
