import uuid
from collections.abc import Callable, Iterator, Sequence
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from forgeeval.api.main import app
from forgeeval.db.models import RunItem
from forgeeval.models_client import Generation
from forgeeval.queue import Message, get_queue
from forgeeval.stats import direction, paired_delta
from forgeeval.worker.processor import process

ClientFactory = Callable[..., TestClient]
N = 20


# ---------- stats ----------


def test_paired_delta_matches_hand_computation() -> None:
    # 1 of 20 items flips 1 -> 0: mean -0.05, sd^2 = 0.05, se = 0.05, half-width 0.098
    pairs = [(1.0, 1.0)] * 19 + [(1.0, 0.0)]
    d = paired_delta(pairs)
    assert d is not None
    assert d.mean == pytest.approx(-0.05)
    assert d.ci95_high - d.mean == pytest.approx(1.959964 * 0.05, rel=1e-6)
    assert not d.significant


def test_constant_shift_is_significant_and_single_pair_is_not() -> None:
    d = paired_delta([(100.0, 150.0)] * 10)
    assert d is not None and d.mean == 50 and d.significant
    one = paired_delta([(0.0, 1.0)])
    assert one is not None and not one.significant
    assert paired_delta([]) is None


@pytest.mark.parametrize(
    ("delta", "higher", "sig", "expected"),
    [
        (-0.2, True, True, "regressed"),
        (0.2, True, True, "improved"),
        (50, False, True, "regressed"),
        (-50, False, True, "improved"),
        (-0.2, True, False, "unchanged"),
    ],
)
def test_direction_respects_metric_polarity(
    delta: float, higher: bool, sig: bool, expected: str
) -> None:
    assert direction(delta, higher, sig) == expected


# ---------- API ----------


class NullQueue:
    def enqueue(self, run_item_ids: Sequence[uuid.UUID]) -> None: ...

    def receive(self, consumer: str, max_messages: int, wait_seconds: float) -> list[Message]:
        return []

    def ack(self, message: Message) -> None: ...

    def depth(self) -> int:
        return 0


class LabelModel:
    """Correct label, except for the first `wrong_first` tickets; latency from params."""

    def generate(
        self, model: str, prompt: str, params: dict[str, Any], expected: Any = None
    ) -> Generation:
        idx = int(prompt.rsplit(" ", 1)[-1])
        wrong = idx < int(params.get("wrong_first", 0))
        return Generation(
            output={"label": "WRONG" if wrong else expected["label"]},
            latency_ms=100 + int(params.get("extra_latency", 0)),
            cost_usd=Decimal("0.001"),
        )


@pytest.fixture
def client(make_client: ClientFactory) -> Iterator[TestClient]:
    app.dependency_overrides[get_queue] = NullQueue
    c = make_client()
    c.post("/v1/datasets", json={"name": "tickets"})
    c.post(
        "/v1/datasets/tickets/versions",
        json={
            "items": [
                {
                    "input": {"q": f"ticket {i}"},
                    "expected_output": {"label": ["billing", "bug"][i % 2]},
                }
                for i in range(N)
            ]
        },
    )
    c.post("/v1/evaluators", json={"name": "label", "kind": "exact_match"})
    c.post("/v1/evaluators/label/versions", json={"config": {"field": "label"}})
    c.post("/v1/evaluators", json={"name": "speed", "kind": "latency"})
    c.post("/v1/evaluators/speed/versions", json={"config": {"max_ms": 1000}})
    yield c
    app.dependency_overrides.pop(get_queue, None)


def completed_run(
    client: TestClient,
    db: Session,
    params: dict[str, Any],
    evaluators: list[str] | None = None,
    **extra: Any,
) -> str:
    pv = client.post(
        "/v1/prompts/triage/versions",
        json={"template": "Classify: {q}", "model": "stub/label", "params": params},
    ).json()
    run = client.post(
        "/v1/runs",
        json={
            "dataset": {"name": "tickets"},
            "prompt": {"name": "triage", "version": pv["version"]},
            "evaluators": [{"name": e} for e in (evaluators or ["label", "speed"])],
            **extra,
        },
    ).json()
    for item_id in db.scalars(select(RunItem.id).where(RunItem.run_id == run["id"])):
        process(db, item_id, "test", LabelModel())
    return str(run["id"])


def by_name(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {r["evaluator"]: r for r in rows}


def test_metrics_aggregate_scores(client: TestClient, db: Session) -> None:
    run_id = completed_run(client, db, {"wrong_first": 5})
    m = client.get(f"/v1/runs/{run_id}/metrics").json()
    assert m["complete"] and m["status"] == "succeeded"
    label, speed = by_name(m["evaluators"])["label"], by_name(m["evaluators"])["speed"]
    assert label["n"] == N and label["mean"] == pytest.approx(0.75)
    assert label["pass_rate"] == pytest.approx(0.75) and label["pass_n"] == N
    assert speed["p50"] == 100 and speed["p95"] == 100 and speed["higher_is_better"] is False


def test_real_regression_is_flagged_with_the_flipped_items(client: TestClient, db: Session) -> None:
    base = completed_run(client, db, {})
    cand = completed_run(client, db, {"wrong_first": 5})
    cmp = client.get(f"/v1/runs/{cand}/compare", params={"baseline_run_id": base}).json()
    label = by_name(cmp["evaluators"])["label"]
    assert label["paired_n"] == N
    assert label["delta_mean"] == pytest.approx(-0.25)
    assert label["delta_pass_rate"] == pytest.approx(-0.25)
    assert label["significant"] and label["direction"] == "regressed"
    assert label["ci95_high"] < 0
    assert len(cmp["newly_failing"]) == 5 and cmp["newly_passing"] == []
    assert by_name(cmp["evaluators"])["speed"]["direction"] == "unchanged"


def test_single_flip_is_within_noise(client: TestClient, db: Session) -> None:
    base = completed_run(client, db, {})
    cand = completed_run(client, db, {"wrong_first": 1})
    label = by_name(
        client.get(f"/v1/runs/{cand}/compare", params={"baseline_run_id": base}).json()[
            "evaluators"
        ]
    )["label"]
    assert label["delta_mean"] == pytest.approx(-0.05)
    assert not label["significant"] and label["direction"] == "unchanged"


def test_lower_is_better_metrics_and_improvements(client: TestClient, db: Session) -> None:
    base = completed_run(client, db, {"wrong_first": 6})
    cand = completed_run(client, db, {"extra_latency": 50}, baseline_run_id=base)
    cmp = client.get(f"/v1/runs/{cand}/compare").json()  # baseline taken from the run
    assert cmp["baseline_run_id"] == base
    evs = by_name(cmp["evaluators"])
    assert evs["speed"]["delta_mean"] == 50 and evs["speed"]["direction"] == "regressed"
    assert evs["label"]["direction"] == "improved" and len(cmp["newly_passing"]) == 6


def test_evaluators_in_only_one_run_are_reported(client: TestClient, db: Session) -> None:
    base = completed_run(client, db, {}, evaluators=["label"])
    cand = completed_run(client, db, {}, evaluators=["label", "speed"])
    cmp = client.get(f"/v1/runs/{cand}/compare", params={"baseline_run_id": base}).json()
    assert [e["evaluator"] for e in cmp["evaluators"]] == ["label"]
    assert cmp["not_comparable"] == ["speed@v1"]


def test_invalid_comparisons(client: TestClient, db: Session) -> None:
    run = completed_run(client, db, {})
    assert client.get(f"/v1/runs/{run}/compare").status_code == 422  # no baseline
    same = client.get(f"/v1/runs/{run}/compare", params={"baseline_run_id": run})
    assert same.status_code == 422
    missing = client.get(f"/v1/runs/{run}/compare", params={"baseline_run_id": str(uuid.uuid4())})
    assert missing.status_code == 404

    client.post(
        "/v1/datasets/tickets/versions",
        json={"items": [{"input": {"q": "ticket 0"}, "expected_output": {"label": "x"}}]},
    )
    other = completed_run(client, db, {})
    resp = client.get(f"/v1/runs/{other}/compare", params={"baseline_run_id": run})
    assert resp.status_code == 422 and "different dataset versions" in resp.json()["detail"]
