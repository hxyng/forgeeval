"""Process one run item: claim, generate, score, then succeed, retry, or dead-letter.

Every state change is a conditional UPDATE, so the database, not the queue, decides who owns an
item. A duplicate delivery or a message for a cancelled run fails the claim and is a no-op. The
final write is fenced on ``leased_by``: a worker whose lease expired and was reassigned cannot
overwrite the new owner's result.
"""

import enum
import logging
import random
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import exists, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from forgeeval.config import get_settings
from forgeeval.db.models import (
    DatasetItem,
    DeadLetter,
    EvalRun,
    Evaluator,
    EvaluatorVersion,
    ItemStatus,
    PromptVersion,
    RunEvaluator,
    RunItem,
    RunStatus,
    Score,
)
from forgeeval.evaluator_configs import parse_config
from forgeeval.evaluators import REGISTRY, EvalCase
from forgeeval.models_client import ModelClient, PermanentError, TransientError, render

log = logging.getLogger("forgeeval.worker")

ACTIVE = (ItemStatus.queued, ItemStatus.leased, ItemStatus.retry_wait)
BACKOFF_BASE_SECONDS = 2.0
BACKOFF_CAP_SECONDS = 120.0


class Outcome(enum.StrEnum):
    skipped = "skipped"  # not claimable: duplicate delivery, not due yet, or cancelled
    succeeded = "succeeded"
    retrying = "retrying"
    dead_lettered = "dead_lettered"
    lost_lease = "lost_lease"


@dataclass(frozen=True)
class Claimed:
    id: uuid.UUID
    run_id: uuid.UUID
    dataset_item_id: uuid.UUID
    attempt: int


def retry_delay(attempt: int, rng: random.Random | None = None) -> float:
    """Full jitter on min(cap, base * 2^(attempt-1)); ``attempt`` counts from 1."""
    ceiling = min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * 2 ** max(0, attempt - 1))
    return (rng or random).uniform(0, ceiling)


def classify(exc: Exception) -> str:
    if isinstance(exc, PermanentError):
        return "permanent"
    if isinstance(exc, TransientError):
        return "transient"
    return "unknown"


def claim(db: Session, item_id: uuid.UUID, worker_id: str) -> Claimed | None:
    lease_until = datetime.now(UTC) + timedelta(seconds=get_settings().lease_seconds)
    row = db.execute(
        update(RunItem)
        .where(
            RunItem.id == item_id,
            RunItem.status.in_([ItemStatus.queued, ItemStatus.retry_wait]),
            (RunItem.next_attempt_at.is_(None)) | (RunItem.next_attempt_at <= func.now()),
            ~exists().where(EvalRun.id == RunItem.run_id, EvalRun.status == RunStatus.cancelled),
        )
        .values(
            status=ItemStatus.leased,
            attempt=RunItem.attempt + 1,
            leased_by=worker_id,
            lease_expires_at=lease_until,
        )
        .returning(RunItem.id, RunItem.run_id, RunItem.dataset_item_id, RunItem.attempt)
    ).one_or_none()
    if row is None:
        db.rollback()
        return None
    db.execute(
        update(EvalRun)
        .where(EvalRun.id == row.run_id, EvalRun.status == RunStatus.queued)
        .values(status=RunStatus.running, started_at=func.now())
    )
    db.commit()
    return Claimed(row.id, row.run_id, row.dataset_item_id, row.attempt)


def _execute(db: Session, item: Claimed, models: ModelClient) -> dict[str, Any]:
    """Generate the candidate output and score it. Pure reads; returns what to write."""
    run = db.get_one(EvalRun, item.run_id)
    prompt = db.get_one(PromptVersion, run.prompt_version_id)
    case_row = db.get_one(DatasetItem, item.dataset_item_id)

    gen = models.generate(
        prompt.model,
        render(prompt.template, case_row.input),
        dict(prompt.params),
        expected=case_row.expected_output,
    )
    case = EvalCase(
        item_input=case_row.input,
        expected_output=case_row.expected_output,
        context=case_row.context,
        output=gen.output,
        latency_ms=gen.latency_ms,
        cost_usd=gen.cost_usd,
        tags=list(case_row.tags),
    )
    evaluators = db.execute(
        select(EvaluatorVersion, Evaluator.kind)
        .join(Evaluator, Evaluator.id == EvaluatorVersion.evaluator_id)
        .join(RunEvaluator, RunEvaluator.evaluator_version_id == EvaluatorVersion.id)
        .where(RunEvaluator.run_id == item.run_id)
    ).all()
    scores: list[dict[str, Any]] = []
    for ev_version, kind in evaluators:
        impl = REGISTRY.get(kind)
        if impl is None:
            raise PermanentError(f"evaluator kind {kind.value!r} is not implemented")
        result = impl.score(
            parse_config(kind.value, ev_version.config), case, ev_version.judge_model, models
        )
        scores.append(
            {
                "run_item_id": item.id,
                "evaluator_version_id": ev_version.id,
                "value": result.value,
                "passed": result.passed,
                "rationale": result.rationale,
                "judge_cost_usd": result.judge_cost_usd,
            }
        )
    judge_cost = sum((s["judge_cost_usd"] or Decimal(0) for s in scores), Decimal(0))
    return {
        "output": gen.output,
        "latency_ms": gen.latency_ms,
        "cost_usd": (gen.cost_usd or Decimal(0)) + judge_cost,
        "scores": scores,
    }


def _finish_success(db: Session, item: Claimed, worker_id: str, result: dict[str, Any]) -> bool:
    updated = db.execute(
        update(RunItem)
        .where(
            RunItem.id == item.id,
            RunItem.status == ItemStatus.leased,
            RunItem.leased_by == worker_id,
        )
        .values(
            status=ItemStatus.succeeded,
            output=result["output"],
            latency_ms=result["latency_ms"],
            cost_usd=result["cost_usd"],
            last_error=None,
            lease_expires_at=None,
        )
        .returning(RunItem.id)
    ).one_or_none()
    if updated is None:
        db.rollback()
        return False
    if result["scores"]:
        stmt = insert(Score).values(result["scores"])
        db.execute(
            stmt.on_conflict_do_update(
                index_elements=["run_item_id", "evaluator_version_id"],
                set_={
                    "value": stmt.excluded.value,
                    "passed": stmt.excluded.passed,
                    "rationale": stmt.excluded.rationale,
                    "judge_cost_usd": stmt.excluded.judge_cost_usd,
                },
            )
        )
    db.execute(
        update(EvalRun)
        .where(EvalRun.id == item.run_id)
        .values(
            succeeded_items=EvalRun.succeeded_items + 1,
            cost_usd=EvalRun.cost_usd + result["cost_usd"],
        )
    )
    db.commit()
    return True


def dead_letter(
    db: Session, item_id: uuid.UUID, run_id: uuid.UUID, attempts: int, error_class: str, detail: str
) -> None:
    """Caller has already moved the item out of 'leased' (or holds it) in this transaction."""
    db.add(
        DeadLetter(
            run_item_id=item_id,
            error_class=error_class,
            error_detail=detail[:2000],
            attempts=attempts,
            payload={"run_item_id": str(item_id), "run_id": str(run_id)},
        )
    )
    db.execute(
        update(EvalRun).where(EvalRun.id == run_id).values(failed_items=EvalRun.failed_items + 1)
    )


def _finish_failure(db: Session, item: Claimed, worker_id: str, exc: Exception) -> Outcome:
    kind = classify(exc)
    detail = f"{exc.__class__.__name__}: {exc}"
    give_up = kind == "permanent" or item.attempt >= get_settings().max_attempts
    values: dict[str, Any] = {"last_error": detail[:2000], "lease_expires_at": None}
    if give_up:
        values["status"] = ItemStatus.dead_lettered
    else:
        values["status"] = ItemStatus.retry_wait
        values["next_attempt_at"] = datetime.now(UTC) + timedelta(seconds=retry_delay(item.attempt))
    updated = db.execute(
        update(RunItem)
        .where(
            RunItem.id == item.id,
            RunItem.status == ItemStatus.leased,
            RunItem.leased_by == worker_id,
        )
        .values(**values)
        .returning(RunItem.id)
    ).one_or_none()
    if updated is None:
        db.rollback()
        return Outcome.lost_lease
    if give_up:
        dead_letter(db, item.id, item.run_id, item.attempt, kind, detail)
    db.commit()
    return Outcome.dead_lettered if give_up else Outcome.retrying


def finalize_run(db: Session, run_id: uuid.UUID) -> RunStatus | None:
    """If no item is still active, set the run's terminal status. Safe to call repeatedly."""
    run = db.get(EvalRun, run_id)
    if run is None or run.status not in (RunStatus.queued, RunStatus.running):
        return None
    still_active = db.scalar(
        select(exists().where(RunItem.run_id == run_id, RunItem.status.in_(ACTIVE)))
    )
    if still_active:
        return None
    failed_ratio = Decimal(run.failed_items) / max(run.total_items, 1)
    if run.failed_items == 0:
        final = RunStatus.succeeded
    elif failed_ratio <= run.max_failure_ratio:
        final = RunStatus.partial
    else:
        final = RunStatus.failed
    done = db.execute(
        update(EvalRun)
        .where(EvalRun.id == run_id, EvalRun.status.in_([RunStatus.queued, RunStatus.running]))
        .values(status=final, finished_at=func.now())
        .returning(EvalRun.id)
    ).one_or_none()
    db.commit()
    return final if done else None


def process(db: Session, item_id: uuid.UUID, worker_id: str, models: ModelClient) -> Outcome:
    item = claim(db, item_id, worker_id)
    if item is None:
        return Outcome.skipped
    try:
        result = _execute(db, item, models)
    except Exception as exc:
        db.rollback()
        if classify(exc) == "unknown":
            log.exception("unexpected error on run item %s (attempt %d)", item.id, item.attempt)
        else:
            log.warning("run item %s attempt %d failed: %s", item.id, item.attempt, exc)
        outcome = _finish_failure(db, item, worker_id, exc)
    else:
        outcome = (
            Outcome.succeeded
            if _finish_success(db, item, worker_id, result)
            else Outcome.lost_lease
        )
    if outcome in (Outcome.succeeded, Outcome.dead_lettered):
        finalize_run(db, item.run_id)
    return outcome
