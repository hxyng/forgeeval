from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from forgeeval_sdk import ForgeEval
from forgeeval_sdk.payload import MAX_PAYLOAD_BYTES

from .conftest import Recorder

MakeClient = Callable[..., tuple[ForgeEval, Recorder]]


def one_trace(client: ForgeEval, rec: Recorder) -> dict[str, Any]:
    assert client.flush(timeout=5)
    batches = rec.batches()
    assert len(batches) == 1 and len(batches[0]) == 1
    return batches[0][0]


def test_basic_trace_fields(make_client: MakeClient) -> None:
    client, rec = make_client()
    with client.trace(model="openai/gpt-5-mini", prompt=("support-answer", 3)) as t:
        t.input = {"q": "hi"}
        t.output = {"a": "hello"}
        t.usage(input_tokens=120, output_tokens=18, cost_usd=0.00021)
    p = one_trace(client, rec)
    assert p["id"] == t.id and len(t.id) == 32
    assert p["model"] == "openai/gpt-5-mini"
    assert p["status"] == "ok"
    assert p["prompt"] == {"name": "support-answer", "version": 3}
    assert (p["input_tokens"], p["output_tokens"], p["cost_usd"]) == (120, 18, 0.00021)
    assert p["input"] == {"q": "hi"} and p["output"] == {"a": "hello"}
    assert isinstance(p["latency_ms"], int) and p["latency_ms"] >= 0
    started = datetime.fromisoformat(p["started_at"])
    assert started.tzinfo is not None and started.utcoffset() == UTC.utcoffset(None)
    req = rec.requests[0]
    assert req.headers["authorization"] == "Bearer fe_abcd1234_secretsecret"
    assert req.url.path == "/v1/traces:batch"


def test_error_status_and_exception_reraised_unchanged(make_client: MakeClient) -> None:
    client, rec = make_client()
    boom = ValueError("boom")
    with pytest.raises(ValueError) as info, client.trace(model="m") as t:
        t.input = "x"
        raise boom
    assert info.value is boom
    assert one_trace(client, rec)["status"] == "error"


def test_span_parent_ids(make_client: MakeClient) -> None:
    client, rec = make_client()
    with client.trace(model="m") as t:
        with t.span("a", kind="retrieval") as a:
            a.attributes["doc_ids"] = [1, 2]
            with t.span("a.child", kind="tool") as c:
                pass
            with t.span("a.child2") as c2:
                pass
        with t.span("b", kind="llm") as b:
            pass
    spans = one_trace(client, rec)["spans"]
    by_name = {s["name"]: s for s in spans}
    assert [s["name"] for s in spans] == ["a", "a.child", "a.child2", "b"]
    assert by_name["a"]["parent_id"] is None
    assert by_name["a.child"]["parent_id"] == a.id
    assert by_name["a.child2"]["parent_id"] == a.id
    assert by_name["b"]["parent_id"] is None
    assert by_name["a"]["attributes"] == {"doc_ids": [1, 2]}
    assert by_name["a"]["kind"] == "retrieval"
    assert {c.id, c2.id, b.id} <= {s["id"] for s in spans}
    ids = [s["id"] for s in spans]
    assert len(set(ids)) == len(ids)


def test_span_error_marks_attribute_and_pops_stack(make_client: MakeClient) -> None:
    client, rec = make_client()
    with client.trace(model="m") as t:
        with pytest.raises(KeyError), t.span("bad") as s:
            raise KeyError("k")
        with t.span("after"):
            pass
    spans = one_trace(client, rec)["spans"]
    assert spans[0]["attributes"]["error"] == "KeyError"
    assert spans[1]["parent_id"] is None
    assert s.id == spans[0]["id"]


def test_unknown_span_kind_falls_back_to_other(make_client: MakeClient) -> None:
    client, rec = make_client()
    with client.trace(model="m") as t, t.span("x", kind="bogus"):
        pass
    assert one_trace(client, rec)["spans"][0]["kind"] == "other"


def test_redaction_applied_and_counted(make_client: MakeClient) -> None:
    client, rec = make_client()
    with client.trace(model="m") as t:
        t.input = {"messages": [{"role": "user", "content": "I am a@b.com, call 415-555-2671"}]}
        with t.span("kb", kind="retrieval") as s:
            s.attributes["owner"] = "c@d.org"
        t.output = "ok sk-abcdefghijklmnopqrstuv"
    p = one_trace(client, rec)
    dumped = json.dumps(p)
    assert "a@b.com" not in dumped and "c@d.org" not in dumped and "415-555" not in dumped
    assert p["redaction"] == {"email": 2, "phone": 1, "secret": 1}
    assert p["spans"][0]["attributes"]["owner"] == "[REDACTED:email]"


def test_custom_redactors_replace_defaults(make_client: MakeClient) -> None:
    client, rec = make_client(redactors=[("acct", r"ACCT-\d+")])
    with client.trace(model="m") as t:
        t.input = "ACCT-42 and a@b.com"
    p = one_trace(client, rec)
    assert p["input"] == "[REDACTED:acct] and a@b.com"
    assert p["redaction"] == {"acct": 1}


def test_truncation_of_oversized_payload(make_client: MakeClient) -> None:
    client, rec = make_client()
    with client.trace(model="m") as t:
        t.input = {"small": "keep me", "big": "x" * 400_000}
        t.output = "y" * 1000
    p = one_trace(client, rec)
    assert p["redaction"]["truncated"] is True
    assert p["input"]["small"] == "keep me"
    assert p["input"]["big"].endswith("...[truncated]")
    assert p["output"] == "y" * 1000
    assert len(json.dumps([p["input"], p["output"]], default=str).encode()) <= MAX_PAYLOAD_BYTES


def test_truncation_multibyte(make_client: MakeClient) -> None:
    client, rec = make_client()
    with client.trace(model="m") as t:
        t.input = "é中" * 100_000
    p = one_trace(client, rec)
    assert p["redaction"]["truncated"] is True
    assert len(json.dumps([p["input"], p["output"]], default=str).encode()) <= MAX_PAYLOAD_BYTES


def test_small_payload_not_marked_truncated(make_client: MakeClient) -> None:
    client, rec = make_client()
    with client.trace(model="m") as t:
        t.input = "short"
    assert "truncated" not in one_trace(client, rec)["redaction"]


def test_log_trace(make_client: MakeClient) -> None:
    client, rec = make_client()
    start = datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC)
    tid = client.log_trace(
        "m",
        input="q",
        output="a",
        latency_ms=250,
        started_at=start,
        status="error",
        spans=[
            {"id": "s1", "name": "one", "kind": "llm", "started_at": start, "duration_ms": 5},
            {"name": "two", "parent_id": "s1", "started_at": start, "duration_ms": 1},
        ],
    )
    p = one_trace(client, rec)
    assert p["id"] == tid and p["latency_ms"] == 250 and p["status"] == "error"
    assert p["started_at"] == "2025-01-02T03:04:05+00:00"
    assert p["spans"][1]["parent_id"] == "s1" and p["spans"][1]["kind"] == "other"


def test_env_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORGEEVAL_API_KEY", "fe_envkey00_abcdefgh")
    monkeypatch.setenv("FORGEEVAL_BASE_URL", "http://example.test:9000/")
    rec = Recorder()
    with ForgeEval(transport=httpx.MockTransport(rec)) as client:
        client.log_trace("m")
    assert str(rec.requests[0].url) == "http://example.test:9000/v1/traces:batch"
    assert rec.requests[0].headers["authorization"] == "Bearer fe_envkey00_abcdefgh"


def test_missing_key_disables_without_raising(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("FORGEEVAL_API_KEY", raising=False)
    with ForgeEval() as client, client.trace(model="m") as t:
        t.input = "x"
    assert client.stats()["queued"] == 0
    assert "tracing is disabled" in caplog.text
