"""Trace and span context managers."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from types import TracebackType
from typing import Any

from .payload import SpanRecord, TraceRecord

logger = logging.getLogger("forgeeval_sdk")

SPAN_KINDS = frozenset({"llm", "retrieval", "tool", "other"})


def new_id() -> str:
    return uuid.uuid4().hex


def ms_since(start: float) -> int:
    return max(0, round((time.perf_counter() - start) * 1000))


class Span:
    """A timed step inside a trace. Use as ``with trace.span(name, kind=...) as s:``."""

    def __init__(self, trace: Trace, name: str, kind: str) -> None:
        if kind not in SPAN_KINDS:
            logger.warning("forgeeval: unknown span kind %r, using 'other'", kind)
            kind = "other"
        self.id = new_id()[:16]
        self.name = (name or "span")[:200]
        self.kind = kind
        self.attributes: dict[str, Any] = {}
        self._trace = trace
        self._record: SpanRecord | None = None
        self._t0 = 0.0

    def __enter__(self) -> Span:
        try:
            self._t0 = time.perf_counter()
            stack = self._trace._span_stack
            self._record = SpanRecord(
                id=self.id,
                name=self.name,
                kind=self.kind,
                started_at=datetime.now(UTC),
                parent_id=stack[-1] if stack else None,
            )
            self._trace._spans.append(self._record)
            stack.append(self.id)
        except Exception:  # pragma: no cover - defensive
            logger.exception("forgeeval: span enter failed")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            if self._record is not None:
                self._record.duration_ms = ms_since(self._t0)
                if exc_type is not None:
                    self.attributes.setdefault("error", exc_type.__name__)
                self._record.attributes = self.attributes
            stack = self._trace._span_stack
            if self.id in stack:
                del stack[stack.index(self.id) :]
        except Exception:  # pragma: no cover - defensive
            logger.exception("forgeeval: span exit failed")


class Trace:
    """One LLM call / request. Created via ``client.trace(...)``.

    Spans nest according to the ``with`` structure. A trace is intended to be used from a
    single thread.
    """

    def __init__(
        self,
        submit: Callable[[TraceRecord], None],
        model: str,
        prompt: tuple[str, int] | None = None,
        input: Any = None,
        trace_id: str | None = None,
    ) -> None:
        self.id = trace_id or new_id()
        self.model = model
        self.prompt = prompt
        self.input: Any = input
        self.output: Any = None
        self.status = "ok"
        self.started_at = datetime.now(UTC)
        self._submit = submit
        self._t0 = time.perf_counter()
        self._spans: list[SpanRecord] = []
        self._span_stack: list[str] = []
        self._input_tokens: int | None = None
        self._output_tokens: int | None = None
        self._cost_usd: float | Decimal | str | None = None

    def span(self, name: str, kind: str = "other") -> Span:
        return Span(self, name, kind)

    def usage(
        self,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cost_usd: float | Decimal | str | None = None,
    ) -> None:
        if input_tokens is not None:
            self._input_tokens = max(0, int(input_tokens))
        if output_tokens is not None:
            self._output_tokens = max(0, int(output_tokens))
        if cost_usd is not None:
            self._cost_usd = cost_usd

    def __enter__(self) -> Trace:
        self.started_at = datetime.now(UTC)
        self._t0 = time.perf_counter()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # Never swallow or replace the user's exception; never raise from here.
        try:
            if exc_type is not None:
                self.status = "error"
            record = TraceRecord(
                id=self.id,
                model=self.model,
                started_at=self.started_at,
                latency_ms=ms_since(self._t0),
                status=self.status,
                input=self.input,
                output=self.output,
                prompt=self.prompt,
                input_tokens=self._input_tokens,
                output_tokens=self._output_tokens,
                cost_usd=self._cost_usd,
                spans=self._spans,
            )
            self._submit(record)
        except Exception:
            logger.exception("forgeeval: failed to record trace")
