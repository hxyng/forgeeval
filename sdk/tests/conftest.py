from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest

from forgeeval_sdk import ForgeEval
from forgeeval_sdk import sender as sender_mod

OK_BODY = {"accepted": 0, "duplicates": 0, "rejected": [], "unresolved_prompts": []}


class Recorder:
    """MockTransport handler that records requests and replays scripted responses."""

    def __init__(self, responses: list[httpx.Response] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self._responses = list(responses or [])

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._responses:
            return self._responses.pop(0)
        n = len(json.loads(request.content)["traces"])
        return httpx.Response(202, json={**OK_BODY, "accepted": n})

    def batches(self) -> list[list[dict[str, Any]]]:
        return [json.loads(r.content)["traces"] for r in self.requests]


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    recorded: list[float] = []
    monkeypatch.setattr(sender_mod, "_sleep", recorded.append)
    return recorded


@pytest.fixture
def make_client() -> Iterator[Callable[..., tuple[ForgeEval, Recorder]]]:
    clients: list[ForgeEval] = []

    def _make(
        responses: list[httpx.Response] | None = None,
        handler: Callable[[httpx.Request], httpx.Response] | None = None,
        **kwargs: Any,
    ) -> tuple[ForgeEval, Recorder]:
        rec = Recorder(responses)
        kwargs.setdefault("api_key", "fe_abcd1234_secretsecret")
        kwargs.setdefault("flush_interval", 60.0)
        transport = httpx.MockTransport(handler or rec)
        client = ForgeEval(transport=transport, **kwargs)
        clients.append(client)
        return client, rec

    yield _make
    for c in clients:
        c.close(timeout=2.0)
