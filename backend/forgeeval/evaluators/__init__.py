"""Evaluator implementations, looked up by EvaluatorKind."""

from forgeeval.db.models import EvaluatorKind
from forgeeval.evaluators.base import EvalCase, Evaluator, ScoreResult
from forgeeval.evaluators.llm_rubric import LlmRubric
from forgeeval.evaluators.simple import Cost, ExactMatch, Latency
from forgeeval.evaluators.structured import JsonSchema, RetrievalRelevance

REGISTRY: dict[EvaluatorKind, Evaluator] = {
    EvaluatorKind.exact_match: ExactMatch(),
    EvaluatorKind.latency: Latency(),
    EvaluatorKind.cost: Cost(),
    EvaluatorKind.json_schema: JsonSchema(),
    EvaluatorKind.retrieval_relevance: RetrievalRelevance(),
    EvaluatorKind.llm_rubric: LlmRubric(),
}

__all__ = ["REGISTRY", "EvalCase", "Evaluator", "ScoreResult"]
