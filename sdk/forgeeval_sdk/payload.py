"""Turn finished trace records into server payloads (redact, size-limit, serialise)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from .redact import Redactor, scrub

MAX_PAYLOAD_BYTES = 256 * 1024  # server limit on input+output
MAX_SPANS = 500
TRUNCATION_MARKER = "...[truncated]"


@dataclass
class SpanRecord:
    id: str
    name: str
    kind: str
    started_at: datetime
    duration_ms: int = 0
    parent_id: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass
class TraceRecord:
    id: str
    model: str
    started_at: datetime
    latency_ms: int
    status: str = "ok"
    input: Any = None
    output: Any = None
    prompt: tuple[str, int] | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | Decimal | str | None = None
    spans: list[SpanRecord] = field(default_factory=list)


def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def _measure(holder: list[Any]) -> int:
    # Mirrors the server's check: json.dumps([input, output], default=str).encode()
    return len(json.dumps(holder, default=str).encode())


def _largest_string(node: Any, parent: Any, key: Any, best: list[Any]) -> None:
    if isinstance(node, str):
        if len(node) > best[0]:
            best[:] = [len(node), parent, key]
    elif isinstance(node, dict):
        for k, v in node.items():
            _largest_string(v, node, k, best)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _largest_string(v, node, i, best)


def truncate_to_fit(holder: list[Any], limit: int = MAX_PAYLOAD_BYTES) -> bool:
    """Shrink the largest string fields of ``holder`` ([input, output]) in place until the
    serialised size is within ``limit``. Returns True if anything was truncated."""
    size = _measure(holder)
    if size <= limit:
        return False
    for _ in range(1000):
        excess = size - limit
        best: list[Any] = [len(TRUNCATION_MARKER), None, None]
        _largest_string(holder, None, None, best)
        _, parent, key = best
        if parent is None:
            break
        s: str = parent[key]
        keep = max(0, len(s) - excess - len(TRUNCATION_MARKER))
        parent[key] = s[:keep] + TRUNCATION_MARKER
        size = _measure(holder)
        if size <= limit:
            return True
    # Nothing left to trim sensibly (e.g. a huge list of numbers): drop the content.
    holder[0] = TRUNCATION_MARKER if holder[0] is not None else None
    holder[1] = TRUNCATION_MARKER if holder[1] is not None else None
    return True


def build_payload(record: TraceRecord, redactors: Sequence[Redactor]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    holder = [
        scrub(record.input, redactors, counts),
        scrub(record.output, redactors, counts),
    ]

    spans = record.spans
    spans_dropped = max(0, len(spans) - MAX_SPANS)
    span_payloads: list[dict[str, Any]] = []
    for sp in spans[:MAX_SPANS]:
        attrs = scrub(sp.attributes, redactors, counts)
        span_payloads.append(
            {
                "id": sp.id[:128],
                "kind": sp.kind,
                "name": sp.name,
                "started_at": iso(sp.started_at),
                "duration_ms": max(0, sp.duration_ms),
                "parent_id": sp.parent_id,
                "attributes": attrs,
            }
        )

    redaction: dict[str, Any] = dict(counts)
    if truncate_to_fit(holder):
        redaction["truncated"] = True
    if spans_dropped:
        redaction["spans_dropped"] = spans_dropped

    payload: dict[str, Any] = {
        "id": record.id[:128],
        "model": record.model[:200],
        "latency_ms": max(0, record.latency_ms),
        "started_at": iso(record.started_at),
        "status": record.status,
        "input": holder[0],
        "output": holder[1],
        "spans": span_payloads,
        "redaction": redaction,
    }
    if record.prompt is not None:
        payload["prompt"] = {"name": record.prompt[0][:200], "version": record.prompt[1]}
    if record.input_tokens is not None:
        payload["input_tokens"] = record.input_tokens
    if record.output_tokens is not None:
        payload["output_tokens"] = record.output_tokens
    if record.cost_usd is not None:
        payload["cost_usd"] = (
            record.cost_usd if isinstance(record.cost_usd, int | float) else str(record.cost_usd)
        )
    return payload
