"""Evaluation runs: create (fan out one job per dataset item), inspect progress, cancel.

Creation is one transaction: freeze the dataset version, insert the run, its evaluators and one
``run_items`` row per dataset item. Jobs are enqueued only after commit, so a worker can never
receive an id that isn't in the database yet. If enqueueing fails after commit, the items stay
``queued`` and the worker's reaper re-enqueues them; the run is never lost.
"""

import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, insert, select, update
from sqlalchemy.orm import Session

from forgeeval.auth import AuthContext, Scope, require
from forgeeval.db.base import uuid7
from forgeeval.db.models import (
    Dataset,
    DatasetItem,
    DatasetVersion,
    EvalRun,
    Evaluator,
    EvaluatorVersion,
    ItemStatus,
    PromptVersion,
    RunEvaluator,
    RunItem,
    RunStatus,
    RunTrigger,
)
from forgeeval.db.session import get_session
from forgeeval.queue import JobQueue, get_queue

log = logging.getLogger("forgeeval.api.runs")
router = APIRouter(prefix="/v1/runs", tags=["runs"])

DbSession = Annotated[Session, Depends(get_session)]
Queue = Annotated[JobQueue, Depends(get_queue)]
ENQUEUE_CHUNK = 500
TERMINAL = {RunStatus.succeeded, RunStatus.partial, RunStatus.failed, RunStatus.cancelled}


class VersionRef(BaseModel):
    name: str
    version: int | None = Field(None, ge=1, description="Omit for the latest version")


class RunIn(BaseModel):
    dataset: VersionRef
    prompt: VersionRef
    evaluators: list[VersionRef] = Field(min_length=1, max_length=20)
    baseline_run_id: uuid.UUID | None = Field(
        None, description="Earlier run over the same dataset version to compare against"
    )
    trigger: RunTrigger = RunTrigger.manual
    git_sha: str | None = Field(None, max_length=64)
    max_failure_ratio: Decimal = Field(Decimal("0.02"), ge=0, le=1, decimal_places=3)


class RunEvaluatorOut(BaseModel):
    name: str
    version: int
    evaluator_version_id: uuid.UUID


class Progress(BaseModel):
    queued: int = 0
    leased: int = 0
    retry_wait: int = 0
    succeeded: int = 0
    dead_lettered: int = 0
    cancelled: int = 0


class RunOut(BaseModel):
    id: uuid.UUID
    status: RunStatus
    trigger: RunTrigger
    dataset: str
    dataset_version: int
    prompt: str
    prompt_version: int
    evaluators: list[RunEvaluatorOut]
    baseline_run_id: uuid.UUID | None
    total_items: int
    progress: Progress
    cost_usd: Decimal
    git_sha: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class RunPage(BaseModel):
    items: list[RunOut]
    next_cursor: uuid.UUID | None


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail)


def _resolve_dataset_version(db: Session, project_id: uuid.UUID, ref: VersionRef) -> DatasetVersion:
    q = (
        select(DatasetVersion)
        .join(Dataset, Dataset.id == DatasetVersion.dataset_id)
        .where(Dataset.project_id == project_id, Dataset.name == ref.name)
    )
    q = q.where(DatasetVersion.version == ref.version) if ref.version else q
    dv = db.scalar(q.order_by(DatasetVersion.version.desc()).limit(1))
    if dv is None:
        raise _unprocessable(f"Dataset '{ref.name}' version {ref.version or 'latest'} not found.")
    return dv


def _resolve_prompt_version(db: Session, project_id: uuid.UUID, ref: VersionRef) -> PromptVersion:
    q = select(PromptVersion).where(
        PromptVersion.project_id == project_id, PromptVersion.name == ref.name
    )
    q = q.where(PromptVersion.version == ref.version) if ref.version else q
    pv = db.scalar(q.order_by(PromptVersion.version.desc()).limit(1))
    if pv is None:
        raise _unprocessable(f"Prompt '{ref.name}' version {ref.version or 'latest'} not found.")
    return pv


def _resolve_evaluator_version(
    db: Session, project_id: uuid.UUID, ref: VersionRef
) -> EvaluatorVersion:
    q = (
        select(EvaluatorVersion)
        .join(Evaluator, Evaluator.id == EvaluatorVersion.evaluator_id)
        .where(Evaluator.project_id == project_id, Evaluator.name == ref.name)
    )
    q = q.where(EvaluatorVersion.version == ref.version) if ref.version else q
    ev = db.scalar(q.order_by(EvaluatorVersion.version.desc()).limit(1))
    if ev is None:
        raise _unprocessable(f"Evaluator '{ref.name}' version {ref.version or 'latest'} not found.")
    return ev


def _run_out(db: Session, run: EvalRun) -> RunOut:
    dataset, dv = db.execute(
        select(Dataset.name, DatasetVersion.version)
        .join(DatasetVersion, DatasetVersion.dataset_id == Dataset.id)
        .where(DatasetVersion.id == run.dataset_version_id)
    ).one()
    pv = db.get_one(PromptVersion, run.prompt_version_id)
    evaluators = [
        RunEvaluatorOut(name=name, version=version, evaluator_version_id=evid)
        for name, version, evid in db.execute(
            select(Evaluator.name, EvaluatorVersion.version, EvaluatorVersion.id)
            .join(Evaluator, Evaluator.id == EvaluatorVersion.evaluator_id)
            .join(RunEvaluator, RunEvaluator.evaluator_version_id == EvaluatorVersion.id)
            .where(RunEvaluator.run_id == run.id)
            .order_by(Evaluator.name)
        )
    ]
    counts = dict(
        db.execute(
            select(RunItem.status, func.count())
            .where(RunItem.run_id == run.id)
            .group_by(RunItem.status)
        ).all()
    )
    progress = Progress(**{s.value: counts.get(s, 0) for s in ItemStatus})
    return RunOut(
        id=run.id,
        status=run.status,
        trigger=run.trigger,
        dataset=dataset,
        dataset_version=dv,
        prompt=pv.name,
        prompt_version=pv.version,
        evaluators=evaluators,
        baseline_run_id=run.baseline_run_id,
        total_items=run.total_items,
        progress=progress,
        cost_usd=run.cost_usd,
        git_sha=run.git_sha,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )


def _get_run(db: Session, project_id: uuid.UUID, run_id: uuid.UUID) -> EvalRun:
    run = db.scalar(select(EvalRun).where(EvalRun.id == run_id, EvalRun.project_id == project_id))
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found.")
    return run


@router.post("", status_code=status.HTTP_201_CREATED)
def create_run(
    body: RunIn,
    db: DbSession,
    queue: Queue,
    ctx: Annotated[AuthContext, require(Scope.runs_write)],
) -> RunOut:
    dv = _resolve_dataset_version(db, ctx.project_id, body.dataset)
    pv = _resolve_prompt_version(db, ctx.project_id, body.prompt)
    evs = {
        ev.id: ev
        for ev in (_resolve_evaluator_version(db, ctx.project_id, r) for r in body.evaluators)
    }
    if len(evs) != len(body.evaluators):
        raise _unprocessable("The same evaluator version is listed twice.")
    if dv.item_count == 0:
        raise _unprocessable("Dataset version has no items.")
    if body.baseline_run_id is not None:
        baseline = db.scalar(
            select(EvalRun).where(
                EvalRun.id == body.baseline_run_id, EvalRun.project_id == ctx.project_id
            )
        )
        if baseline is None:
            raise _unprocessable("Baseline run not found.")
        if baseline.dataset_version_id != dv.id:
            raise _unprocessable(
                "Baseline run used a different dataset version; results would not be comparable."
            )

    # Freeze first: from here on the items this run reads can never change underneath it.
    db.execute(
        update(DatasetVersion)
        .where(DatasetVersion.id == dv.id, DatasetVersion.frozen_at.is_(None))
        .values(frozen_at=func.now())
    )
    run = EvalRun(
        id=uuid7(),
        project_id=ctx.project_id,
        dataset_version_id=dv.id,
        prompt_version_id=pv.id,
        baseline_run_id=body.baseline_run_id,
        trigger=body.trigger,
        max_failure_ratio=body.max_failure_ratio,
        total_items=dv.item_count,
        git_sha=body.git_sha,
    )
    db.add(run)
    db.flush()
    db.add_all(RunEvaluator(run_id=run.id, evaluator_version_id=evid) for evid in evs)
    item_ids = [uuid7() for _ in range(dv.item_count)]
    dataset_items = db.scalars(
        select(DatasetItem.id)
        .where(DatasetItem.dataset_version_id == dv.id)
        .order_by(DatasetItem.id)
    ).all()
    db.execute(
        insert(RunItem),
        [
            {"id": rid, "run_id": run.id, "dataset_item_id": did}
            for rid, did in zip(item_ids, dataset_items, strict=True)
        ],
    )
    db.commit()

    try:
        for i in range(0, len(item_ids), ENQUEUE_CHUNK):
            chunk = item_ids[i : i + ENQUEUE_CHUNK]
            queue.enqueue(chunk)
            db.execute(update(RunItem).where(RunItem.id.in_(chunk)).values(enqueued_at=func.now()))
            db.commit()
    except Exception:
        # Items stay 'queued' in Postgres; the worker reaper re-enqueues them.
        log.exception("enqueue failed for run %s; reaper will recover it", run.id)
    return _run_out(db, run)


@router.get("")
def list_runs(
    db: DbSession,
    ctx: Annotated[AuthContext, require(Scope.project_read)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: uuid.UUID | None = None,
) -> RunPage:
    """Newest first. Run ids are UUIDv7, so id order is creation order."""
    q = select(EvalRun).where(EvalRun.project_id == ctx.project_id)
    if cursor:
        q = q.where(EvalRun.id < cursor)
    rows = list(db.scalars(q.order_by(EvalRun.id.desc()).limit(limit + 1)))
    page = rows[:limit]
    return RunPage(
        items=[_run_out(db, r) for r in page],
        next_cursor=page[-1].id if len(rows) > limit else None,
    )


@router.get("/{run_id}")
def get_run(
    run_id: uuid.UUID,
    db: DbSession,
    ctx: Annotated[AuthContext, require(Scope.project_read)],
) -> RunOut:
    return _run_out(db, _get_run(db, ctx.project_id, run_id))


@router.post("/{run_id}/cancel")
def cancel_run(
    run_id: uuid.UUID,
    db: DbSession,
    ctx: Annotated[AuthContext, require(Scope.runs_write)],
) -> RunOut:
    """Stop scheduling new work. Leased items finish; queued and waiting ones are skipped."""
    run = _get_run(db, ctx.project_id, run_id)
    if run.status in TERMINAL:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Run is already {run.status.value}.")
    db.execute(
        update(RunItem)
        .where(
            RunItem.run_id == run.id,
            RunItem.status.in_([ItemStatus.queued, ItemStatus.retry_wait]),
        )
        .values(status=ItemStatus.cancelled)
    )
    run.status = RunStatus.cancelled
    run.finished_at = datetime.now(UTC)
    db.commit()
    return _run_out(db, run)
