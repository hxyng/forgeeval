"""The contract every evaluator implements.

An evaluator is a pure function of (validated config, one model output and its test case).
It returns a score and, when the config defines a pass line, whether the case passed.
Evaluators raise TransientError/PermanentError from forgeeval.models_client for failures that
should be retried or dead-lettered; any other exception is treated as transient.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from forgeeval.evaluator_configs import EvaluatorConfig
from forgeeval.models_client import ModelClient


@dataclass(frozen=True)
class EvalCase:
    item_input: Any
    expected_output: Any
    context: dict[str, Any] | None
    output: Any
    latency_ms: int
    cost_usd: Decimal | None
    tags: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ScoreResult:
    value: Decimal
    passed: bool | None
    rationale: str | None = None
    judge_cost_usd: Decimal | None = None


class Evaluator(Protocol):
    def score(
        self, config: EvaluatorConfig, case: EvalCase, judge_model: str | None, models: ModelClient
    ) -> ScoreResult: ...


_MISSING = object()


def get_path(value: Any, path: str | None) -> Any:
    """Follow a dot path through dicts and list indexes; returns a sentinel when absent."""
    if not path:
        return value
    current = value
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return _MISSING
    return current


def is_missing(value: Any) -> bool:
    return value is _MISSING
