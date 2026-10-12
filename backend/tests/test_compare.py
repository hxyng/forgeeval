import random
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from scenarios import N, NullQueue, completed_run, setup_tickets
from sqlalchemy.orm import Session

from forgeeval.api.main import app
from forgeeval.queue import get_queue
from forgeeval.stats import direction, min_items_for_quantile, paired_delta

ClientFactory = Callable[..., TestClient]


# ---------- stats ----------


def test_continuous_scores_use_a_t_interval() -> None:
    # diffs 1,2,3,4: mean 2.5, sd 1.2910, se 0.6455, t(0.975, 3) = 3.1824 -> half-width 2.0542
    d = paired_delta([(0.0, 1.0), (0.0, 2.0), (0.0, 3.0), (0.0, 4.0)])
    assert d is not None and d.method == "paired-t"
    assert d.mean == pytest.approx(2.5)
    assert d.ci_high - d.mean == pytest.approx(2.0542, abs=1e-3)


def test_binary_scores_use_exact_mcnemar_and_a_bounded_interval() -> None:
    lost5 = [(1.0, 1.0)] * 15 + [(1.0, 0.0)] * 5
    d = paired_delta(lost5)
    assert d is not None and d.method == "mcnemar-newcombe"
    assert d.mean == pytest.approx(-0.25)
    assert d.p_value == pytest.approx(2 * 0.5**5)  # exact two-sided binomial on 5 discordant
    assert -1 <= d.ci_low < d.ci_high <= 1
    one = paired_delta([(1.0, 1.0)] * 19 + [(1.0, 0.0)])
    assert one is not None and not one.significant
    all_lost = paired_delta([(1.0, 0.0)] * 30)
    assert all_lost is not None and all_lost.ci_low == -1.0 and all_lost.significant


def test_binary_interval_keeps_its_coverage_when_flips_are_rare() -> None:
    # The old normal approximation covered the true drop only ~80% of the time here
    # (zero-width intervals whenever no item happened to flip).
    rng = random.Random(3)
    covered, reps, true_drop = 0, 600, 0.05
    for _ in range(reps):
        pairs = [(1.0, 0.0) if rng.random() < true_drop else (1.0, 1.0) for _ in range(30)]
        d = paired_delta(pairs)
        assert d is not None
        covered += d.ci_low <= -true_drop <= d.ci_high
    assert covered / reps >= 0.95


def test_wider_confidence_widens_the_interval() -> None:
    pairs = [(1.0, 1.0)] * 12 + [(1.0, 0.0)] * 8
    narrow, wide = paired_delta(pairs, 0.95), paired_delta(pairs, 0.975)
    assert narrow is not None and wide is not None
    assert wide.ci_low < narrow.ci_low and wide.ci_high > narrow.ci_high


def test_single_pair_and_empty() -> None:
    one = paired_delta([(0.0, 1.0)])
    assert one is not None and not one.significant and one.method == "single-pair"
    assert paired_delta([]) is None


def test_quantile_minimum_sizes() -> None:
    assert min_items_for_quantile(0.95) == 59  # log(0.05) / log(0.95) = 58.4
    assert min_items_for_quantile(0.5) == 5


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


@pytest.fixture
def client(make_client: ClientFactory) -> Iterator[TestClient]:
    app.dependency_overrides[get_queue] = NullQueue
    c = make_client()
    setup_tickets(c)
    yield c
    app.dependency_overrides.pop(get_queue, None)


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
