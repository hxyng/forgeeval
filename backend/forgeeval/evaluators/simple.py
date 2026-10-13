"""Deterministic evaluators that need no model call."""

from decimal import Decimal
from typing import Any

from forgeeval.evaluator_configs import (
    CostConfig,
    EvaluatorConfig,
    ExactMatchConfig,
    LatencyConfig,
)
from forgeeval.evaluators.base import EvalCase, ScoreResult, get_path, is_missing
from forgeeval.models_client import ModelClient, PermanentError

ONE, ZERO = Decimal(1), Decimal(0)


def _normalize(value: Any, cfg: ExactMatchConfig) -> Any:
    if isinstance(value, str):
        if cfg.strip:
            value = value.strip()
        if not cfg.case_sensitive:
            value = value.casefold()
    return value


class ExactMatch:
    def score(
        self, config: EvaluatorConfig, case: EvalCase, judge_model: str | None, models: ModelClient
    ) -> ScoreResult:
        assert isinstance(config, ExactMatchConfig)
        if case.expected_output is None:
            raise PermanentError("exact_match needs expected_output on the dataset item")
        expected = get_path(case.expected_output, config.field)
        if is_missing(expected):
            raise PermanentError(f"expected_output has no field {config.field!r}")
        actual = get_path(case.output, config.field)
        if is_missing(actual):
            return ScoreResult(ZERO, False, f"output has no field {config.field!r}")
        ok = _normalize(actual, config) == _normalize(expected, config)
        return ScoreResult(ONE if ok else ZERO, ok, None if ok else f"expected {expected!r}")


class Latency:
    def score(
        self, config: EvaluatorConfig, case: EvalCase, judge_model: str | None, models: ModelClient
    ) -> ScoreResult:
        assert isinstance(config, LatencyConfig)
        return ScoreResult(Decimal(case.latency_ms), case.latency_ms <= config.max_ms)


class Cost:
    def score(
        self, config: EvaluatorConfig, case: EvalCase, judge_model: str | None, models: ModelClient
    ) -> ScoreResult:
        assert isinstance(config, CostConfig)
        if case.cost_usd is None:
            return ScoreResult(ZERO, None, "model reported no cost")
        return ScoreResult(case.cost_usd, case.cost_usd <= config.max_usd)
