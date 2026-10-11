from forgeeval.api.openapi import CONTRACT_PATH, render


def test_committed_openapi_contract_is_current() -> None:
    assert CONTRACT_PATH.exists(), "run: uv run python -m forgeeval.api.openapi"
    assert CONTRACT_PATH.read_text(encoding="utf-8") == render(), (
        "docs/openapi.json is stale; run: uv run python -m forgeeval.api.openapi"
    )


def test_contract_exposes_trace_item_schema() -> None:
    import json

    spec = json.loads(render())
    schemas = spec["components"]["schemas"]
    assert {"TraceIn", "SpanIn", "PromptRef", "TraceBatchOut"} <= schemas.keys()
    assert "/v1/traces:batch" in spec["paths"]
