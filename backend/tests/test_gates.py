import uuid
from collections.abc import Callable, Iterator
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from scenarios import NullQueue, completed_run, setup_tickets
from sqlalchemy.orm import Session

from forgeeval.api.compare import EvaluatorMetrics
from forgeeval.api.gates import decide
from forgeeval.api.main import app
from forgeeval.db.models import (
    EvaluatorKind,
    GateAggregate,
    GateComparator,
    GateVerdict,
    RegressionGate,
)
from forgeeval.queue import get_queue
from forgeeval.stats import PairedDelta

# ---------- verdict logic ----------


def metrics(mean: float | None = None, p95: float | None = None, n: int = 100) -> EvaluatorMetrics:
    return EvaluatorMetrics(
        evaluator="e",
        version=1,
        evaluator_version_id=uuid.uuid4(),
        kind=EvaluatorKind.latency,
        higher_is_better=True,
        n=n,
        mean=mean,
        p50=None,
        p95=p95,
        pass_rate=None,
        pass_n=0,
    )


def gate(agg: GateAggregate, comp: GateComparator, threshold: str) -> RegressionGate:
    return RegressionGate(name="g", aggregate=agg, comparator=comp, threshold=Decimal(threshold))


def interval(low: float, high: float, n: int = 100) -> PairedDelta:
    """Interval on candidate - baseline (negative = worse for higher-is-better metrics)."""
    return PairedDelta(n, (low + high) / 2, low, high, 0.5, "paired-t", 0.95)


MIN_P95 = gate(GateAggregate.p95, GateComparator.min_absolute, "1500")
DROP_MEAN = gate(GateAggregate.mean, GateComparator.max_drop_vs_baseline, "0.05")


@pytest.mark.parametrize(
    ("g", "hib", "cand", "base", "ci", "verdict"),
    [
        (MIN_P95, False, metrics(p95=1200), None, None, GateVerdict.passed),
        (MIN_P95, False, metrics(p95=1800), None, None, GateVerdict.failed),
        (MIN_P95, False, metrics(p95=1200, n=58), None, None, GateVerdict.inconclusive),
        (
            gate(GateAggregate.mean, GateComparator.min_absolute, "0.9"),
            True,
            metrics(0.85),
            None,
            None,
            GateVerdict.failed,
        ),
        # worsening interval = (-high, -low): entirely above 0.05 -> fail
        (DROP_MEAN, True, metrics(0.70), metrics(0.82), interval(-0.2, -0.06), GateVerdict.failed),
        # entirely within 0.05 -> pass, even though the point estimate moved
        (DROP_MEAN, True, metrics(0.80), metrics(0.82), interval(-0.04, 0.0), GateVerdict.passed),
        # straddles 0.05 -> inconclusive, never a silent pass
        (
            DROP_MEAN,
            True,
            metrics(0.70),
            metrics(0.82),
            interval(-0.3, 0.1),
            GateVerdict.inconclusive,
        ),
        (
            DROP_MEAN,
            True,
            metrics(0.81),
            metrics(0.82),
            interval(-0.3, 0.1),
            GateVerdict.inconclusive,
        ),
        # lower-is-better: worsening interval = (low, high)
        (DROP_MEAN, False, metrics(130), metrics(100), interval(20, 40), GateVerdict.failed),
        (DROP_MEAN, True, metrics(0.70), None, None, GateVerdict.inconclusive),
        (DROP_MEAN, True, None, metrics(0.8), None, GateVerdict.inconclusive),
        (DROP_MEAN, True, metrics(0.70), metrics(0.82), None, GateVerdict.inconclusive),
        (MIN_P95, False, metrics(mean=1.0), None, None, GateVerdict.inconclusive),
    ],
)
def test_decide(
    g: RegressionGate,
    hib: bool,
    cand: EvaluatorMetrics | None,
    base: EvaluatorMetrics | None,
    ci: PairedDelta | None,
    verdict: GateVerdict,
) -> None:
    assert decide(g, hib, cand, base, ci).verdict is verdict


def test_inconclusive_says_how_many_items_would_decide() -> None:
    v = decide(DROP_MEAN, True, metrics(0.70), metrics(0.82), interval(-0.3, 0.06, n=40))
    assert v.verdict is GateVerdict.inconclusive
    assert "straddles allowed 0.05" in v.reason and "items would decide it" in v.reason
    assert v.delta == Decimal("-0.12")


def test_p95_drop_gate_needs_59_items_in_both_runs() -> None:
    drop_p95 = gate(GateAggregate.p95, GateComparator.max_drop_vs_baseline, "100")
    v = decide(drop_p95, False, metrics(p95=900, n=200), metrics(p95=800, n=30), None)
    assert v.verdict is GateVerdict.inconclusive and "at least 59" in v.reason


# ---------- API ----------


ClientFactory = Callable[..., TestClient]


@pytest.fixture
def client(make_client: ClientFactory) -> Iterator[TestClient]:
    app.dependency_overrides[get_queue] = NullQueue
    c = make_client()
    setup_tickets(c)
    yield c
    app.dependency_overrides.pop(get_queue, None)


def make_gate(c: TestClient, name: str, evaluator: str, **spec: Any) -> dict[str, Any]:
    resp = c.post("/v1/gates", json={"name": name, "evaluator": {"name": evaluator}, **spec})
    assert resp.status_code == 201, resp.text
    result: dict[str, Any] = resp.json()
    return result


ACCURACY = {"aggregate": "mean", "comparator": "max_drop_vs_baseline", "threshold": "0.05"}
SPEED = {"aggregate": "p50", "comparator": "min_absolute", "threshold": "120"}


def test_gate_crud(client: TestClient) -> None:
    g = make_gate(client, "accuracy", "label", **ACCURACY)
    assert g["evaluator"] == "label" and g["higher_is_better"] is True
    assert (
        client.post(
            "/v1/gates", json={"name": "accuracy", "evaluator": {"name": "label"}, **ACCURACY}
        ).status_code
        == 409
    )
    bad = client.post("/v1/gates", json={"name": "x", "evaluator": {"name": "nope"}, **ACCURACY})
    assert bad.status_code == 422
    patched = client.patch("/v1/gates/accuracy", json={"threshold": "0.1", "enabled": False})
    assert patched.json()["threshold"] == "0.1" and patched.json()["enabled"] is False
    assert [x["name"] for x in client.get("/v1/gates").json()] == ["accuracy"]


def test_regression_fails_the_gate_against_auto_baseline(
    client: TestClient,
    db: Session,
) -> None:
    make_gate(client, "accuracy", "label", **ACCURACY)
    base = completed_run(client, db, {})
    cand = completed_run(client, db, {"wrong_first": 8})
    check = client.post(
        "/v1/gates/accuracy/check",
        json={"run_id": cand, "repo": "acme/bot", "pr_number": 42, "commit_sha": "abc123"},
    ).json()
    assert check["verdict"] == "fail"
    assert check["baseline_run_id"] == base
    assert Decimal(check["delta"]) == Decimal("-0.4")
    assert "entirely above allowed 0.05" in check["reason"]

    history = client.get("/v1/gates/accuracy/checks").json()
    assert history[0]["pr_number"] == 42 and history[0]["verdict"] == "fail"


def test_small_drop_on_20_items_is_inconclusive_not_pass(client: TestClient, db: Session) -> None:
    make_gate(client, "accuracy", "label", **{**ACCURACY, "threshold": "0.01"})
    base = completed_run(client, db, {})
    noisy = completed_run(client, db, {"wrong_first": 1}, baseline_run_id=base)
    v = client.post("/v1/gates/accuracy/check", json={"run_id": noisy}).json()
    assert v["verdict"] == "inconclusive" and "straddles allowed 0.01" in v["reason"]

    five = completed_run(client, db, {"wrong_first": 5}, baseline_run_id=base)
    make_gate(client, "accuracy5", "label", **ACCURACY)
    v = client.post("/v1/gates/accuracy5/check", json={"run_id": five}).json()
    assert v["verdict"] == "inconclusive"  # CI on the drop reaches down to 3.8 points


def test_identical_runs_pass_at_a_tolerance_the_sample_can_certify(
    client: TestClient, db: Session
) -> None:
    make_gate(client, "loose", "label", **{**ACCURACY, "threshold": "0.2"})
    base = completed_run(client, db, {})
    same = completed_run(client, db, {}, baseline_run_id=base)
    v = client.post("/v1/gates/loose/check", json={"run_id": same}).json()
    assert v["verdict"] == "pass" and "within allowed 0.2" in v["reason"]
    assert "worsening 0," in v["reason"] and "worsening -0" not in v["reason"]


def test_p95_gate_on_20_items_is_inconclusive(client: TestClient, db: Session) -> None:
    make_gate(client, "tail", "speed", aggregate="p95", comparator="min_absolute", threshold="120")
    run = completed_run(client, db, {})
    v = client.post("/v1/gates/tail/check", json={"run_id": run}).json()
    assert v["verdict"] == "inconclusive" and "at least 59" in v["reason"]


def test_absolute_latency_gate(client: TestClient, db: Session) -> None:
    make_gate(client, "speed", "speed", **SPEED)
    fast = completed_run(client, db, {})
    slow = completed_run(client, db, {"extra_latency": 50})
    assert client.post("/v1/gates/speed/check", json={"run_id": fast}).json()["verdict"] == "pass"
    slow_check = client.post("/v1/gates/speed/check", json={"run_id": slow}).json()
    assert slow_check["verdict"] == "fail" and "<= 120" in slow_check["reason"]


def test_inconclusive_and_unfinished_runs(client: TestClient, db: Session) -> None:
    make_gate(client, "accuracy", "label", **ACCURACY)
    first = completed_run(client, db, {})
    v = client.post("/v1/gates/accuracy/check", json={"run_id": first}).json()
    assert v["verdict"] == "inconclusive" and "no comparable baseline" in v["reason"]

    pending = client.post(
        "/v1/runs",
        json={
            "dataset": {"name": "tickets"},
            "prompt": {"name": "triage"},
            "evaluators": [{"name": "label"}],
        },
    ).json()["id"]
    assert client.post("/v1/gates/accuracy/check", json={"run_id": pending}).status_code == 409
    client.post(f"/v1/runs/{pending}/cancel")
    v = client.post("/v1/gates/accuracy/check", json={"run_id": pending}).json()
    assert v["verdict"] == "inconclusive" and "cancelled" in v["reason"]


def test_check_all_combines_verdicts(client: TestClient, db: Session) -> None:
    assert (
        client.post("/v1/gates:check", json={"run_id": completed_run(client, db, {})}).json()[
            "verdict"
        ]
        == "inconclusive"
    )  # no gates apply

    make_gate(client, "accuracy", "label", **ACCURACY)
    make_gate(client, "speed", "speed", **SPEED)
    make_gate(client, "off", "label", **{**ACCURACY, "enabled": False})
    completed_run(client, db, {})
    cand = completed_run(client, db, {"wrong_first": 8})
    out = client.post("/v1/gates:check", json={"run_id": cand}).json()
    accuracy = next(c for c in out["checks"] if c["gate"] == "accuracy")
    assert "97.5% CI" in accuracy["reason"]  # Bonferroni across the 2 applicable gates
    assert out["verdict"] == "fail"
    assert {c["gate"]: c["verdict"] for c in out["checks"]} == {"accuracy": "fail", "speed": "pass"}

    only_label = completed_run(client, db, {}, evaluators=["label"])
    out = client.post("/v1/gates:check", json={"run_id": only_label}).json()
    assert [c["gate"] for c in out["checks"]] == ["accuracy"]  # speed gate doesn't apply


def test_checking_requires_gates_scope(
    client: TestClient,
    db: Session,
    make_client: ClientFactory,
) -> None:
    make_gate(client, "accuracy", "label", **ACCURACY)
    run = completed_run(client, db, {})
    reader = make_client(["project:read"])
    assert reader.post("/v1/gates/accuracy/check", json={"run_id": run}).status_code == 403
    assert reader.get("/v1/gates/accuracy/checks").status_code == 200


def test_unchanged_metric_reads_as_zero_not_negative_zero() -> None:
    drop_p50 = gate(GateAggregate.p50, GateComparator.max_drop_vs_baseline, "0.05")
    m = metrics(1.0)
    m = m.model_copy(update={"p50": 1.0})
    v = decide(drop_p50, True, m, m, None)
    assert v.verdict is GateVerdict.passed
    assert "worsened by 0," in v.reason and "-0" not in v.reason


def test_a_run_that_failed_its_gate_never_becomes_the_baseline(
    client: TestClient, db: Session
) -> None:
    make_gate(client, "accuracy", "label", **ACCURACY)
    good = completed_run(client, db, {})
    bad1 = completed_run(client, db, {"wrong_first": 8})
    first = client.post("/v1/gates/accuracy/check", json={"run_id": bad1}).json()
    assert first["verdict"] == "fail" and first["baseline_run_id"] == good

    # The same regression again must still be compared with the good run, not with bad1.
    bad2 = completed_run(client, db, {"wrong_first": 8})
    second = client.post("/v1/gates/accuracy/check", json={"run_id": bad2}).json()
    assert second["baseline_run_id"] == good
    assert second["verdict"] == "fail"
