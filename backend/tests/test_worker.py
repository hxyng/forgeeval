import uuid
from collections.abc import Callable, Iterator, Sequence
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from forgeeval.api.main import app
from forgeeval.db.models import DeadLetter, EvalRun, ItemStatus, RunItem, RunStatus, Score
from forgeeval.models_client import FakeModels, Generation, PermanentError, TransientError
from forgeeval.queue import Message, RedisStreamQueue, get_queue
from forgeeval.worker import processor
from forgeeval.worker.main import Worker
from forgeeval.worker.processor import Outcome, claim, process
from forgeeval.worker.reaper import REAPER_LOCK_KEY, reap

ClientFactory = Callable[..., TestClient]
N_ITEMS = 4


class RecordingQueue:
    def __init__(self, fail: bool = False) -> None:
        self.ids: list[uuid.UUID] = []
        self.fail = fail

    def enqueue(self, run_item_ids: Sequence[uuid.UUID]) -> None:
        if self.fail:
            raise ConnectionError("redis down")
        self.ids.extend(run_item_ids)

    def receive(self, consumer: str, max_messages: int, wait_seconds: float) -> list[Message]:
        return []

    def ack(self, message: Message) -> None: ...

    def depth(self) -> int:
        return len(self.ids)


class ScriptedModels:
    """Raises the queued exceptions in order, then behaves like fake/oracle."""

    def __init__(self, *errors: Exception) -> None:
        self.errors = list(errors)
        self.calls = 0

    def generate(
        self, model: str, prompt: str, params: dict[str, Any], expected: Any = None
    ) -> Generation:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return Generation(output=expected, latency_ms=40, cost_usd=Decimal("0.0001"))


@pytest.fixture
def queue() -> Iterator[RecordingQueue]:
    q = RecordingQueue()
    app.dependency_overrides[get_queue] = lambda: q
    yield q
    app.dependency_overrides.pop(get_queue, None)


@pytest.fixture
def client(make_client: ClientFactory, queue: RecordingQueue) -> TestClient:
    c = make_client()
    c.post("/v1/prompts/oracle/versions", json={"template": "Q: {q}", "model": "fake/oracle"})
    c.post(
        "/v1/prompts/broken/versions",
        json={"template": "Q: {q}", "model": "fake/oracle", "params": {"permanent_fail": True}},
    )
    c.post("/v1/datasets", json={"name": "faq"})
    c.post(
        "/v1/datasets/faq/versions",
        json={
            "items": [
                {"input": {"q": f"q{i}"}, "expected_output": {"label": f"L{i}"}}
                for i in range(N_ITEMS)
            ]
        },
    )
    c.post("/v1/evaluators", json={"name": "match", "kind": "exact_match"})
    c.post("/v1/evaluators/match/versions", json={"config": {"field": "label"}})
    c.post("/v1/evaluators", json={"name": "speed", "kind": "latency"})
    c.post("/v1/evaluators/speed/versions", json={"config": {"max_ms": 1000}})
    return c


def start_run(client: TestClient, prompt: str = "oracle", **extra: Any) -> uuid.UUID:
    resp = client.post(
        "/v1/runs",
        json={
            "dataset": {"name": "faq"},
            "prompt": {"name": prompt},
            "evaluators": [{"name": "match"}, {"name": "speed"}],
            **extra,
        },
    )
    assert resp.status_code == 201, resp.text
    return uuid.UUID(resp.json()["id"])


def items(db: Session, run_id: uuid.UUID) -> list[RunItem]:
    db.expire_all()
    return list(db.scalars(select(RunItem).where(RunItem.run_id == run_id).order_by(RunItem.id)))


def run_row(db: Session, run_id: uuid.UUID) -> EvalRun:
    db.expire_all()
    return db.get_one(EvalRun, run_id)


def make_due(db: Session, run_id: uuid.UUID) -> None:
    db.execute(
        update(RunItem)
        .where(RunItem.run_id == run_id, RunItem.status == ItemStatus.retry_wait)
        .values(next_attempt_at=func.now() - text("interval '1 second'"))
    )
    db.commit()


# ---------- happy path ----------


def test_items_are_scored_and_run_succeeds(client: TestClient, db: Session) -> None:
    run_id = start_run(client)
    models = FakeModels()
    for item in items(db, run_id):
        assert process(db, item.id, "w1", models) is Outcome.succeeded

    run = run_row(db, run_id)
    assert run.status is RunStatus.succeeded
    assert run.succeeded_items == N_ITEMS and run.failed_items == 0
    assert run.started_at is not None and run.finished_at is not None
    assert run.cost_usd > 0
    scores = db.scalars(
        select(Score).join(RunItem, RunItem.id == Score.run_item_id).where(RunItem.run_id == run_id)
    ).all()
    assert len(scores) == N_ITEMS * 2
    assert all(s.passed for s in scores)

    api = client.get(f"/v1/runs/{run_id}").json()
    assert api["status"] == "succeeded" and api["progress"]["succeeded"] == N_ITEMS


def test_duplicate_delivery_is_a_no_op(client: TestClient, db: Session) -> None:
    run_id = start_run(client)
    item = items(db, run_id)[0]
    models = ScriptedModels()
    assert process(db, item.id, "w1", models) is Outcome.succeeded
    assert process(db, item.id, "w2", models) is Outcome.skipped
    assert models.calls == 1
    assert run_row(db, run_id).succeeded_items == 1


# ---------- failures ----------


def test_transient_failure_retries_with_backoff_then_succeeds(
    client: TestClient, db: Session, queue: RecordingQueue
) -> None:
    run_id = start_run(client)
    item = items(db, run_id)[0]
    models = ScriptedModels(TransientError("HTTP 503"))

    assert process(db, item.id, "w1", models) is Outcome.retrying
    waiting = items(db, run_id)[0]
    assert waiting.status is ItemStatus.retry_wait and waiting.attempt == 1
    assert waiting.next_attempt_at is not None and "HTTP 503" in (waiting.last_error or "")

    assert process(db, item.id, "w1", models) is Outcome.skipped  # backoff not elapsed
    make_due(db, run_id)
    queue.ids.clear()
    assert reap(db, queue).requeued >= 1
    assert item.id in queue.ids

    assert process(db, item.id, "w1", models) is Outcome.succeeded
    done = items(db, run_id)[0]
    assert done.attempt == 2 and done.last_error is None


def test_exhausted_retries_dead_letter_and_fail_the_run(client: TestClient, db: Session) -> None:
    run_id = start_run(client)
    first, *rest = items(db, run_id)
    db.execute(update(RunItem).where(RunItem.id == first.id).values(attempt=4))
    db.commit()

    models = ScriptedModels(TransientError("timeout"))
    assert process(db, first.id, "w1", models) is Outcome.dead_lettered
    for item in rest:
        process(db, item.id, "w1", models)

    letter = db.scalar(select(DeadLetter).where(DeadLetter.run_item_id == first.id))
    assert letter is not None and letter.attempts == 5 and letter.error_class == "transient"
    run = run_row(db, run_id)
    assert run.failed_items == 1 and run.succeeded_items == N_ITEMS - 1
    assert run.status is RunStatus.failed  # 1/4 failed > default 2% tolerance


def test_failures_within_tolerance_make_the_run_partial(client: TestClient, db: Session) -> None:
    run_id = start_run(client, max_failure_ratio="0.5")
    first, *rest = items(db, run_id)
    assert process(db, first.id, "w1", ScriptedModels(PermanentError("bad"))) is (
        Outcome.dead_lettered
    )
    for item in rest:
        process(db, item.id, "w1", ScriptedModels())
    assert run_row(db, run_id).status is RunStatus.partial


def test_permanent_error_skips_retries(client: TestClient, db: Session) -> None:
    run_id = start_run(client, prompt="broken")
    item = items(db, run_id)[0]
    assert process(db, item.id, "w1", FakeModels()) is Outcome.dead_lettered
    row = items(db, run_id)[0]
    assert row.attempt == 1 and "permanently" in (row.last_error or "")


def test_unexpected_exceptions_are_retried(client: TestClient, db: Session) -> None:
    run_id = start_run(client)
    item = items(db, run_id)[0]
    assert process(db, item.id, "w1", ScriptedModels(KeyError("surprise"))) is Outcome.retrying


def test_cancelled_run_items_are_not_processed(client: TestClient, db: Session) -> None:
    run_id = start_run(client)
    client.post(f"/v1/runs/{run_id}/cancel")
    models = ScriptedModels()
    assert all(process(db, i.id, "w1", models) is Outcome.skipped for i in items(db, run_id))
    assert models.calls == 0


def test_stale_worker_cannot_overwrite_a_reassigned_item(client: TestClient, db: Session) -> None:
    run_id = start_run(client)
    item = items(db, run_id)[0]
    claimed = claim(db, item.id, "worker-a")
    assert claimed is not None
    db.execute(update(RunItem).where(RunItem.id == item.id).values(leased_by="worker-b"))
    db.commit()
    result = {"output": {"label": "x"}, "latency_ms": 1, "cost_usd": Decimal(0), "scores": []}
    assert processor._finish_success(db, claimed, "worker-a", result) is False
    assert items(db, run_id)[0].status is ItemStatus.leased


# ---------- reaper ----------


def expire_lease(db: Session, item_id: uuid.UUID, attempt: int | None = None) -> None:
    values: dict[str, Any] = {"lease_expires_at": func.now() - text("interval '1 minute'")}
    if attempt is not None:
        values["attempt"] = attempt
    db.execute(update(RunItem).where(RunItem.id == item_id).values(**values))
    db.commit()


def test_reaper_retries_items_whose_worker_died(
    client: TestClient, db: Session, queue: RecordingQueue
) -> None:
    run_id = start_run(client)
    item = items(db, run_id)[0]
    claim(db, item.id, "dead-worker")
    expire_lease(db, item.id)

    result = reap(db, queue)
    assert result.expired == 1 and result.dead_lettered == 0
    row = items(db, run_id)[0]
    assert row.status is ItemStatus.retry_wait and "lease expired" in (row.last_error or "")


def test_reaper_dead_letters_expired_lease_on_last_attempt(
    client: TestClient, db: Session, queue: RecordingQueue
) -> None:
    run_id = start_run(client)
    first, *rest = items(db, run_id)
    for item in rest:
        process(db, item.id, "w1", ScriptedModels())
    claim(db, first.id, "dead-worker")
    expire_lease(db, first.id, attempt=5)

    assert reap(db, queue).dead_lettered == 1
    letter = db.scalar(select(DeadLetter).where(DeadLetter.run_item_id == first.id))
    assert letter is not None and letter.error_class == "timeout"
    assert run_row(db, run_id).status is RunStatus.failed  # finalized by the reaper


def test_reaper_enqueues_items_whose_enqueue_failed(client: TestClient, db: Session) -> None:
    failing = RecordingQueue(fail=True)
    app.dependency_overrides[get_queue] = lambda: failing
    run_id = start_run(client)
    assert all(i.enqueued_at is None for i in items(db, run_id))

    recovered = RecordingQueue()
    assert reap(db, recovered).requeued == N_ITEMS
    assert set(recovered.ids) == {i.id for i in items(db, run_id)}
    assert all(i.enqueued_at is not None for i in items(db, run_id))
    assert reap(db, recovered).requeued == 0  # freshly enqueued items are left alone


def test_only_one_reaper_runs_at_a_time(
    client: TestClient, db: Session, queue: RecordingQueue, migrated_engine: Engine
) -> None:
    with migrated_engine.connect() as other:
        assert other.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": REAPER_LOCK_KEY})
        assert reap(db, queue).skipped_locked
        other.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": REAPER_LOCK_KEY})
    assert not reap(db, queue).skipped_locked


# ---------- end to end ----------


def test_worker_drains_a_run_through_real_redis(
    client: TestClient, db: Session, redis_queue: RedisStreamQueue
) -> None:
    app.dependency_overrides[get_queue] = lambda: redis_queue
    run_id = start_run(client)
    worker = Worker(queue=redis_queue, models=FakeModels(), worker_id="e2e", wait_seconds=0.1)
    for _ in range(5):
        worker.run_once()
        if run_row(db, run_id).status is RunStatus.succeeded:
            break
    assert run_row(db, run_id).status is RunStatus.succeeded
    assert redis_queue.depth() == 0
