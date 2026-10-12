"""Evaluation registry: versioned prompts, datasets and evaluators.

Every change creates a new immutable version. Re-submitting content identical to the latest
version returns that version (200) instead of minting a duplicate (201), so CI can register
its prompt on every build without inflating version numbers.
"""

import hashlib
import json
import uuid
from datetime import datetime
from typing import Annotated, Any, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response, status
from pydantic import BaseModel, Field, ValidationError, model_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from forgeeval.api.traces import load_payload
from forgeeval.auth import AuthContext, Scope, require
from forgeeval.db.base import Base
from forgeeval.db.models import (
    Dataset,
    DatasetItem,
    DatasetVersion,
    Evaluator,
    EvaluatorKind,
    EvaluatorVersion,
    PromptVersion,
    Trace,
)
from forgeeval.db.session import get_session
from forgeeval.evaluator_configs import dump_config, parse_config
from forgeeval.storage import PayloadStore, get_store

router = APIRouter(prefix="/v1", tags=["registry"])

DbSession = Annotated[Session, Depends(get_session)]
Writer = Annotated[AuthContext, require(Scope.registry_write)]
Reader = Annotated[AuthContext, require(Scope.project_read)]
NAME_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,99}$"
Name = Annotated[str, Path(pattern=NAME_PATTERN)]
MAX_ITEMS = 5000

M = TypeVar("M", bound=Base)


def content_hash(value: Any) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _not_found(what: str) -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, f"{what} not found.")


def _commit_new_version(db: Session) -> None:
    """Two writers racing for the same version number lose on the unique constraint."""
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "A concurrent request created this version; retry."
        ) from exc


# ---------- prompts ----------


class PromptVersionIn(BaseModel):
    template: str = Field(min_length=1)
    model: str = Field(min_length=1, max_length=200, examples=["openai/gpt-5-mini"])
    params: dict[str, Any] = Field(default_factory=dict, examples=[{"temperature": 0.2}])
    git_sha: str | None = Field(None, max_length=64)


class PromptVersionOut(BaseModel):
    id: uuid.UUID
    name: str
    version: int
    template: str
    model: str
    params: dict[str, Any]
    content_hash: str
    git_sha: str | None
    created_at: datetime


class PromptSummary(BaseModel):
    name: str
    latest_version: int
    versions: int


@router.post("/prompts/{name}/versions", status_code=status.HTTP_201_CREATED)
def create_prompt_version(
    name: Name, body: PromptVersionIn, db: DbSession, ctx: Writer, response: Response
) -> PromptVersionOut:
    digest = content_hash({"template": body.template, "model": body.model, "params": body.params})
    latest = db.scalar(
        select(PromptVersion)
        .where(PromptVersion.project_id == ctx.project_id, PromptVersion.name == name)
        .order_by(PromptVersion.version.desc())
        .limit(1)
    )
    if latest is not None and latest.content_hash == digest:
        response.status_code = status.HTTP_200_OK
        return PromptVersionOut.model_validate(latest, from_attributes=True)
    pv = PromptVersion(
        project_id=ctx.project_id,
        name=name,
        version=(latest.version + 1) if latest else 1,
        template=body.template,
        model=body.model,
        params=body.params,
        content_hash=digest,
        git_sha=body.git_sha,
    )
    db.add(pv)
    _commit_new_version(db)
    return PromptVersionOut.model_validate(pv, from_attributes=True)


@router.get("/prompts")
def list_prompts(db: DbSession, ctx: Reader) -> list[PromptSummary]:
    rows = db.execute(
        select(PromptVersion.name, func.max(PromptVersion.version), func.count())
        .where(PromptVersion.project_id == ctx.project_id)
        .group_by(PromptVersion.name)
        .order_by(PromptVersion.name)
    )
    return [PromptSummary(name=n, latest_version=v, versions=c) for n, v, c in rows]


@router.get("/prompts/{name}/versions")
def list_prompt_versions(name: Name, db: DbSession, ctx: Reader) -> list[PromptVersionOut]:
    rows = db.scalars(
        select(PromptVersion)
        .where(PromptVersion.project_id == ctx.project_id, PromptVersion.name == name)
        .order_by(PromptVersion.version.desc())
    )
    return [PromptVersionOut.model_validate(r, from_attributes=True) for r in rows]


# ---------- datasets ----------


class DatasetIn(BaseModel):
    name: str = Field(pattern=NAME_PATTERN)
    description: str | None = Field(None, max_length=2000)


class DatasetOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    latest_version: int | None
    created_at: datetime


class ItemIn(BaseModel):
    input: Any
    expected_output: Any = None
    context: dict[str, Any] | None = Field(
        None, description="Ground truth for retrieval metrics, e.g. relevant_doc_ids"
    )
    tags: list[str] = Field(default_factory=list, max_length=20)


class FromTraces(BaseModel):
    trace_ids: list[uuid.UUID] = Field(min_length=1, max_length=MAX_ITEMS)
    use_output_as_expected: bool = Field(
        False, description="Treat the production output as the reference answer"
    )
    tags: list[str] = Field(default_factory=list, max_length=20)


class DatasetVersionIn(BaseModel):
    items: list[ItemIn] = Field(default_factory=list, max_length=MAX_ITEMS)
    from_traces: FromTraces | None = None

    @model_validator(mode="after")
    def _has_items(self) -> "DatasetVersionIn":
        if not self.items and self.from_traces is None:
            raise ValueError("provide items, from_traces, or both")
        n = len(self.items) + (len(self.from_traces.trace_ids) if self.from_traces else 0)
        if n > MAX_ITEMS:
            raise ValueError(f"a version holds at most {MAX_ITEMS} items")
        return self


class DatasetVersionOut(BaseModel):
    id: uuid.UUID
    dataset: str
    version: int
    item_count: int
    content_hash: str
    frozen_at: datetime | None
    created_at: datetime


class ItemOut(BaseModel):
    id: uuid.UUID
    input: Any
    expected_output: Any
    context: dict[str, Any] | None
    tags: list[str]
    source_trace_id: uuid.UUID | None


class ItemPage(BaseModel):
    items: list[ItemOut]
    next_cursor: uuid.UUID | None


def _dataset(db: Session, ctx: AuthContext, name: str) -> Dataset:
    ds = db.scalar(
        select(Dataset).where(Dataset.project_id == ctx.project_id, Dataset.name == name)
    )
    if ds is None:
        raise _not_found(f"Dataset '{name}'")
    return ds


def _version_out(dv: DatasetVersion, name: str) -> DatasetVersionOut:
    return DatasetVersionOut(
        id=dv.id,
        dataset=name,
        version=dv.version,
        item_count=dv.item_count,
        content_hash=dv.content_hash,
        frozen_at=dv.frozen_at,
        created_at=dv.created_at,
    )


@router.post("/datasets", status_code=status.HTTP_201_CREATED)
def create_dataset(body: DatasetIn, db: DbSession, ctx: Writer) -> DatasetOut:
    ds = Dataset(project_id=ctx.project_id, name=body.name, description=body.description)
    db.add(ds)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, f"Dataset '{body.name}' exists.") from exc
    return DatasetOut(
        id=ds.id,
        name=ds.name,
        description=ds.description,
        latest_version=None,
        created_at=ds.created_at,
    )


@router.get("/datasets")
def list_datasets(db: DbSession, ctx: Reader) -> list[DatasetOut]:
    latest = (
        select(DatasetVersion.dataset_id, func.max(DatasetVersion.version).label("v"))
        .group_by(DatasetVersion.dataset_id)
        .subquery()
    )
    rows = db.execute(
        select(Dataset, latest.c.v)
        .outerjoin(latest, latest.c.dataset_id == Dataset.id)
        .where(Dataset.project_id == ctx.project_id)
        .order_by(Dataset.name)
    )
    return [
        DatasetOut(
            id=d.id,
            name=d.name,
            description=d.description,
            latest_version=v,
            created_at=d.created_at,
        )
        for d, v in rows
    ]


@router.post("/datasets/{name}/versions", status_code=status.HTTP_201_CREATED)
def create_dataset_version(
    name: Name,
    body: DatasetVersionIn,
    db: DbSession,
    ctx: Writer,
    response: Response,
    store: Annotated[PayloadStore | None, Depends(get_store)],
) -> DatasetVersionOut:
    """Snapshot items into a new version. Items can be given inline or promoted from traces."""
    ds = _dataset(db, ctx, name)
    rows: list[dict[str, Any]] = [
        {**item.model_dump(), "source_trace_id": None} for item in body.items
    ]
    if body.from_traces:
        wanted = body.from_traces.trace_ids
        found = {
            t.id: t
            for t in db.scalars(
                select(Trace).where(Trace.project_id == ctx.project_id, Trace.id.in_(wanted))
            )
        }
        missing = [str(t) for t in wanted if t not in found]
        if missing:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, f"Unknown trace ids: {missing[:10]}"
            )
        for tid in wanted:
            t = found[tid]
            payload = load_payload(t, store)
            rows.append(
                {
                    "input": payload["input"],
                    "expected_output": payload["output"]
                    if body.from_traces.use_output_as_expected
                    else None,
                    "context": None,
                    "tags": body.from_traces.tags,
                    "source_trace_id": t.id,
                }
            )

    digest = content_hash([{k: v for k, v in r.items() if k != "source_trace_id"} for r in rows])
    latest = db.scalar(
        select(DatasetVersion)
        .where(DatasetVersion.dataset_id == ds.id)
        .order_by(DatasetVersion.version.desc())
        .limit(1)
    )
    if latest is not None and latest.content_hash == digest:
        response.status_code = status.HTTP_200_OK
        return _version_out(latest, name)

    dv = DatasetVersion(
        dataset_id=ds.id,
        version=(latest.version + 1) if latest else 1,
        item_count=len(rows),
        content_hash=digest,
    )
    db.add(dv)
    db.flush()
    db.add_all(DatasetItem(dataset_version_id=dv.id, **r) for r in rows)
    _commit_new_version(db)
    return _version_out(dv, name)


@router.get("/datasets/{name}/versions")
def list_dataset_versions(name: Name, db: DbSession, ctx: Reader) -> list[DatasetVersionOut]:
    ds = _dataset(db, ctx, name)
    rows = db.scalars(
        select(DatasetVersion)
        .where(DatasetVersion.dataset_id == ds.id)
        .order_by(DatasetVersion.version.desc())
    )
    return [_version_out(dv, name) for dv in rows]


@router.get("/datasets/{name}/versions/{version}/items")
def list_dataset_items(
    name: Name,
    version: int,
    db: DbSession,
    ctx: Reader,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    cursor: uuid.UUID | None = None,
) -> ItemPage:
    ds = _dataset(db, ctx, name)
    dv = db.scalar(
        select(DatasetVersion).where(
            DatasetVersion.dataset_id == ds.id, DatasetVersion.version == version
        )
    )
    if dv is None:
        raise _not_found(f"Version {version} of dataset '{name}'")
    q = select(DatasetItem).where(DatasetItem.dataset_version_id == dv.id)
    if cursor:
        q = q.where(DatasetItem.id > cursor)
    rows = list(db.scalars(q.order_by(DatasetItem.id).limit(limit + 1)))
    page = rows[:limit]
    return ItemPage(
        items=[ItemOut.model_validate(r, from_attributes=True) for r in page],
        next_cursor=page[-1].id if len(rows) > limit else None,
    )


# ---------- evaluators ----------


class EvaluatorIn(BaseModel):
    name: str = Field(pattern=NAME_PATTERN)
    kind: EvaluatorKind
    higher_is_better: bool | None = Field(
        None, description="Defaults to false for latency and cost, true otherwise"
    )


class EvaluatorVersionIn(BaseModel):
    config: dict[str, Any] = Field(default_factory=dict)
    judge_model: str | None = Field(None, max_length=200)


class EvaluatorVersionOut(BaseModel):
    id: uuid.UUID
    evaluator: str
    kind: EvaluatorKind
    version: int
    config: dict[str, Any]
    judge_model: str | None
    config_hash: str
    created_at: datetime


class EvaluatorOut(BaseModel):
    id: uuid.UUID
    name: str
    kind: EvaluatorKind
    higher_is_better: bool
    latest_version: int | None


def _evaluator(db: Session, ctx: AuthContext, name: str) -> Evaluator:
    ev = db.scalar(
        select(Evaluator).where(Evaluator.project_id == ctx.project_id, Evaluator.name == name)
    )
    if ev is None:
        raise _not_found(f"Evaluator '{name}'")
    return ev


def _ev_version_out(v: EvaluatorVersion, ev: Evaluator) -> EvaluatorVersionOut:
    return EvaluatorVersionOut(
        id=v.id,
        evaluator=ev.name,
        kind=ev.kind,
        version=v.version,
        config=v.config,
        judge_model=v.judge_model,
        config_hash=v.config_hash,
        created_at=v.created_at,
    )


@router.post("/evaluators", status_code=status.HTTP_201_CREATED)
def create_evaluator(body: EvaluatorIn, db: DbSession, ctx: Writer) -> EvaluatorOut:
    higher = body.higher_is_better
    if higher is None:
        higher = body.kind not in (EvaluatorKind.latency, EvaluatorKind.cost)
    ev = Evaluator(
        project_id=ctx.project_id, name=body.name, kind=body.kind, higher_is_better=higher
    )
    db.add(ev)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, f"Evaluator '{body.name}' exists.") from exc
    return EvaluatorOut(
        id=ev.id,
        name=ev.name,
        kind=ev.kind,
        higher_is_better=ev.higher_is_better,
        latest_version=None,
    )


@router.get("/evaluators")
def list_evaluators(db: DbSession, ctx: Reader) -> list[EvaluatorOut]:
    latest = (
        select(EvaluatorVersion.evaluator_id, func.max(EvaluatorVersion.version).label("v"))
        .group_by(EvaluatorVersion.evaluator_id)
        .subquery()
    )
    rows = db.execute(
        select(Evaluator, latest.c.v)
        .outerjoin(latest, latest.c.evaluator_id == Evaluator.id)
        .where(Evaluator.project_id == ctx.project_id)
        .order_by(Evaluator.name)
    )
    return [
        EvaluatorOut(
            id=e.id,
            name=e.name,
            kind=e.kind,
            higher_is_better=e.higher_is_better,
            latest_version=v,
        )
        for e, v in rows
    ]


@router.post("/evaluators/{name}/versions", status_code=status.HTTP_201_CREATED)
def create_evaluator_version(
    name: Name, body: EvaluatorVersionIn, db: DbSession, ctx: Writer, response: Response
) -> EvaluatorVersionOut:
    ev = _evaluator(db, ctx, name)
    try:
        config = dump_config(parse_config(ev.kind.value, body.config))
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(p) for p in first["loc"][1:]) or "config"
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"Invalid {ev.kind.value} config: {loc}: {first['msg']}",
        ) from exc
    if ev.kind is EvaluatorKind.llm_rubric and not body.judge_model:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "llm_rubric evaluators require judge_model."
        )
    if ev.kind is not EvaluatorKind.llm_rubric and body.judge_model:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "judge_model applies only to llm_rubric."
        )

    digest = content_hash({"config": config, "judge_model": body.judge_model})
    latest = db.scalar(
        select(EvaluatorVersion)
        .where(EvaluatorVersion.evaluator_id == ev.id)
        .order_by(EvaluatorVersion.version.desc())
        .limit(1)
    )
    if latest is not None and latest.config_hash == digest:
        response.status_code = status.HTTP_200_OK
        return _ev_version_out(latest, ev)
    v = EvaluatorVersion(
        evaluator_id=ev.id,
        version=(latest.version + 1) if latest else 1,
        config=config,
        judge_model=body.judge_model,
        config_hash=digest,
    )
    db.add(v)
    _commit_new_version(db)
    return _ev_version_out(v, ev)


@router.get("/evaluators/{name}/versions")
def list_evaluator_versions(name: Name, db: DbSession, ctx: Reader) -> list[EvaluatorVersionOut]:
    ev = _evaluator(db, ctx, name)
    rows = db.scalars(
        select(EvaluatorVersion)
        .where(EvaluatorVersion.evaluator_id == ev.id)
        .order_by(EvaluatorVersion.version.desc())
    )
    return [_ev_version_out(v, ev) for v in rows]
