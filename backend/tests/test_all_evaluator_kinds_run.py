import json
import uuid
from collections.abc import Callable, Iterator, Sequence
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from forgeeval.api.main import app
from forgeeval.db.models import EvalRun, RunItem, RunStatus, Score
from forgeeval.models_client import FakeModels, Generation
from forgeeval.queue import Message, get_queue
from forgeeval.worker.processor import Outcome, process

ClientFactory = Callable[..., TestClient]

SCHEMA = {"type": "object", "required": ["n"], "properties": {"n": {"type": "integer"}}}


class NullQueue:
    def enqueue(self, run_item_ids: Sequence[uuid.UUID]) -> None: ...

    def receive(self, consumer: str, max_messages: int, wait_seconds: float) -> list[Message]:
        return []

    def ack(self, message: Message) -> None: ...

    def depth(self) -> int:
        return 0


@pytest.fixture
def client(make_client: ClientFactory) -> Iterator[TestClient]:
    app.dependency_overrides[get_queue] = lambda: NullQueue()
    c = make_client()
    c.post("/v1/prompts/oracle/versions", json={"template": "Q: {q}", "model": "fake/oracle"})
    yield c
    app.dependency_overrides.pop(get_queue, None)


class SystemUnderTest:
    """Answers with everything the six evaluators need; delegates judge calls to FakeModels."""

    def __init__(self) -> None:
        self.fake = FakeModels()

    def generate(
        self, model: str, prompt: str, params: dict[str, Any], expected: Any = None
    ) -> Generation:
        if model == "fake/judge":
            return self.fake.generate(model, prompt, params, expected)
        output = {
            "label": expected["label"],
            "text": json.dumps({"n": 1}),
            "retrieved_doc_ids": ["d1", "zz", "d2"],
        }
        return Generation(output=output, latency_ms=40, cost_usd=Decimal("0.0001"))


def test_run_with_all_six_evaluator_kinds_completes(client: TestClient, db: Session) -> None:
    client.post("/v1/datasets", json={"name": "rag"})
    resp = client.post(
        "/v1/datasets/rag/versions",
        json={
            "items": [
                {
                    "input": {"q": f"q{i}"},
                    "expected_output": {"label": f"L{i}"},
                    "context": {"relevant_doc_ids": ["d1", "d2", "d3"]},
                }
                for i in range(3)
            ]
        },
    )
    assert resp.status_code in (200, 201), resp.text
    rubric = {"rubric": "Is the answer helpful?", "pass_threshold": 4}
    specs: list[tuple[str, str, dict[str, Any], str | None]] = [
        ("ex", "exact_match", {"field": "label"}, None),
        ("js", "json_schema", {"schema": SCHEMA, "field": "text"}, None),
        ("lat", "latency", {"max_ms": 1000}, None),
        ("cst", "cost", {"max_usd": 1}, None),
        ("ret", "retrieval_relevance", {"k": 3}, None),
        ("rub", "llm_rubric", rubric, "fake/judge"),
    ]
    for name, kind, config, judge_model in specs:
        made = client.post("/v1/evaluators", json={"name": name, "kind": kind})
        assert made.status_code == 201, made.text
        body: dict[str, Any] = {"config": config}
        if judge_model:
            body["judge_model"] = judge_model
        resp = client.post(f"/v1/evaluators/{name}/versions", json=body)
        assert resp.status_code == 201, resp.text

    resp = client.post(
        "/v1/runs",
        json={
            "dataset": {"name": "rag"},
            "prompt": {"name": "oracle"},
            "evaluators": [{"name": n} for n, *_ in specs],
        },
    )
    assert resp.status_code == 201, resp.text
    run_id = uuid.UUID(resp.json()["id"])

    models = SystemUnderTest()
    db.expire_all()
    run_items = list(db.scalars(select(RunItem).where(RunItem.run_id == run_id)))
    assert len(run_items) == 3
    for item in run_items:
        assert process(db, item.id, "w1", models) is Outcome.succeeded

    db.expire_all()
    run = db.get_one(EvalRun, run_id)
    assert run.status is RunStatus.succeeded and run.succeeded_items == 3
    scores = db.scalars(
        select(Score).join(RunItem, RunItem.id == Score.run_item_id).where(RunItem.run_id == run_id)
    ).all()
    assert len(scores) == 3 * len(specs)
    assert len({s.evaluator_version_id for s in scores}) == len(specs)
    judged = [s for s in scores if s.rationale == "fake judge"]
    assert len(judged) == 3 and all(s.value == Decimal("0.75") and s.passed for s in judged)
