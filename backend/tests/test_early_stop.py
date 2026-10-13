import random
import uuid
from collections.abc import Callable, Iterator, Sequence
from typing import Any

import pytest
from fastapi.testclient import TestClient
from scenarios import LabelModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from forgeeval.api.main import app
from forgeeval.db.models import EvalRun, ItemStatus, RunItem, RunStatus
from forgeeval.early_stop import check_early_stops, prefix_diffs
from forgeeval.queue import Message, get_queue
from forgeeval.stats import confidence_sequence
from forgeeval.worker.processor import process

ClientFactory = Callable[..., TestClient]
N = 50

# ---------- the confidence sequence itself ----------


def test_confidence_sequence_is_valid_under_continuous_monitoring() -> None:
    # Checked after every item, the sequence must still contain the truth in >= 95% of runs.
    rng = random.Random(4)
    reps, misses = 300, 0
    for _ in range(reps):
        diffs: list[float] = []
        for _ in range(80):
            r = rng.random()
            diffs.append(-1.0 if r < 0.3 else 1.0 if r < 0.45 else 0.0)  # true mean -0.15
            cs = confidence_sequence(diffs, 0.05)
            assert cs is not None
            if not cs.low <= -0.15 <= cs.high:
                misses += 1
                break
    assert misses / reps <= 0.05


def test_confidence_sequence_basics() -> None:
    cs = confidence_sequence([-1.0] * 40, 0.01)
    assert cs is not None and cs.mean == -1 and cs.high < 0  # decisive drop
    wide = confidence_sequence([0.0] * 5, 0.01)
    assert wide is not None and wide.low < -0.5 and wide.high > 0.5  # 5 items prove little
    assert confidence_sequence([], 0.05) is None
    with pytest.raises(ValueError):
        confidence_sequence([2.0], 0.05)


# ---------- runs that stop early ----------


class RecordingQueue:
    def __init__(self) -> None:
        self.ids: list[uuid.UUID] = []

    def enqueue(self, run_item_ids: Sequence[uuid.UUID]) -> None:
        self.ids.extend(run_item_ids)

    def receive(self, consumer: str, max_messages: int, wait_seconds: float) -> list[Message]:
        return []

    def ack(self, message: Message) -> None: ...

    def depth(self) -> int:
        return len(self.ids)


@pytest.fixture
def queue() -> Iterator[RecordingQueue]:
    q = RecordingQueue()
    app.dependency_overrides[get_queue] = lambda: q
    yield q
    app.dependency_overrides.pop(get_queue, None)


@pytest.fixture
def client(make_client: ClientFactory, queue: RecordingQueue) -> TestClient:
    c = make_client()
    c.post("/v1/datasets", json={"name": "big"})
    c.post(
        "/v1/datasets/big/versions",
        json={
            "items": [
                {"input": {"q": f"ticket {i}"}, "expected_output": {"label": ["a", "b"][i % 2]}}
                for i in range(N)
            ]
        },
    )
    c.post("/v1/evaluators", json={"name": "label", "kind": "exact_match"})
    c.post("/v1/evaluators/label/versions", json={"config": {"field": "label"}})
    c.post("/v1/evaluators", json={"name": "speed", "kind": "latency"})
    c.post("/v1/evaluators/speed/versions", json={"config": {"max_ms": 1000}})
    c.post(
        "/v1/gates",
        json={
            "name": "accuracy",
            "evaluator": {"name": "label"},
            "aggregate": "mean",
            "comparator": "max_drop_vs_baseline",
            "threshold": "0.05",
        },
    )
    return c


def create_run(c: TestClient, params: dict[str, Any], **extra: Any) -> dict[str, Any]:
    pv = c.post(
        "/v1/prompts/p/versions",
        json={"template": "Classify: {q}", "model": "stub/label", "params": params},
    ).json()
    resp = c.post(
        "/v1/runs",
        json={
            "dataset": {"name": "big"},
            "prompt": {"name": "p", "version": pv["version"]},
            "evaluators": [{"name": "label"}, {"name": "speed"}],
            **extra,
        },
    )
    assert resp.status_code == 201, resp.text
    out: dict[str, Any] = resp.json()
    return out


def run_in_order(db: Session, run_id: str, check: bool) -> int:
    """Process items in position order like the queue would, running the early-stop check every
    few items (the reaper checks every few seconds). Returns how many items were processed."""
    order = list(
        db.scalars(select(RunItem.id).where(RunItem.run_id == run_id).order_by(RunItem.position))
    )
    done = 0
    for item_id in order:
        if process(db, item_id, "test", LabelModel()).value == "skipped":
            continue  # cancelled by an early stop
        done += 1
        if check and done % 5 == 0:
            check_early_stops(db)
    return done


def run_row(db: Session, run_id: str) -> EvalRun:
    db.expire_all()
    return db.get_one(EvalRun, uuid.UUID(run_id))


def test_items_get_a_random_order_and_are_enqueued_in_it(
    client: TestClient, db: Session, queue: RecordingQueue
) -> None:
    run = create_run(client, {})
    rows = db.execute(select(RunItem.id, RunItem.position).where(RunItem.run_id == run["id"])).all()
    assert sorted(p for _, p in rows) == list(range(N))
    by_position = [rid for rid, _ in sorted(rows, key=lambda r: r[1])]
    assert queue.ids == by_position
    assert by_position != sorted(by_position)  # shuffled, not creation order


def test_a_clear_regression_stops_early_and_fails_its_gate(client: TestClient, db: Session) -> None:
    base = create_run(client, {})
    run_in_order(db, base["id"], check=False)
    cand = create_run(client, {"wrong_first": N}, early_stop=True)
    assert cand["baseline_run_id"] == base["id"]  # picked automatically

    processed = run_in_order(db, cand["id"], check=True)
    run = run_row(db, cand["id"])
    assert run.stopped_early_at is not None and processed < N
    assert run.status is RunStatus.succeeded
    assert "gate 'accuracy' failed after" in (run.early_stop_reason or "")
    cancelled = db.scalar(
        select(func.count()).where(RunItem.run_id == run.id, RunItem.status == ItemStatus.cancelled)
    )
    assert cancelled == N - processed

    check = client.post("/v1/gates/accuracy/check", json={"run_id": cand["id"]}).json()
    assert check["verdict"] == "fail"
    assert "99% CI" in check["reason"]  # anytime-valid interval at the sequential budget
    api = client.get(f"/v1/runs/{cand['id']}").json()
    assert api["early_stop"] and api["stopped_early_at"] and api["progress"]["cancelled"] > 0


def test_an_unchanged_prompt_runs_to_the_end_and_uses_the_fixed_test(
    client: TestClient, db: Session
) -> None:
    client.patch("/v1/gates/accuracy", json={"threshold": "0.1"})
    base = create_run(client, {})
    run_in_order(db, base["id"], check=False)
    cand = create_run(client, {}, early_stop=True)
    assert run_in_order(db, cand["id"], check=True) == N
    run = run_row(db, cand["id"])
    assert run.stopped_early_at is None and run.status is RunStatus.succeeded
    check = client.post("/v1/gates/accuracy/check", json={"run_id": cand["id"]}).json()
    assert check["verdict"] == "pass" and "96% CI" in check["reason"]


def test_ineligible_gate_blocks_an_early_pass_but_not_an_early_fail(
    client: TestClient, db: Session
) -> None:
    client.post(
        "/v1/gates",
        json={
            "name": "median-latency",
            "evaluator": {"name": "speed"},
            "aggregate": "p50",
            "comparator": "min_absolute",
            "threshold": "1000",
        },
    )
    base = create_run(client, {})
    run_in_order(db, base["id"], check=False)
    bad = create_run(client, {"wrong_first": N}, early_stop=True)
    assert run_in_order(db, bad["id"], check=True) < N  # failing accuracy still stops early


def test_early_stop_needs_a_baseline(client: TestClient) -> None:
    client.post("/v1/prompts/p/versions", json={"template": "Classify: {q}", "model": "stub/label"})
    resp = client.post(
        "/v1/runs",
        json={
            "dataset": {"name": "big"},
            "prompt": {"name": "p"},
            "evaluators": [{"name": "label"}],
            "early_stop": True,
        },
    )
    assert resp.status_code == 422 and "needs a baseline" in resp.json()["detail"]


def test_prefix_stops_at_the_first_unfinished_item(client: TestClient, db: Session) -> None:
    base = create_run(client, {})
    run_in_order(db, base["id"], check=False)
    cand = create_run(client, {}, early_stop=True)
    run = run_row(db, cand["id"])
    from forgeeval.db.models import EvaluatorVersion

    label_ev = db.scalar(select(EvaluatorVersion.id).order_by(EvaluatorVersion.id).limit(1))
    assert label_ev is not None
    # finish the item at position 1 but not position 0: no usable prefix yet
    second = db.scalar(select(RunItem.id).where(RunItem.run_id == run.id, RunItem.position == 1))
    assert second is not None
    process(db, second, "test", LabelModel())
    assert prefix_diffs(db, run, uuid.UUID(base["id"]), label_ev) == []
