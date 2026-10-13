"""Validate SDK-built payloads against the TraceIn schema in docs/openapi.json."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from forgeeval_sdk import ForgeEval

from .conftest import Recorder

OPENAPI = Path(__file__).resolve().parents[2] / "docs" / "openapi.json"


@pytest.fixture(scope="module")
def trace_validator() -> Draft202012Validator:
    spec = json.loads(OPENAPI.read_text(encoding="utf-8"))
    # Make every "#/components/schemas/X" ref resolvable by publishing the whole spec as one
    # resource; refs are relative to the document root.
    resource = Resource.from_contents(spec, default_specification=DRAFT202012)
    registry: Registry[Any] = Registry().with_resource("urn:openapi", resource)
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$ref": "urn:openapi#/components/schemas/TraceIn",
    }
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, registry=registry)


def test_validator_actually_rejects_bad_payloads(trace_validator: Draft202012Validator) -> None:
    assert list(trace_validator.iter_errors({"id": "x"}))  # missing required fields
    bad_span = {
        "id": "t",
        "model": "m",
        "latency_ms": 1,
        "started_at": "2025-01-01T00:00:00+00:00",
        "spans": [{"id": "s", "kind": "nope", "name": "n", "started_at": "x", "duration_ms": 1}],
    }
    assert list(trace_validator.iter_errors(bad_span))  # enum on nested $ref is enforced


def test_sdk_payloads_match_schema(
    make_client: Callable[..., tuple[ForgeEval, Recorder]],
    trace_validator: Draft202012Validator,
) -> None:
    client, rec = make_client()
    with client.trace(model="openai/gpt-5-mini", prompt=("support-answer", 3)) as t:
        t.input = {"messages": [{"role": "user", "content": "mail a@b.com"}]}
        with t.span("kb.search", kind="retrieval") as s:
            s.attributes["doc_ids"] = ["d1", "d2"]
            with t.span("rerank", kind="tool"):
                pass
        with t.span("gen", kind="llm"):
            pass
        t.output = {"text": "hi"}
        t.usage(input_tokens=120, output_tokens=18, cost_usd=0.00021)
    with pytest.raises(RuntimeError), client.trace(model="m") as t2:
        t2.input = "x"
        raise RuntimeError("fail")
    with client.trace(model="m") as t3:
        t3.input = "z" * 400_000
    with client.trace(model="m"):
        pass  # no input/output/usage at all
    client.log_trace(
        "m",
        input="q",
        output="a",
        latency_ms=12,
        started_at=datetime(2025, 1, 1, tzinfo=UTC),
        cost_usd="0.0004",
        spans=[{"name": "x", "started_at": datetime(2025, 1, 1, tzinfo=UTC), "duration_ms": 3}],
    )
    assert client.flush(timeout=5)
    traces = [tr for batch in rec.batches() for tr in batch]
    assert len(traces) == 5
    for tr in traces:
        errors = [e.message for e in trace_validator.iter_errors(tr)]
        assert errors == []
    assert traces[0]["redaction"] == {"email": 1}
    assert traces[2]["redaction"]["truncated"] is True
