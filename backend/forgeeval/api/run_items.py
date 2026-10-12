"""Item-level results of a run: the test case, what the model produced, and every score.

This is what a reviewer reads to understand *why* a metric moved: filter to the items that failed
one evaluator and see input, expected output, actual output and the evaluator's rationale.
"""

import uuid
from collections import defaultdict
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import any_, exists, literal, select
from sqlalchemy.orm import Session

from forgeeval.api.compare import get_run
from forgeeval.auth import AuthContext, Scope, require
from forgeeval.db.models import (
    DatasetItem,
    Evaluator,
    EvaluatorVersion,
    ItemStatus,
    RunItem,
    Score,
)
from forgeeval.db.session import get_session

router = APIRouter(prefix="/v1/runs", tags=["runs"])

DbSession = Annotated[Session, Depends(get_session)]


class ItemScore(BaseModel):
    evaluator: str
    version: int
    value: Decimal
    passed: bool | None
    rationale: str | None


class RunItemOut(BaseModel):
    id: uuid.UUID
    dataset_item_id: uuid.UUID
    status: ItemStatus
    attempt: int
    input: Any
    expected_output: Any
    context: dict[str, Any] | None
    tags: list[str]
    output: Any
    latency_ms: int | None
    cost_usd: Decimal | None
    last_error: str | None
    scores: list[ItemScore]


class RunItemPage(BaseModel):
    items: list[RunItemOut]
    next_cursor: uuid.UUID | None


@router.get("/{run_id}/items")
def list_run_items(
    run_id: uuid.UUID,
    db: DbSession,
    ctx: Annotated[AuthContext, require(Scope.project_read)],
    status: ItemStatus | None = None,
    failed_evaluator: Annotated[
        str | None, Query(description="Only items this evaluator scored as not passed")
    ] = None,
    tag: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: uuid.UUID | None = None,
) -> RunItemPage:
    """Items in id order with their test case, output and scores."""
    run = get_run(db, ctx.project_id, run_id)
    q = (
        select(RunItem, DatasetItem)
        .join(DatasetItem, DatasetItem.id == RunItem.dataset_item_id)
        .where(RunItem.run_id == run.id)
    )
    if status is not None:
        q = q.where(RunItem.status == status)
    if tag is not None:
        q = q.where(literal(tag) == any_(DatasetItem.tags))
    if failed_evaluator is not None:
        q = q.where(
            exists()
            .where(
                Score.run_item_id == RunItem.id,
                Score.passed.is_(False),
                Score.evaluator_version_id == EvaluatorVersion.id,
                EvaluatorVersion.evaluator_id == Evaluator.id,
                Evaluator.name == failed_evaluator,
            )
            .correlate(RunItem)
        )
    if cursor is not None:
        q = q.where(RunItem.id > cursor)
    rows = db.execute(q.order_by(RunItem.id).limit(limit + 1)).all()
    page = rows[:limit]

    scores: dict[uuid.UUID, list[ItemScore]] = defaultdict(list)
    if page:
        for item_id, name, version, value, passed, rationale in db.execute(
            select(
                Score.run_item_id,
                Evaluator.name,
                EvaluatorVersion.version,
                Score.value,
                Score.passed,
                Score.rationale,
            )
            .join(EvaluatorVersion, EvaluatorVersion.id == Score.evaluator_version_id)
            .join(Evaluator, Evaluator.id == EvaluatorVersion.evaluator_id)
            .where(Score.run_item_id.in_([ri.id for ri, _ in page]))
            .order_by(Evaluator.name)
        ):
            scores[item_id].append(
                ItemScore(
                    evaluator=name,
                    version=version,
                    value=value,
                    passed=passed,
                    rationale=rationale,
                )
            )

    return RunItemPage(
        items=[
            RunItemOut(
                id=ri.id,
                dataset_item_id=di.id,
                status=ri.status,
                attempt=ri.attempt,
                input=di.input,
                expected_output=di.expected_output,
                context=di.context,
                tags=list(di.tags),
                output=ri.output,
                latency_ms=ri.latency_ms,
                cost_usd=ri.cost_usd,
                last_error=ri.last_error,
                scores=scores.get(ri.id, []),
            )
            for ri, di in page
        ],
        next_cursor=page[-1][0].id if len(rows) > limit else None,
    )
