from decimal import Decimal
from typing import Any

import httpx
import pytest

from forgeeval.db.models import EvaluatorKind
from forgeeval.evaluator_configs import parse_config
from forgeeval.evaluators import REGISTRY, EvalCase
from forgeeval.models_client import (
    FakeModels,
    OpenAICompatible,
    PermanentError,
    Router,
    TransientError,
    render,
)


def test_render_fills_fields_and_keeps_unknown_placeholders() -> None:
    assert render("Q: {q} / {missing}", {"q": "hi"}) == "Q: hi / {missing}"
    assert render("Raw: {input}", ["a", 1]) == 'Raw: ["a", 1]'
    assert render("Ctx: {ctx} n={n}", {"ctx": {"b": 1}, "n": 3}) == 'Ctx: {"b": 1} n=3'


def test_render_leaves_json_examples_in_templates_alone() -> None:
    template = 'Reply as JSON like {"label": "billing"} or {"label": "bug"}. Ticket: {q}'
    out = render(template, {"q": "refund please"})
    assert (
        out == 'Reply as JSON like {"label": "billing"} or {"label": "bug"}. Ticket: refund please'
    )
    assert render("{ {q} }", {"q": "x"}) == "{ x }"


def openai(handler: Any, strip: bool = False) -> OpenAICompatible:
    return OpenAICompatible(
        "https://models.example/v1",
        "k",
        strip,
        httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_openai_compatible_success_and_provider_stripping() -> None:
    seen: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = req.read()
        seen["auth"] = req.headers["authorization"]
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "billing"}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 1},
            },
        )

    gen = openai(handler, strip=True).generate("openai/gpt-5-mini", "classify", {"temperature": 0})
    assert gen.output == {"text": "billing"} and gen.input_tokens == 12
    assert b'"model":"gpt-5-mini"' in seen["body"].replace(b" ", b"")
    assert seen["auth"] == "Bearer k"


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (httpx.Response(429), TransientError),
        (httpx.Response(503), TransientError),
        (httpx.Response(400, text="bad model"), PermanentError),
        (httpx.Response(200, json={"unexpected": True}), PermanentError),
    ],
)
def test_openai_compatible_classifies_errors(response: httpx.Response, error: type) -> None:
    with pytest.raises(error):
        openai(lambda _req: response).generate("m", "p", {})


def test_openai_compatible_timeout_is_transient() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=req)

    with pytest.raises(TransientError):
        openai(handler).generate("m", "p", {})


def test_router_requires_endpoint_for_real_models() -> None:
    router = Router(FakeModels(), None)
    assert router.generate("fake/oracle", "p", {}, expected={"a": 1}).output == {"a": 1}
    with pytest.raises(PermanentError, match="MODEL_BASE_URL"):
        router.generate("openai/gpt-5-mini", "p", {})


def test_fake_failure_injection() -> None:
    import random

    fake = FakeModels(random.Random(0))
    with pytest.raises(TransientError):
        fake.generate("fake/echo", "p", {"fail_rate": 1.0})
    with pytest.raises(PermanentError):
        fake.generate("fake/nope", "p", {})


def case(
    output: Any, expected: Any = None, latency: int = 100, cost: str | None = "0.002"
) -> EvalCase:
    return EvalCase(
        item_input={},
        expected_output=expected,
        context=None,
        output=output,
        latency_ms=latency,
        cost_usd=Decimal(cost) if cost else None,
    )


def score(kind: str, config: dict[str, Any], c: EvalCase) -> tuple[Decimal, bool | None]:
    r = REGISTRY[EvaluatorKind(kind)].score(parse_config(kind, config), c, None, FakeModels())
    return r.value, r.passed


def test_exact_match_paths_case_and_whitespace() -> None:
    cfg = {"field": "answer.label", "case_sensitive": False}
    assert score(
        "exact_match",
        cfg,
        case({"answer": {"label": " Billing "}}, {"answer": {"label": "billing"}}),
    ) == (1, True)
    assert score("exact_match", cfg, case({"answer": {}}, {"answer": {"label": "x"}})) == (0, False)
    assert score("exact_match", {}, case([1, 2], [1, 2])) == (1, True)
    with pytest.raises(PermanentError):
        score("exact_match", cfg, case({"answer": {"label": "x"}}, None))


def test_latency_and_cost() -> None:
    assert score("latency", {"max_ms": 500}, case({}, latency=120)) == (120, True)
    assert score("latency", {"max_ms": 500}, case({}, latency=900)) == (900, False)
    assert score("cost", {"max_usd": "0.001"}, case({}, cost="0.002")) == (Decimal("0.002"), False)
    assert score("cost", {"max_usd": "0.001"}, case({}, cost=None)) == (0, None)
