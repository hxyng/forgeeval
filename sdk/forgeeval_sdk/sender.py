"""Background batch sender: one daemon thread, bounded queue, retries with jittered backoff."""

from __future__ import annotations

import email.utils
import logging
import random
import threading
import time
from collections import deque
from typing import Any

import httpx

logger = logging.getLogger("forgeeval_sdk")

MAX_BATCH = 500  # server limit per request
BACKOFF_BASE = 0.5
BACKOFF_CAP = 30.0
MAX_ATTEMPTS = 5
RETRY_AFTER_CAP = 120.0
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


def _sleep(seconds: float) -> None:
    """Indirection so tests can patch it."""
    time.sleep(seconds)


def backoff_delay(attempt: int) -> float:
    """Full jitter: uniform(0, min(cap, base * 2**attempt)); ``attempt`` starts at 0."""
    return random.uniform(0.0, min(BACKOFF_CAP, BACKOFF_BASE * (2**attempt)))


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        seconds = when.timestamp() - time.time()
    if seconds != seconds:  # NaN
        return None
    return min(max(seconds, 0.0), RETRY_AFTER_CAP)


class Sender:
    def __init__(
        self,
        client: httpx.Client,
        url: str,
        headers: dict[str, str],
        batch_size: int,
        flush_interval: float,
        max_queue: int,
    ) -> None:
        self._http = client
        self._url = url
        self._headers = headers
        self._batch_size = max(1, min(batch_size, MAX_BATCH))
        self._interval = max(0.01, flush_interval)
        self._max_queue = max(1, max_queue)
        self._q: deque[dict[str, Any]] = deque()
        self._cond = threading.Condition()
        self._thread: threading.Thread | None = None
        self._closing = False
        self._flush_req = False
        self._inflight = False
        self._stats = {
            "queued": 0,
            "dropped": 0,
            "sent": 0,
            "duplicates": 0,
            "rejected": 0,
            "failed": 0,
            "retries": 0,
        }

    # ---- caller-side API (never blocks, never raises) ----

    def enqueue(self, payload: dict[str, Any]) -> bool:
        with self._cond:
            if self._closing or len(self._q) >= self._max_queue:
                self._stats["dropped"] += 1
                return False
            self._q.append(payload)
            self._stats["queued"] += 1
            self._ensure_thread()
            if len(self._q) >= self._batch_size:
                self._cond.notify_all()
            return True

    def flush(self, timeout: float | None = None) -> bool:
        """Block until everything queued so far has been attempted. True if fully drained."""
        with self._cond:
            if not self._q and not self._inflight:
                return True
            self._flush_req = True
            self._ensure_thread()
            self._cond.notify_all()
            return self._cond.wait_for(
                lambda: not self._q and not self._inflight and not self._flush_req,
                timeout,
            )

    def close(self, timeout: float | None = 10.0) -> None:
        with self._cond:
            self._closing = True
            self._flush_req = True
            self._cond.notify_all()
            thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)

    def stats(self) -> dict[str, int]:
        with self._cond:
            out = dict(self._stats)
            out["pending"] = len(self._q) + (1 if self._inflight else 0)
            return out

    # ---- worker ----

    def _ensure_thread(self) -> None:
        # called with the lock held
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name="forgeeval-sender", daemon=True)
            self._thread.start()

    def _next_batch(self) -> list[dict[str, Any]] | None:
        deadline = time.monotonic() + self._interval
        with self._cond:
            while True:
                n = len(self._q)
                if self._closing and n == 0:
                    self._flush_req = False
                    self._cond.notify_all()
                    return None
                if n == 0 and self._flush_req:
                    self._flush_req = False
                    self._cond.notify_all()
                now = time.monotonic()
                if n and (n >= self._batch_size or self._flush_req or now >= deadline):
                    batch = [self._q.popleft() for _ in range(min(n, self._batch_size))]
                    self._inflight = True
                    return batch
                if now >= deadline:
                    deadline = now + self._interval
                self._cond.wait(max(0.0, deadline - now))

    def _run(self) -> None:
        while True:
            try:
                batch = self._next_batch()
                if batch is None:
                    return
                try:
                    self._send(batch)
                except Exception:
                    logger.exception("forgeeval: unexpected error sending batch; dropping it")
                    self._bump("failed", len(batch))
                finally:
                    with self._cond:
                        self._inflight = False
                        self._cond.notify_all()
            except Exception:  # pragma: no cover - defensive, keeps the thread alive
                logger.exception("forgeeval: sender loop error")
                _sleep(1.0)

    def _bump(self, key: str, n: int = 1) -> None:
        with self._cond:
            self._stats[key] += n

    def _send(self, batch: list[dict[str, Any]]) -> None:
        body = {"traces": batch}
        for attempt in range(MAX_ATTEMPTS):
            retry_after: float | None = None
            reason: str
            try:
                resp = self._http.post(self._url, json=body, headers=self._headers)
            except httpx.HTTPError as exc:
                reason = f"network error: {exc.__class__.__name__}: {exc}"
            else:
                if resp.is_success:
                    self._handle_success(resp, len(batch))
                    return
                if resp.status_code not in RETRYABLE_STATUS:
                    logger.error(
                        "forgeeval: upload of %d traces rejected with HTTP %d, not retrying: %s",
                        len(batch),
                        resp.status_code,
                        resp.text[:500],
                    )
                    self._bump("failed", len(batch))
                    return
                reason = f"HTTP {resp.status_code}"
                if resp.status_code in (429, 503):
                    retry_after = parse_retry_after(resp.headers.get("Retry-After"))
            if attempt == MAX_ATTEMPTS - 1:
                logger.error(
                    "forgeeval: giving up on %d traces after %d attempts (%s)",
                    len(batch),
                    MAX_ATTEMPTS,
                    reason,
                )
                self._bump("failed", len(batch))
                return
            delay = retry_after if retry_after is not None else backoff_delay(attempt)
            logger.warning(
                "forgeeval: upload failed (%s); retrying in %.2fs (attempt %d/%d)",
                reason,
                delay,
                attempt + 1,
                MAX_ATTEMPTS,
            )
            self._bump("retries")
            _sleep(delay)

    def _handle_success(self, resp: httpx.Response, sent: int) -> None:
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        rejected = data.get("rejected") or []
        for item in rejected:
            logger.warning(
                "forgeeval: trace rejected by server (index=%s id=%s): %s",
                item.get("index"),
                item.get("id"),
                item.get("error"),
            )
        for ref in data.get("unresolved_prompts") or []:
            logger.warning("forgeeval: prompt not registered on server: %s", ref)
        with self._cond:
            self._stats["sent"] += int(data.get("accepted", sent - len(rejected)))
            self._stats["duplicates"] += int(data.get("duplicates", 0))
            self._stats["rejected"] += len(rejected)
