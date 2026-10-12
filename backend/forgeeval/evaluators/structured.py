"""Deterministic evaluators over structured output: schema validity and retrieval recall."""

import json
from decimal import Decimal
from typing import Any

from jsonschema import Draft202012Validator

from forgeeval.evaluator_configs import (
    EvaluatorConfig,
    JsonSchemaConfig,
    RetrievalRelevanceConfig,
)
from forgeeval.evaluators.base import EvalCase, ScoreResult, get_path, is_missing
from forgeeval.models_client import ModelClient, PermanentError

ONE, ZERO = Decimal(1), Decimal(0)
MAX_RATIONALE = 300


def _json_path(parts: Any) -> str:
    return "$" + "".join(f"[{p}]" if isinstance(p, int) else f".{p}" for p in parts)


class JsonSchema:
    def score(
        self, config: EvaluatorConfig, case: EvalCase, judge_model: str | None, models: ModelClient
    ) -> ScoreResult:
        assert isinstance(config, JsonSchemaConfig)
        target = get_path(case.output, config.field)
        if is_missing(target):
            return ScoreResult(ZERO, False, f"output has no field {config.field!r}")
        if isinstance(target, str):
            try:
                target = json.loads(target)
            except ValueError:
                return ScoreResult(ZERO, False, "output is not valid JSON")
        validator = Draft202012Validator(config.json_schema)
        error = next(iter(validator.iter_errors(target)), None)
        if error is None:
            return ScoreResult(ONE, True)
        rationale = f"{_json_path(error.absolute_path)}: {error.message}"
        return ScoreResult(ZERO, False, rationale[:MAX_RATIONALE])


def _hashable(value: Any) -> bool:
    try:
        hash(value)
    except TypeError:
        return False
    return True


def _as_set(items: list[Any]) -> set[Any]:
    # Unhashable entries (dicts, lists) are compared by their JSON form.
    return {i if _hashable(i) else json.dumps(i, sort_keys=True, default=str) for i in items}


class RetrievalRelevance:
    def score(
        self, config: EvaluatorConfig, case: EvalCase, judge_model: str | None, models: ModelClient
    ) -> ScoreResult:
        assert isinstance(config, RetrievalRelevanceConfig)
        relevant_raw = get_path(case.context or {}, config.relevant_field)
        if is_missing(relevant_raw) or not isinstance(relevant_raw, list) or not relevant_raw:
            raise PermanentError(
                f"dataset item context has no non-empty list at {config.relevant_field!r}"
            )
        retrieved = get_path(case.output, config.retrieved_field)
        if is_missing(retrieved) or not isinstance(retrieved, list):
            return ScoreResult(ZERO, None, f"output has no list at {config.retrieved_field!r}")
        relevant = _as_set(relevant_raw)
        hits = len(_as_set(retrieved[: config.k]) & relevant)
        return ScoreResult(
            Decimal(hits) / Decimal(len(relevant)),
            None,
            f"{hits}/{len(relevant)} relevant docs in top {config.k}",
        )
