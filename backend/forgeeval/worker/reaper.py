"""Periodic recovery. Runs in every worker, but a Postgres advisory lock lets one at a time act.

1. Expired leases: a worker died mid-item. Count it as a failed attempt: retry or dead-letter.
2. Due retries: retry_wait items whose backoff elapsed go back to queued and are enqueued.
3. Stale queued items: never enqueued (enqueue failed after commit) or enqueued long ago with no
   claim (message lost). Re-enqueue; a duplicate message is harmless because claims are conditional.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select, text, update
from sqlalchemy.orm import Session

from forgeeval.config import get_settings
from forgeeval.db.models import EvalRun, ItemStatus, RunItem, RunStatus
from forgeeval.queue import JobQueue
from forgeeval.worker.processor import dead_letter, finalize_run, retry_delay

log = logging.getLogger("forgeeval.reaper")

REAPER_LOCK_KEY = 0x0F0E_E7A1  # arbitrary, unique to this job
BATCH = 500


@dataclass
class ReapResult:
    expired: int = 0
    dead_lettered: int = 0
    requeued: int = 0
    skipped_locked: bool = False


def _expire_leases(db: Session, result: ReapResult) -> set[uuid.UUID]:
    max_attempts = get_settings().max_attempts
    rows = db.execute(
        select(RunItem.id, RunItem.run_id, RunItem.attempt)
        .where(RunItem.status == ItemStatus.leased, RunItem.lease_expires_at < func.now())
        .limit(BATCH)
        .with_for_update(skip_locked=True)
    ).all()
    touched: set[uuid.UUID] = set()
    for item_id, run_id, attempt in rows:
        error = f"lease expired on attempt {attempt}; worker presumed dead"
        if attempt >= max_attempts:
            db.execute(
                update(RunItem)
                .where(RunItem.id == item_id)
                .values(status=ItemStatus.dead_lettered, last_error=error, lease_expires_at=None)
            )
            dead_letter(db, item_id, run_id, attempt, "timeout", error)
            result.dead_lettered += 1
            touched.add(run_id)
        else:
            db.execute(
                update(RunItem)
                .where(RunItem.id == item_id)
                .values(
                    status=ItemStatus.retry_wait,
                    last_error=error,
                    lease_expires_at=None,
                    next_attempt_at=datetime.now(UTC) + timedelta(seconds=retry_delay(attempt)),
                )
            )
        result.expired += 1
    db.commit()
    return touched


def _requeue(db: Session, queue: JobQueue, result: ReapResult) -> None:
    stale_before = datetime.now(UTC) - timedelta(seconds=get_settings().requeue_stale_after_seconds)
    due_retry = (RunItem.status == ItemStatus.retry_wait) & (RunItem.next_attempt_at <= func.now())
    stale_queued = (RunItem.status == ItemStatus.queued) & or_(
        RunItem.enqueued_at.is_(None), RunItem.enqueued_at < stale_before
    )
    ids = list(
        db.scalars(
            select(RunItem.id)
            .join(EvalRun, EvalRun.id == RunItem.run_id)
            .where(or_(due_retry, stale_queued), EvalRun.status != RunStatus.cancelled)
            .limit(BATCH)
            .with_for_update(of=RunItem, skip_locked=True)
        )
    )
    if not ids:
        db.rollback()
        return
    queue.enqueue(ids)
    db.execute(
        update(RunItem)
        .where(RunItem.id.in_(ids))
        .values(status=ItemStatus.queued, next_attempt_at=None, enqueued_at=func.now())
    )
    db.commit()
    result.requeued += len(ids)


def reap(db: Session, queue: JobQueue) -> ReapResult:
    result = ReapResult()
    got_lock = db.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": REAPER_LOCK_KEY})
    if not got_lock:
        result.skipped_locked = True
        return result
    try:
        for run_id in _expire_leases(db, result):
            finalize_run(db, run_id)
        _requeue(db, queue, result)
    finally:
        db.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": REAPER_LOCK_KEY})
        db.commit()
    if result.expired or result.requeued:
        log.info(
            "reaper: %d leases expired (%d dead-lettered), %d items requeued",
            result.expired,
            result.dead_lettered,
            result.requeued,
        )
    return result
