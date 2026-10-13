"""Typed configuration for each evaluator kind.

The registry validates configs against these models when a version is created, so workers can
trust ``evaluator_versions.config`` without re-checking it.
"""

from decimal import Decimal
from typing import Annotated, Any, Literal

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator


class _Config(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExactMatchConfig(_Config):
    kind: Literal["exact_match"] = "exact_match"
    field: str | None = Field(
        None, description="Dot path into output and expected_output, e.g. 'answer.label'"
    )
    case_sensitive: bool = True
    strip: bool = True


class JsonSchemaConfig(_Config):
    kind: Literal["json_schema"] = "json_schema"
    json_schema: dict[str, Any] = Field(alias="schema")
    field: str | None = Field(None, description="Dot path into output to validate")

    @field_validator("json_schema")
    @classmethod
    def _schema_is_valid(cls, value: dict[str, Any]) -> dict[str, Any]:
        try:
            Draft202012Validator.check_schema(value)
        except SchemaError as exc:
            raise ValueError(f"not a valid JSON Schema: {exc.message}") from exc
        return value


class LatencyConfig(_Config):
    kind: Literal["latency"] = "latency"
    max_ms: int = Field(gt=0)


class CostConfig(_Config):
    kind: Literal["cost"] = "cost"
    max_usd: Decimal = Field(gt=0)


class RetrievalRelevanceConfig(_Config):
    kind: Literal["retrieval_relevance"] = "retrieval_relevance"
    k: int = Field(5, ge=1, le=100)
    retrieved_field: str = Field(
        "retrieved_doc_ids",
        description="Dot path into output listing retrieved doc ids, in rank order",
    )
    relevant_field: str = Field(
        "relevant_doc_ids", description="Dot path into the item's context listing relevant doc ids"
    )


class LlmRubricConfig(_Config):
    kind: Literal["llm_rubric"] = "llm_rubric"
    rubric: str = Field(min_length=10)
    scale_min: int = 1
    scale_max: int = 5
    pass_threshold: float | None = None

    @model_validator(mode="after")
    def _scale(self) -> "LlmRubricConfig":
        if self.scale_max <= self.scale_min:
            raise ValueError("scale_max must be greater than scale_min")
        if self.pass_threshold is not None and not (
            self.scale_min <= self.pass_threshold <= self.scale_max
        ):
            raise ValueError("pass_threshold must lie within the scale")
        return self


EvaluatorConfig = Annotated[
    ExactMatchConfig
    | JsonSchemaConfig
    | LatencyConfig
    | CostConfig
    | RetrievalRelevanceConfig
    | LlmRubricConfig,
    Field(discriminator="kind"),
]

_adapter: TypeAdapter[EvaluatorConfig] = TypeAdapter(EvaluatorConfig)


def parse_config(kind: str, config: dict[str, Any]) -> EvaluatorConfig:
    """Validate ``config`` for evaluator ``kind``. Raises pydantic.ValidationError."""
    return _adapter.validate_python({**config, "kind": kind})


def dump_config(config: EvaluatorConfig) -> dict[str, Any]:
    """Canonical JSON form stored in evaluator_versions.config (no 'kind'; it lives on the row)."""
    data: dict[str, Any] = config.model_dump(mode="json", by_alias=True, exclude={"kind"})
    return data
