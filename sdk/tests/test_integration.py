from __future__ import annotations

import os
import uuid

import httpx
import pytest

API_KEY = os.environ.get("FORGEEVAL_IT_API_KEY")
BASE_URL = os.environ.get("FORGEEVAL_IT_BASE_URL", "http://localhost:8000")


@pytest.mark.integration
@pytest.mark.skipif(not API_KEY, reason="FORGEEVAL_IT_API_KEY not set")
def test_send_twice_second_is_duplicate(caplog: pytest.LogCaptureFixture) -> None:
    from forgeeval_sdk import ForgeEval

    tid = uuid.uuid4().hex
    results: list[dict[str, object]] = []

    # Capture each server response body so we can assert on accepted/duplicates.
    class Tap(httpx.BaseTransport):
        def __init__(self) -> None:
            self.inner = httpx.HTTPTransport()

        def handle_request(self, request: httpx.Request) -> httpx.Response:
            resp = self.inner.handle_request(request)
            resp.read()
            results.append(resp.json())
            return resp

    with ForgeEval(api_key=API_KEY, base_url=BASE_URL, transport=Tap()) as client:
        for _ in range(2):
            with client.trace(model="sdk-it/model", id=tid) as t:
                t.input = {"q": "integration, contact a@b.com"}
                with t.span("step", kind="tool"):
                    pass
                t.output = {"a": "ok"}
            assert client.flush(timeout=15)

    assert len(results) == 2, caplog.text
    assert (results[0]["accepted"], results[0]["duplicates"]) == (1, 0)
    assert (results[1]["accepted"], results[1]["duplicates"]) == (0, 1)
    assert results[0]["rejected"] == []
