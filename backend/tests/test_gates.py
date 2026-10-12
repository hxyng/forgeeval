import uuid
from collections.abc import Callable, Iterator
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from scenarios import NullQueue, completed_run, setup_tickets
from sqlalchemy.orm import Session

from forgeeval.api.compare import EvaluatorComparison, EvaluatorMetrics
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

# ---------- verdict logic ----------


def metrics(mean: float | None = None, p95: float | None = None) -> EvaluatorMetrics:
    return EvaluatorMetrics(
        evaluator="e",
        version=1,
        evaluator_version_id=uuid.uuid4(),
        kind=EvaluatorKind.latency,
        higher_is_better=True,
        n=10,
        mean=mean,
        p50=None,
        p95=p95,
        pass_rate=None,
        pass_n=0,
    )


def gate(agg: GateAggregate, comp: GateComparator, threshold: str) -> RegressionGate:
    return RegressionGate(name="g", aggregate=agg, comparator=comp, threshold=Decimal(threshold))


def comparison(significant: bool) -> EvaluatorComparison:
    m = metrics(0.5)
    return EvaluatorComparison(
        evaluator="e",
        version=1,
        higher_is_better=True,
        candidate=m,
        baseline=m,
        paired_n=20,
        delta_mean=-0.1,
        ci95_low=-0.2 if significant else -0.3,
        ci95_high=-0.01 if significant else 0.1,
        delta_pass_rate=None,
        significant=significant,
        direction="regressed" if significant else "unchanged",
    )


MIN_P95 = gate(GateAggregate.p95, GateComparator.min_absolute, "1500")
DROP_MEAN = gate(GateAggregate.mean, GateComparator.max_drop_vs_baseline, "0.05")


@pytest.mark.parametrize(
    ("g", "hib", "cand", "base", "cmp", "verdict"),
    [
        (MIN_P95, False, metrics(p95=1200), None, None, GateVerdict.passed),
        (MIN_P95, False, metrics(p95=1800), None, None, GateVerdict.failed),
        (
            gate(GateAggregate.mean, GateComparator.min_absolute, "0.9"),
            True,
            metrics(0.85),
            None,
            None,
            GateVerdict.failed,
        ),
        (DROP_MEAN, True, metrics(0.80), metrics(0.82), comparison(True), GateVerdict.passed),
        (DROP_MEAN, True, metrics(0.70), metrics(0.82), comparison(True), GateVerdict.failed),
        (DROP_MEAN, True, metrics(0.70), metrics(0.82), comparison(False), GateVerdict.passed),
        (DROP_MEAN, True, metrics(0.70), None, None, GateVerdict.inconclusive),
        (DROP_MEAN, True, None, metrics(0.8), None, GateVerdict.inconclusive),
        (MIN_P95, False, metrics(mean=1.0), None, None, GateVerdict.inconclusive),
    ],
)
def test_decide(
    g: RegressionGate,
    hib: bool,
    cand: EvaluatorMetrics | None,
    base: EvaluatorMetrics | None,
    cmp: EvaluatorComparison | None,
    verdict: GateVerdict,
) -> None:
    assert decide(g, hib, cand, base, cmp).verdict is verdict


def test_noise_verdict_explains_itself() -> None:
    v = decide(DROP_MEAN, True, metrics(0.70), metrics(0.82), comparison(False))
    assert "treated as noise" in v.reason and v.delta == Decimal("-0.12")


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
SPEED = {"aggregate": "p95", "comparator": "min_absolute", "threshold": "120"}


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
    cand = completed_run(client, db, {"wrong_first": 5})
    check = client.post(
        "/v1/gates/accuracy/check",
        json={"run_id": cand, "repo": "acme/bot", "pr_number": 42, "commit_sha": "abc123"},
    ).json()
    assert check["verdict"] == "fail"
    assert check["baseline_run_id"] == base
    assert Decimal(check["delta"]) == Decimal("-0.25")
    assert "worsened by 0.25" in check["reason"]

    history = client.get("/v1/gates/accuracy/checks").json()
    assert history[0]["pr_number"] == 42 and history[0]["verdict"] == "fail"


def test_noise_passes_and_improvement_passes(client: TestClient, db: Session) -> None:
    make_gate(client, "accuracy", "label", **{**ACCURACY, "threshold": "0.01"})
    base = completed_run(client, db, {})
    noisy = completed_run(client, db, {"wrong_first": 1}, baseline_run_id=base)
    v = client.post("/v1/gates/accuracy/check", json={"run_id": noisy}).json()
    assert v["verdict"] == "pass" and "treated as noise" in v["reason"]


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
    cand = completed_run(client, db, {"wrong_first": 5})
    out = client.post("/v1/gates:check", json={"run_id": cand}).json()
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
    v = decide(DROP_MEAN, True, metrics(1.0), metrics(1.0), comparison(False))
    assert v.verdict is GateVerdict.passed
    assert "worsened by 0," in v.reason and "-0" not in v.reason
