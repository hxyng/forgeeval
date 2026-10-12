"""Evaluator implementations, looked up by EvaluatorKind."""

from forgeeval.db.models import EvaluatorKind
from forgeeval.evaluators.base import EvalCase, Evaluator, ScoreResult
from forgeeval.evaluators.simple import Cost, ExactMatch, Latency

REGISTRY: dict[EvaluatorKind, Evaluator] = {
    EvaluatorKind.exact_match: ExactMatch(),
    EvaluatorKind.latency: Latency(),
    EvaluatorKind.cost: Cost(),
}

__all__ = ["REGISTRY", "EvalCase", "Evaluator", "ScoreResult"]
