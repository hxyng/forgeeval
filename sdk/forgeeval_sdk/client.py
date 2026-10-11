"""The ForgeEval client."""

from __future__ import annotations

import atexit
import logging
import os
import weakref
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from types import TracebackType
from typing import Any

import httpx

from .payload import SpanRecord, TraceRecord, build_payload
from .redact import DEFAULT_REDACTORS, Matcher, Redactor, as_redactor
from .sender import Sender
from .trace import SPAN_KINDS, Trace, new_id

logger = logging.getLogger("forgeeval_sdk")

DEFAULT_BASE_URL = "http://localhost:8000"
INGEST_PATH = "/v1/traces:batch"

PromptArg = tuple[str, int] | Mapping[str, Any] | None


def _norm_prompt(prompt: PromptArg) -> tuple[str, int] | None:
    if prompt is None:
        return None
    try:
        if isinstance(prompt, Mapping):
            return (str(prompt["name"]), int(prompt["version"]))
        name, version = prompt
        return (str(name), int(version))
    except (KeyError, TypeError, ValueError):
        logger.warning("forgeeval: ignoring malformed prompt reference %r", prompt)
        return None


class ForgeEval:
    """Captures traces and uploads them in the background.

    The tracing path never raises into the host application and never blocks on the network:
    traces are redacted in the calling thread, queued, and sent by a daemon thread.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        *,
        batch_size: int = 100,
        flush_interval: float = 2.0,
        max_queue: int = 10_000,
        redactors: Iterable[Redactor | tuple[str, Matcher]] = DEFAULT_REDACTORS,
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        key = api_key or os.environ.get("FORGEEVAL_API_KEY")
        url = base_url or os.environ.get("FORGEEVAL_BASE_URL") or DEFAULT_BASE_URL
        self.redactors: tuple[Redactor, ...] = tuple(as_redactor(r) for r in redactors)
        self._http = httpx.Client(timeout=timeout, transport=transport)
        self._enabled = bool(key)
        if not key:
            logger.warning(
                "forgeeval: no API key (api_key= or FORGEEVAL_API_KEY); tracing is disabled"
            )
        self._sender = Sender(
            self._http,
            url.rstrip("/") + INGEST_PATH,
            {"Authorization": f"Bearer {key}"},
            batch_size,
            flush_interval,
            max_queue,
        )
        self._closed = False
        ref = weakref.ref(self)

        def _at_exit() -> None:
            client = ref()
            if client is not None:
                client.close(timeout=10.0)

        self._atexit = _at_exit
        atexit.register(_at_exit)

    # ---- capturing ----

    def trace(
        self,
        model: str,
        *,
        prompt: PromptArg = None,
        input: Any = None,
        id: str | None = None,
    ) -> Trace:
        """Start a trace; use as a context manager. ``id`` defaults to a fresh uuid4 hex."""
        return Trace(self._submit, model, _norm_prompt(prompt), input, id)

    def log_trace(
        self,
        model: str,
        *,
        input: Any = None,
        output: Any = None,
        latency_ms: int = 0,
        started_at: datetime | None = None,
        status: str = "ok",
        prompt: PromptArg = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cost_usd: float | Decimal | str | None = None,
        spans: Sequence[Mapping[str, Any]] = (),
        id: str | None = None,
    ) -> str:
        """Record an already-finished trace. Returns its id.

        Each span is a mapping with ``name``, ``started_at``, ``duration_ms`` and optionally
        ``kind``, ``id``, ``parent_id`` (must reference an earlier span) and ``attributes``.
        """
        trace_id = id or new_id()
        try:
            start = started_at or datetime.now(UTC)
            records = [self._span_from_mapping(s, start) for s in spans]
            self._submit(
                TraceRecord(
                    id=trace_id,
                    model=model,
                    started_at=start,
                    latency_ms=max(0, int(latency_ms)),
                    status="error" if status == "error" else "ok",
                    input=input,
                    output=output,
                    prompt=_norm_prompt(prompt),
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_usd=cost_usd,
                    spans=records,
                )
            )
        except Exception:
            logger.exception("forgeeval: failed to log trace")
        return trace_id

    @staticmethod
    def _span_from_mapping(s: Mapping[str, Any], default_start: datetime) -> SpanRecord:
        kind = s.get("kind", "other")
        return SpanRecord(
            id=str(s.get("id") or new_id()[:16]),
            name=str(s.get("name") or "span")[:200],
            kind=kind if kind in SPAN_KINDS else "other",
            started_at=s.get("started_at") or default_start,
            duration_ms=max(0, int(s.get("duration_ms", 0))),
            parent_id=s.get("parent_id"),
            attributes=dict(s.get("attributes") or {}),
        )

    def _submit(self, record: TraceRecord) -> None:
        try:
            if not self._enabled:
                return
            self._sender.enqueue(build_payload(record, self.redactors))
        except Exception:
            logger.exception("forgeeval: failed to queue trace; dropping it")

    # ---- lifecycle ----

    def stats(self) -> dict[str, int]:
        """Counters: queued, dropped, sent, duplicates, rejected, failed, retries, pending."""
        return self._sender.stats()

    def flush(self, timeout: float | None = 30.0) -> bool:
        """Wait until queued traces have been sent (or given up on). True if fully drained."""
        try:
            return self._sender.flush(timeout)
        except Exception:
            logger.exception("forgeeval: flush failed")
            return False

    def close(self, timeout: float | None = 10.0) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            atexit.unregister(self._atexit)
            self._sender.close(timeout)
            self._http.close()
        except Exception:
            logger.exception("forgeeval: close failed")

    def __enter__(self) -> ForgeEval:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
