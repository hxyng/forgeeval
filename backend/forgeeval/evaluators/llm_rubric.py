"""LLM-as-judge: score one output against a rubric using a judge model."""

import json
from decimal import Decimal
from typing import Any

from forgeeval.evaluator_configs import EvaluatorConfig, LlmRubricConfig
from forgeeval.evaluators.base import EvalCase, ScoreResult
from forgeeval.models_client import ModelClient, PermanentError, TransientError


def _dump(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, default=str)


def build_prompt(config: LlmRubricConfig, case: EvalCase) -> str:
    parts = [
        "You are a strict evaluator. Grade the candidate output using the rubric.",
        f"Rubric:\n{config.rubric}",
        f"Scale: integer from {config.scale_min} (worst) to {config.scale_max} (best).",
        f"Input:\n{_dump(case.item_input)}",
    ]
    if case.expected_output is not None:
        parts.append(f"Expected output:\n{_dump(case.expected_output)}")
    parts.append(f"Candidate output:\n{_dump(case.output)}")
    parts.append(
        'Respond with JSON only, no other text: {"score": <int>, "reason": "<one sentence>"}'
    )
    return "\n\n".join(parts)


def _first_json_object(text: str) -> dict[str, Any] | None:
    decoder = json.JSONDecoder()
    start = text.find("{")
    while start != -1:
        try:
            obj, _ = decoder.raw_decode(text, start)
        except ValueError:
            start = text.find("{", start + 1)
            continue
        if isinstance(obj, dict):
            return obj
        start = text.find("{", start + 1)
    return None


class LlmRubric:
    def score(
        self, config: EvaluatorConfig, case: EvalCase, judge_model: str | None, models: ModelClient
    ) -> ScoreResult:
        assert isinstance(config, LlmRubricConfig)
        if not judge_model:
            raise PermanentError("llm_rubric needs a judge_model on the evaluator version")
        gen = models.generate(judge_model, build_prompt(config, case), {"temperature": 0})
        raw = gen.output
        text = raw["text"] if isinstance(raw, dict) and "text" in raw else raw
        verdict = _first_json_object(text) if isinstance(text, str) else None
        if verdict is None:
            raise TransientError("judge reply contained no JSON object")
        score = verdict.get("score")
        if isinstance(score, bool) or not isinstance(score, int | float):
            raise TransientError("judge reply has no numeric score")
        if not config.scale_min <= score <= config.scale_max:
            raise TransientError(
                f"judge score {score} outside scale {config.scale_min}..{config.scale_max}"
            )
        span = Decimal(config.scale_max - config.scale_min)
        value = (Decimal(str(score)) - config.scale_min) / span
        passed = None if config.pass_threshold is None else score >= config.pass_threshold
        reason = verdict.get("reason")
        return ScoreResult(value, passed, reason if isinstance(reason, str) else None, gen.cost_usd)
