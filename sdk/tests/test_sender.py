from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable

import httpx
import pytest

from forgeeval_sdk import ForgeEval
from forgeeval_sdk import sender as sender_mod

from .conftest import OK_BODY, Recorder

MakeClient = Callable[..., tuple[ForgeEval, Recorder]]


def wait_for(cond: Callable[[], bool], timeout: float = 5.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        time.sleep(0.005)
    raise AssertionError("condition not met in time")


def test_batches_by_size(make_client: MakeClient) -> None:
    client, rec = make_client(batch_size=3)
    for _ in range(7):
        client.log_trace("m")
    # two full batches go out without flush(); the remainder needs a flush
    wait_for(lambda: len(rec.requests) >= 2)
    assert client.flush(timeout=5)
    sizes = [len(b) for b in rec.batches()]
    assert sizes[:2] == [3, 3] and sum(sizes) == 7
    assert client.stats()["sent"] == 7


def test_batches_by_interval(make_client: MakeClient) -> None:
    client, rec = make_client(batch_size=100, flush_interval=0.05)
    client.log_trace("m")
    wait_for(lambda: len(rec.requests) == 1)
    assert len(rec.batches()[0]) == 1


def test_retry_on_503_then_success(make_client: MakeClient, sleeps: list[float]) -> None:
    client, rec = make_client(responses=[httpx.Response(503), httpx.Response(503)])
    tid = client.log_trace("m")
    assert client.flush(timeout=5)
    assert len(rec.requests) == 3
    # same trace id on every attempt (idempotency)
    assert {b[0]["id"] for b in rec.batches()} == {tid}
    assert len(sleeps) == 2
    assert 0 <= sleeps[0] <= 0.5 and 0 <= sleeps[1] <= 1.0
    s = client.stats()
    assert s["sent"] == 1 and s["retries"] == 2 and s["failed"] == 0


def test_retry_on_network_error(make_client: MakeClient, sleeps: list[float]) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("down", request=request)
        return httpx.Response(202, json={**OK_BODY, "accepted": 1})

    client, _ = make_client(handler=handler)
    client.log_trace("m")
    assert client.flush(timeout=5)
    assert calls["n"] == 2 and len(sleeps) == 1
    assert client.stats()["sent"] == 1


def test_gives_up_after_max_attempts(make_client: MakeClient, sleeps: list[float]) -> None:
    client, rec = make_client(responses=[httpx.Response(500)] * 10)
    client.log_trace("m")
    assert client.flush(timeout=5)
    assert len(rec.requests) == sender_mod.MAX_ATTEMPTS == 5
    assert len(sleeps) == 4
    # full-jitter bounds, capped at 30s
    for attempt, s in enumerate(sleeps):
        assert 0 <= s <= min(30.0, 0.5 * 2**attempt)
    assert client.stats()["failed"] == 1


def test_backoff_cap() -> None:
    assert all(sender_mod.backoff_delay(20) <= 30.0 for _ in range(50))


def test_no_retry_on_400(make_client: MakeClient, sleeps: list[float]) -> None:
    client, rec = make_client(responses=[httpx.Response(400, json={"detail": "bad"})])
    client.log_trace("m")
    assert client.flush(timeout=5)
    assert len(rec.requests) == 1 and sleeps == []
    assert client.stats()["failed"] == 1


@pytest.mark.parametrize("status", [401, 404, 422])
def test_no_retry_on_other_4xx(make_client: MakeClient, sleeps: list[float], status: int) -> None:
    client, rec = make_client(responses=[httpx.Response(status)])
    client.log_trace("m")
    assert client.flush(timeout=5)
    assert len(rec.requests) == 1 and sleeps == []


@pytest.mark.parametrize("status", [429, 503])
def test_retry_after_honored(make_client: MakeClient, sleeps: list[float], status: int) -> None:
    client, rec = make_client(responses=[httpx.Response(status, headers={"Retry-After": "7"})])
    client.log_trace("m")
    assert client.flush(timeout=5)
    assert sleeps == [7.0]
    assert len(rec.requests) == 2


def test_retry_after_http_date_and_garbage() -> None:
    assert sender_mod.parse_retry_after("abc") is None
    assert sender_mod.parse_retry_after(None) is None
    assert sender_mod.parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0
    assert sender_mod.parse_retry_after("9999") == sender_mod.RETRY_AFTER_CAP


def test_rejected_items_logged_at_warning(
    make_client: MakeClient, caplog: pytest.LogCaptureFixture
) -> None:
    body = {
        "accepted": 0,
        "duplicates": 0,
        "rejected": [{"index": 0, "id": "abc", "error": "too big"}],
        "unresolved_prompts": [],
    }
    client, _ = make_client(responses=[httpx.Response(202, json=body)])
    with caplog.at_level(logging.WARNING, logger="forgeeval_sdk"):
        client.log_trace("m")
        assert client.flush(timeout=5)
    msgs = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("too big" in r.getMessage() and "abc" in r.getMessage() for r in msgs)
    assert client.stats()["rejected"] == 1


def test_queue_full_drops_newest_and_never_blocks(make_client: MakeClient) -> None:
    gate = threading.Event()
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        gate.wait(5)
        traces = json.loads(request.content)["traces"]
        seen.extend(int(t["input"]) for t in traces)
        return httpx.Response(202, json={**OK_BODY, "accepted": len(traces)})

    client, _ = make_client(handler=handler, batch_size=2, max_queue=4, flush_interval=60)
    t0 = time.monotonic()
    for i in range(20):
        client.log_trace("m", input=i)
    assert time.monotonic() - t0 < 2.0  # caller never blocked on the stuck network
    gate.set()
    assert client.flush(timeout=5)
    stats = client.stats()
    assert stats["dropped"] > 0
    assert stats["dropped"] + stats["queued"] == 20
    assert stats["sent"] == stats["queued"] == len(seen)
    # newest dropped: the surviving traces are the earliest ones submitted (in order)
    assert seen == sorted(seen) and seen[0] == 0


def test_enqueue_after_close_counts_as_dropped(make_client: MakeClient) -> None:
    client, rec = make_client()
    client.close(timeout=2)
    client.log_trace("m")
    assert client.stats()["dropped"] == 1 and rec.requests == []


def test_close_flushes_pending(make_client: MakeClient) -> None:
    client, rec = make_client(batch_size=100)
    for _ in range(5):
        client.log_trace("m")
    client.close(timeout=5)
    assert sum(len(b) for b in rec.batches()) == 5


def test_background_errors_never_raise(make_client: MakeClient, sleeps: list[float]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise RuntimeError("transport exploded")

    client, _ = make_client(handler=handler)
    client.log_trace("m")
    assert client.flush(timeout=5)
    assert client.stats()["failed"] == 1
    client.log_trace("m")  # thread still alive and usable
    assert client.flush(timeout=5)
    assert client.stats()["failed"] == 2
