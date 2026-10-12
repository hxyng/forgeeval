import json
from decimal import Decimal
from typing import Any

import pytest

from forgeeval.db.models import EvaluatorKind
from forgeeval.evaluator_configs import parse_config
from forgeeval.evaluators import REGISTRY, EvalCase, ScoreResult
from forgeeval.evaluators.llm_rubric import LlmRubric
from forgeeval.evaluators.structured import JsonSchema, RetrievalRelevance
from forgeeval.models_client import FakeModels, Generation, PermanentError, TransientError

RUBRIC = "Is the answer polite and correct?"


def case(output: Any, **kw: Any) -> EvalCase:
    base: dict[str, Any] = {
        "item_input": {"q": "What is 2+2?"},
        "expected_output": None,
        "context": None,
        "latency_ms": 10,
        "cost_usd": None,
    }
    base.update(kw)
    return EvalCase(output=output, **base)


# ---------- json_schema ----------

SCHEMA = {
    "type": "object",
    "required": ["name", "age"],
    "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
}


def run_schema(output: Any, field: str | None = None) -> ScoreResult:
    cfg = parse_config("json_schema", {"schema": SCHEMA, "field": field})
    return JsonSchema().score(cfg, case(output), None, FakeModels())


def test_json_schema_passes_on_valid_object() -> None:
    r = run_schema({"name": "a", "age": 3})
    assert (r.value, r.passed) == (Decimal(1), True)


def test_json_schema_nested_field() -> None:
    assert run_schema({"data": {"person": {"name": "a", "age": 3}}}, "data.person").passed is True
    assert run_schema({"data": {"person": {"name": "a"}}}, "data.person").passed is False


def test_json_schema_parses_string_json() -> None:
    r = run_schema({"text": json.dumps({"name": "a", "age": 3})}, "text")
    assert r.passed is True


def test_json_schema_invalid_json_string() -> None:
    r = run_schema({"text": "not json {"}, "text")
    assert (r.value, r.passed, r.rationale) == (Decimal(0), False, "output is not valid JSON")


def test_json_schema_missing_field() -> None:
    r = run_schema({"other": 1}, "text")
    assert r.value == 0 and r.passed is False and "text" in (r.rationale or "")


def test_json_schema_failure_rationale_names_path_and_message() -> None:
    r = run_schema({"name": "a", "age": "old"})
    assert r.passed is False and r.value == 0
    assert r.rationale is not None and r.rationale.startswith("$.age:") and "old" in r.rationale


def test_json_schema_rationale_is_truncated() -> None:
    cfg = parse_config("json_schema", {"schema": {"const": "x"}})
    r = JsonSchema().score(cfg, case(json.dumps("y" * 1000)), None, FakeModels())
    assert r.rationale is not None and len(r.rationale) <= 300


# ---------- retrieval_relevance ----------


def run_retrieval(retrieved: Any, relevant: Any = ("a", "b", "c"), **cfg: Any) -> ScoreResult:
    config = parse_config("retrieval_relevance", cfg)
    ctx = None if relevant is None else {"relevant_doc_ids": list(relevant)}
    return RetrievalRelevance().score(
        config, case({"retrieved_doc_ids": retrieved}, context=ctx), None, FakeModels()
    )


def test_retrieval_recall_at_k() -> None:
    r = run_retrieval(["a", "x", "b", "y", "c"], k=5)
    assert r.value == Decimal(1) and r.passed is None
    assert r.rationale == "3/3 relevant docs in top 5"
    r = run_retrieval(["a", "x", "b", "y", "c"], k=3)
    assert r.value == Decimal(2) / Decimal(3)
    assert r.rationale == "2/3 relevant docs in top 3"
    assert run_retrieval(["x", "y", "a"], k=2).value == 0


def test_retrieval_duplicates_not_double_counted() -> None:
    r = run_retrieval(["a", "a", "a", "a"], k=5)
    assert r.value == Decimal(1) / Decimal(3)


def test_retrieval_missing_ground_truth_is_permanent() -> None:
    for relevant in (None, []):
        with pytest.raises(PermanentError):
            run_retrieval(["a"], relevant=relevant)


def test_retrieval_missing_or_non_list_retrieved_scores_zero() -> None:
    cfg = parse_config("retrieval_relevance", {})
    ctx = {"relevant_doc_ids": ["a"]}
    for out in ({"other": 1}, {"retrieved_doc_ids": "a"}):
        r = RetrievalRelevance().score(cfg, case(out, context=ctx), None, FakeModels())
        assert r.value == 0 and r.rationale


def test_retrieval_custom_fields() -> None:
    cfg = parse_config(
        "retrieval_relevance", {"retrieved_field": "r.ids", "relevant_field": "gold.ids"}
    )
    c = case({"r": {"ids": ["a"]}}, context={"gold": {"ids": ["a", "b"]}})
    assert RetrievalRelevance().score(cfg, c, None, FakeModels()).value == Decimal("0.5")


# ---------- llm_rubric ----------


class StubJudge:
    def __init__(self, reply: Any, cost: Decimal | None = Decimal("0.002")) -> None:
        self.reply = reply
        self.cost = cost
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def generate(
        self, model: str, prompt: str, params: dict[str, Any], expected: Any = None
    ) -> Generation:
        self.calls.append((model, prompt, params))
        return Generation(output=self.reply, latency_ms=1, cost_usd=self.cost)


def judge(
    reply: Any, judge_model: str | None = "fake/judge", **cfg: Any
) -> tuple[ScoreResult, StubJudge]:
    stub = StubJudge(reply)
    config = parse_config("llm_rubric", {"rubric": RUBRIC, **cfg})
    c = case({"text": "4"}, expected_output={"a": "4"})
    return LlmRubric().score(config, c, judge_model, stub), stub


def text(score: Any, reason: str = "ok") -> dict[str, str]:
    return {"text": json.dumps({"score": score, "reason": reason})}


def test_llm_rubric_normalizes_default_scale() -> None:
    for s, v in ((1, "0"), (3, "0.5"), (5, "1")):
        r, _ = judge(text(s))
        assert r.value == Decimal(v) and r.passed is None
        assert r.rationale == "ok" and r.judge_cost_usd == Decimal("0.002")


def test_llm_rubric_custom_scale_and_threshold() -> None:
    r, _ = judge(text(7), scale_min=0, scale_max=10, pass_threshold=7)
    assert r.value == Decimal("0.7") and r.passed is True
    r, _ = judge(text(6), scale_min=0, scale_max=10, pass_threshold=7)
    assert r.passed is False


def test_llm_rubric_tolerates_fences_and_chatter() -> None:
    r, _ = judge({"text": 'Sure!\n```json\n{"score": 5, "reason": "great"}\n```'})
    assert r.value == 1 and r.rationale == "great"
    r, _ = judge('{"score": 3, "reason": "x"}')  # plain string output
    assert r.value == Decimal("0.5")


@pytest.mark.parametrize(
    "reply",
    [
        {"text": "I cannot decide"},
        {"text": "{broken"},
        {"text": '{"reason": "no score"}'},
        {"text": '{"score": "high"}'},
        text(9),
        text(0),
        None,
        42,
    ],
)
def test_llm_rubric_bad_judge_reply_is_transient(reply: Any) -> None:
    with pytest.raises(TransientError):
        judge(reply)


def test_llm_rubric_without_judge_model_is_permanent() -> None:
    with pytest.raises(PermanentError):
        judge(text(3), judge_model=None)


def test_llm_rubric_prompt_contents_and_call_params() -> None:
    _, stub = judge(text(3), scale_min=1, scale_max=10)
    model, prompt, params = stub.calls[0]
    assert model == "fake/judge" and params == {"temperature": 0}
    assert RUBRIC in prompt and "1" in prompt and "10" in prompt
    assert "What is 2+2?" in prompt and '"a": "4"' in prompt and "Candidate output" in prompt
    assert '"score"' in prompt and "JSON only" in prompt


def test_llm_rubric_prompt_omits_expected_when_absent() -> None:
    stub = StubJudge(text(3))
    config = parse_config("llm_rubric", {"rubric": RUBRIC})
    LlmRubric().score(config, case("out"), "fake/judge", stub)
    assert "Expected output" not in stub.calls[0][1]


# ---------- fake/judge and registry ----------


def test_fake_judge_defaults_and_params() -> None:
    fake = FakeModels()
    assert json.loads(fake.generate("fake/judge", "p", {}).output["text"])["score"] == 4
    out = fake.generate("fake/judge", "p", {"judge_score": 2}).output["text"]
    assert json.loads(out) == {"score": 2, "reason": "fake judge"}
    with pytest.raises(PermanentError):
        fake.generate("fake/judge", "p", {"permanent_fail": True})
    with pytest.raises(TransientError):
        fake.generate("fake/judge", "p", {"fail_rate": 1})


def test_fake_judge_through_registry() -> None:
    config = parse_config("llm_rubric", {"rubric": RUBRIC, "pass_threshold": 4})
    r = REGISTRY[EvaluatorKind.llm_rubric].score(config, case("x"), "fake/judge", FakeModels())
    assert r.value == Decimal("0.75") and r.passed is True
