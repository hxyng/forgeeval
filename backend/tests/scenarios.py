"""Shared test scenario: 20 support tickets, an exact-match 'label' evaluator and a
'speed' latency evaluator, and a stub model whose mistakes and latency are set by prompt params.
"""

import uuid
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from forgeeval.db.models import RunItem
from forgeeval.models_client import Generation
from forgeeval.queue import Message
from forgeeval.worker.processor import process

N = 20


def setup_tickets(c: TestClient) -> None:
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
