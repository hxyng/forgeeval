"""Trace ingestion and read endpoints.

Ingest is idempotent per (project, trace id): the SDK sends its own trace id, which becomes
``traces.idempotency_key``, so retrying a batch after a timeout never double-counts.
Validation is per item: one bad trace is reported in ``rejected`` without failing the batch.
"""

import base64
import json
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import AwareDatetime, BaseModel, Field, ValidationError
from sqlalchemy import and_, or_, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from forgeeval.auth import AuthContext, Scope, require
from forgeeval.db.base import uuid7
from forgeeval.db.models import PromptVersion, Span, SpanKind, Trace, TraceStatus
from forgeeval.db.session import get_session

router = APIRouter(prefix="/v1/traces", tags=["traces"])

MAX_BATCH = 500
MAX_SPANS = 500
MAX_PAYLOAD_BYTES = 256 * 1024

DbSession = Annotated[Session, Depends(get_session)]


# ---------- contract ----------


class PromptRef(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    version: int = Field(ge=1)


class SpanIn(BaseModel):
    id: str = Field(min_length=1, max_length=128, description="Client span id, unique per trace")
    parent_id: str | None = Field(None, description="Client id of an earlier span in this trace")
    kind: SpanKind
    name: str = Field(min_length=1, max_length=200)
    started_at: AwareDatetime
    duration_ms: int = Field(ge=0)
    attributes: dict[str, Any] = Field(default_factory=dict)


class TraceIn(BaseModel):
    id: str = Field(min_length=1, max_length=128, description="Client trace id; idempotency key")
    model: str = Field(min_length=1, max_length=200, examples=["openai/gpt-5-mini"])
    prompt: PromptRef | None = None
    input: Any = None
    output: Any = None
    status: TraceStatus = TraceStatus.ok
    latency_ms: int = Field(ge=0)
    input_tokens: int | None = Field(None, ge=0)
    output_tokens: int | None = Field(None, ge=0)
    cost_usd: Decimal | None = Field(None, ge=0, max_digits=12, decimal_places=6)
    started_at: AwareDatetime
    otel_trace_id: str | None = Field(None, max_length=64)
    redaction: dict[str, Any] = Field(
        default_factory=dict, description="Which redaction rules the SDK applied, with counts"
    )
    spans: list[SpanIn] = Field(default_factory=list, max_length=MAX_SPANS)


class TraceBatchIn(BaseModel):
    traces: list[dict[str, Any]] = Field(
        min_length=1,
        max_length=MAX_BATCH,
        description="Each item must match TraceIn; invalid items are rejected individually.",
        json_schema_extra={"items": {"$ref": "#/components/schemas/TraceIn"}},
    )


class Rejected(BaseModel):
    index: int
    id: str | None
    error: str


class TraceBatchOut(BaseModel):
    accepted: int
    duplicates: int
    rejected: list[Rejected]
    unresolved_prompts: list[PromptRef] = Field(
        description="Prompt refs with no registered version; those traces were stored unlinked."
    )


class SpanOut(BaseModel):
    id: uuid.UUID
    parent_span_id: uuid.UUID | None
    kind: SpanKind
    name: str
    started_at: datetime
    duration_ms: int
    attributes: dict[str, Any]


class TraceOut(BaseModel):
    id: uuid.UUID
    client_id: str
    model: str
    prompt_version_id: uuid.UUID | None
    status: TraceStatus
    latency_ms: int
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: Decimal | None
    started_at: datetime
    ingested_at: datetime


class TraceDetailOut(TraceOut):
    input: Any
    output: Any
    redaction: dict[str, Any]
    otel_trace_id: str | None
    spans: list[SpanOut]


class TracePage(BaseModel):
    items: list[TraceOut]
    next_cursor: str | None


# ---------- helpers ----------


def _validate_item(raw: dict[str, Any]) -> TraceIn:
    item = TraceIn.model_validate(raw)
    size = len(json.dumps([item.input, item.output], default=str).encode())
    if size > MAX_PAYLOAD_BYTES:
        raise ValueError(f"input+output is {size} bytes; limit is {MAX_PAYLOAD_BYTES}")
    seen: set[str] = set()
    for span in item.spans:
        if span.id in seen:
            raise ValueError(f"duplicate span id {span.id!r}")
        if span.parent_id is not None and span.parent_id not in seen:
            raise ValueError(f"span {span.id!r} references unknown or later parent")
        seen.add(span.id)
    return item


def _error_text(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        first = exc.errors()[0]
        loc = ".".join(str(p) for p in first["loc"])
        return f"{loc}: {first['msg']}" if loc else first["msg"]
    return str(exc)


def _to_out(t: Trace) -> TraceOut:
    return TraceOut(
        id=t.id,
        client_id=t.idempotency_key,
        model=t.model,
        prompt_version_id=t.prompt_version_id,
        status=t.status,
        latency_ms=t.latency_ms,
        input_tokens=t.input_tokens,
        output_tokens=t.output_tokens,
        cost_usd=t.cost_usd,
        started_at=t.started_at,
        ingested_at=t.ingested_at,
    )


def _encode_cursor(t: Trace) -> str:
    raw = f"{t.started_at.isoformat()}|{t.id}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        started, tid = base64.urlsafe_b64decode(cursor.encode()).decode().split("|")
        return datetime.fromisoformat(started), uuid.UUID(tid)
    except Exception as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid cursor.") from exc


# ---------- routes ----------


@router.post(":batch", status_code=status.HTTP_202_ACCEPTED)
def ingest_batch(
    body: TraceBatchIn,
    db: DbSession,
    ctx: Annotated[AuthContext, require(Scope.traces_write)],
) -> TraceBatchOut:
    """Store up to 500 traces. Re-sending a trace id already stored counts as a duplicate."""
    rejected: list[Rejected] = []
    valid: list[TraceIn] = []
    seen_ids: set[str] = set()
    in_batch_dupes = 0
    for index, raw in enumerate(body.traces):
        try:
            item = _validate_item(raw)
        except (ValidationError, ValueError) as exc:
            client_id = raw.get("id") if isinstance(raw.get("id"), str) else None
            rejected.append(Rejected(index=index, id=client_id, error=_error_text(exc)))
            continue
        if item.id in seen_ids:
            in_batch_dupes += 1
            continue
        seen_ids.add(item.id)
        valid.append(item)

    refs = {(t.prompt.name, t.prompt.version) for t in valid if t.prompt}
    prompt_ids: dict[tuple[str, int], uuid.UUID] = {}
    if refs:
        rows = db.execute(
            select(PromptVersion.name, PromptVersion.version, PromptVersion.id).where(
                PromptVersion.project_id == ctx.project_id,
                tuple_(PromptVersion.name, PromptVersion.version).in_(list(refs)),
            )
        )
        prompt_ids = {(n, v): pid for n, v, pid in rows}
    unresolved = sorted(refs - prompt_ids.keys())

    inserted: dict[str, uuid.UUID] = {}
    if valid:
        rows_in = [
            {
                "id": uuid7(),
                "project_id": ctx.project_id,
                "idempotency_key": t.id,
                "prompt_version_id": prompt_ids.get((t.prompt.name, t.prompt.version))
                if t.prompt
                else None,
                "model": t.model,
                "input": t.input,
                "output": t.output,
                "status": t.status,
                "latency_ms": t.latency_ms,
                "input_tokens": t.input_tokens,
                "output_tokens": t.output_tokens,
                "cost_usd": t.cost_usd,
                "redaction": t.redaction,
                "otel_trace_id": t.otel_trace_id,
                "started_at": t.started_at,
            }
            for t in valid
        ]
        stmt = (
            insert(Trace)
            .values(rows_in)
            .on_conflict_do_nothing(index_elements=["project_id", "idempotency_key"])
            .returning(Trace.idempotency_key, Trace.id)
        )
        inserted = {key: tid for key, tid in db.execute(stmt)}

        span_rows: list[dict[str, Any]] = []
        for t in valid:
            trace_id = inserted.get(t.id)
            if trace_id is None or not t.spans:
                continue
            ids = {s.id: uuid7() for s in t.spans}
            for s in t.spans:
                span_rows.append(
                    {
                        "id": ids[s.id],
                        "trace_id": trace_id,
                        "parent_span_id": ids[s.parent_id] if s.parent_id else None,
                        "kind": s.kind,
                        "name": s.name,
                        "started_at": s.started_at,
                        "duration_ms": s.duration_ms,
                        "attributes": {**s.attributes, "client_span_id": s.id},
                    }
                )
        if span_rows:
            db.execute(insert(Span), span_rows)
        db.commit()

    return TraceBatchOut(
        accepted=len(inserted),
        duplicates=len(valid) - len(inserted) + in_batch_dupes,
        rejected=rejected,
        unresolved_prompts=[PromptRef(name=n, version=v) for n, v in unresolved],
    )


@router.get("")
def list_traces(
    db: DbSession,
    ctx: Annotated[AuthContext, require(Scope.project_read)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: str | None = None,
    status_filter: Annotated[Literal["ok", "error"] | None, Query(alias="status")] = None,
    model: str | None = None,
) -> TracePage:
    """Newest first, keyset-paginated on (started_at, id)."""
    q = select(Trace).where(Trace.project_id == ctx.project_id)
    if status_filter:
        q = q.where(Trace.status == TraceStatus(status_filter))
    if model:
        q = q.where(Trace.model == model)
    if cursor:
        started, tid = _decode_cursor(cursor)
        q = q.where(
            or_(Trace.started_at < started, and_(Trace.started_at == started, Trace.id < tid))
        )
    rows = list(db.scalars(q.order_by(Trace.started_at.desc(), Trace.id.desc()).limit(limit + 1)))
    page, more = rows[:limit], len(rows) > limit
    return TracePage(
        items=[_to_out(t) for t in page], next_cursor=_encode_cursor(page[-1]) if more else None
    )


@router.get("/{trace_id}")
def get_trace(
    trace_id: uuid.UUID,
    db: DbSession,
    ctx: Annotated[AuthContext, require(Scope.project_read)],
) -> TraceDetailOut:
    trace = db.scalar(select(Trace).where(Trace.id == trace_id, Trace.project_id == ctx.project_id))
    if trace is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Trace not found.")
    spans = db.scalars(select(Span).where(Span.trace_id == trace.id).order_by(Span.started_at))
    return TraceDetailOut(
        **_to_out(trace).model_dump(),
        input=trace.input,
        output=trace.output,
        redaction=trace.redaction,
        otel_trace_id=trace.otel_trace_id,
        spans=[SpanOut.model_validate(s, from_attributes=True) for s in spans],
    )
