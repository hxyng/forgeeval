"""PostgreSQL schema v1. Mirrors the ERD in docs/data-model (21 tables, 5 domains)."""

import enum
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.orm import Mapped, mapped_column

from forgeeval.db.base import Base, CreatedAt, pk

Money = Numeric(12, 6)


def pg_enum(cls: type[enum.Enum]) -> Enum:
    return Enum(cls, name=cls.__name__.lower(), values_callable=lambda e: [m.value for m in e])


def fk(target: str, *, nullable: bool = False, ondelete: str | None = None) -> Any:
    return mapped_column(ForeignKey(target, ondelete=ondelete), nullable=nullable)


# ---------- enums ----------


class MemberRole(enum.StrEnum):
    owner = "owner"
    admin = "admin"
    member = "member"
    viewer = "viewer"


class TraceStatus(enum.StrEnum):
    ok = "ok"
    error = "error"


class SpanKind(enum.StrEnum):
    llm = "llm"
    retrieval = "retrieval"
    tool = "tool"
    other = "other"


class EvaluatorKind(enum.StrEnum):
    exact_match = "exact_match"
    json_schema = "json_schema"
    latency = "latency"
    cost = "cost"
    retrieval_relevance = "retrieval_relevance"
    llm_rubric = "llm_rubric"


class RunTrigger(enum.StrEnum):
    manual = "manual"
    replay = "replay"
    ci = "ci"


class RunStatus(enum.StrEnum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    partial = "partial"
    failed = "failed"
    cancelled = "cancelled"


class ItemStatus(enum.StrEnum):
    queued = "queued"
    leased = "leased"
    retry_wait = "retry_wait"
    succeeded = "succeeded"
    dead_lettered = "dead_lettered"
    cancelled = "cancelled"


class GateAggregate(enum.StrEnum):
    mean = "mean"
    p50 = "p50"
    p95 = "p95"
    pass_rate = "pass_rate"


class GateComparator(enum.StrEnum):
    min_absolute = "min_absolute"
    max_drop_vs_baseline = "max_drop_vs_baseline"


class GateVerdict(enum.StrEnum):
    passed = "pass"
    failed = "fail"
    inconclusive = "inconclusive"


# ---------- tenancy & access ----------


class Organization(CreatedAt, Base):
    __tablename__ = "organizations"
    id: Mapped[uuid.UUID] = pk()
    name: Mapped[str] = mapped_column(Text)
    slug: Mapped[str] = mapped_column(Text, unique=True)


class User(CreatedAt, Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = pk()
    email: Mapped[str] = mapped_column(CITEXT, unique=True)
    display_name: Mapped[str | None] = mapped_column(Text)
    auth_subject: Mapped[str | None] = mapped_column(Text, unique=True)


class Membership(CreatedAt, Base):
    __tablename__ = "memberships"
    org_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[MemberRole] = mapped_column(pg_enum(MemberRole))


class Project(CreatedAt, Base):
    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("org_id", "slug"),)
    id: Mapped[uuid.UUID] = pk()
    org_id: Mapped[uuid.UUID] = fk("organizations.id", ondelete="CASCADE")
    name: Mapped[str] = mapped_column(Text)
    slug: Mapped[str] = mapped_column(Text)
    retention_days: Mapped[int] = mapped_column(Integer, server_default=text("90"))


class ApiKey(CreatedAt, Base):
    __tablename__ = "api_keys"
    __table_args__ = (
        Index("ix_api_keys_active", "project_id", postgresql_where=text("revoked_at IS NULL")),
    )
    id: Mapped[uuid.UUID] = pk()
    project_id: Mapped[uuid.UUID] = fk("projects.id", ondelete="CASCADE")
    key_prefix: Mapped[str] = mapped_column(Text, unique=True)
    key_hash: Mapped[bytes] = mapped_column(LargeBinary)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ---------- ingestion ----------


class PromptVersion(CreatedAt, Base):
    __tablename__ = "prompt_versions"
    __table_args__ = (UniqueConstraint("project_id", "name", "version"),)
    id: Mapped[uuid.UUID] = pk()
    project_id: Mapped[uuid.UUID] = fk("projects.id", ondelete="CASCADE")
    name: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer)
    template: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    params: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    content_hash: Mapped[str] = mapped_column(Text)
    git_sha: Mapped[str | None] = mapped_column(Text)


class Trace(Base):
    __tablename__ = "traces"
    __table_args__ = (
        UniqueConstraint("project_id", "idempotency_key"),
        Index("ix_traces_project_started", "project_id", text("started_at DESC")),
        Index(
            "ix_traces_project_prompt_started",
            "project_id",
            "prompt_version_id",
            text("started_at DESC"),
        ),
    )
    id: Mapped[uuid.UUID] = pk()
    project_id: Mapped[uuid.UUID] = fk("projects.id", ondelete="CASCADE")
    idempotency_key: Mapped[str] = mapped_column(Text)
    prompt_version_id: Mapped[uuid.UUID | None] = fk("prompt_versions.id", nullable=True)
    model: Mapped[str] = mapped_column(Text)
    input: Mapped[dict[str, Any] | None]
    output: Mapped[dict[str, Any] | None]
    payload_s3_key: Mapped[str | None] = mapped_column(Text)
    status: Mapped[TraceStatus] = mapped_column(pg_enum(TraceStatus))
    latency_ms: Mapped[int] = mapped_column(Integer)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[Decimal | None] = mapped_column(Money)
    redaction: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    otel_trace_id: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class Span(Base):
    __tablename__ = "spans"
    __table_args__ = (Index("ix_spans_trace_id", "trace_id"),)
    id: Mapped[uuid.UUID] = pk()
    trace_id: Mapped[uuid.UUID] = fk("traces.id", ondelete="CASCADE")
    parent_span_id: Mapped[uuid.UUID | None] = fk("spans.id", nullable=True, ondelete="CASCADE")
    kind: Mapped[SpanKind] = mapped_column(pg_enum(SpanKind))
    name: Mapped[str] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int] = mapped_column(Integer)
    attributes: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))


# ---------- evaluation registry ----------


class Dataset(CreatedAt, Base):
    __tablename__ = "datasets"
    __table_args__ = (UniqueConstraint("project_id", "name"),)
    id: Mapped[uuid.UUID] = pk()
    project_id: Mapped[uuid.UUID] = fk("projects.id", ondelete="CASCADE")
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)


class DatasetVersion(CreatedAt, Base):
    __tablename__ = "dataset_versions"
    __table_args__ = (UniqueConstraint("dataset_id", "version"),)
    id: Mapped[uuid.UUID] = pk()
    dataset_id: Mapped[uuid.UUID] = fk("datasets.id", ondelete="CASCADE")
    version: Mapped[int] = mapped_column(Integer)
    item_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    content_hash: Mapped[str] = mapped_column(Text)
    frozen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DatasetItem(CreatedAt, Base):
    __tablename__ = "dataset_items"
    __table_args__ = (Index("ix_dataset_items_version", "dataset_version_id"),)
    id: Mapped[uuid.UUID] = pk()
    dataset_version_id: Mapped[uuid.UUID] = fk("dataset_versions.id", ondelete="CASCADE")
    source_trace_id: Mapped[uuid.UUID | None] = fk("traces.id", nullable=True, ondelete="SET NULL")
    input: Mapped[dict[str, Any]]
    expected_output: Mapped[dict[str, Any] | None]
    context: Mapped[dict[str, Any] | None]
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'"))


class Evaluator(CreatedAt, Base):
    __tablename__ = "evaluators"
    __table_args__ = (UniqueConstraint("project_id", "name"),)
    id: Mapped[uuid.UUID] = pk()
    project_id: Mapped[uuid.UUID] = fk("projects.id", ondelete="CASCADE")
    name: Mapped[str] = mapped_column(Text)
    kind: Mapped[EvaluatorKind] = mapped_column(pg_enum(EvaluatorKind))
    higher_is_better: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))


class EvaluatorVersion(CreatedAt, Base):
    __tablename__ = "evaluator_versions"
    __table_args__ = (UniqueConstraint("evaluator_id", "version"),)
    id: Mapped[uuid.UUID] = pk()
    evaluator_id: Mapped[uuid.UUID] = fk("evaluators.id", ondelete="CASCADE")
    version: Mapped[int] = mapped_column(Integer)
    config: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    judge_model: Mapped[str | None] = mapped_column(Text)
    config_hash: Mapped[str] = mapped_column(Text)


# ---------- execution ----------


class EvalRun(CreatedAt, Base):
    __tablename__ = "eval_runs"
    __table_args__ = (
        Index("ix_eval_runs_project_created", "project_id", text("created_at DESC")),
        Index(
            "ix_eval_runs_active",
            "status",
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
        CheckConstraint(
            "baseline_run_id IS NULL OR baseline_run_id <> id", name="not_self_baseline"
        ),
    )
    id: Mapped[uuid.UUID] = pk()
    project_id: Mapped[uuid.UUID] = fk("projects.id", ondelete="CASCADE")
    dataset_version_id: Mapped[uuid.UUID] = fk("dataset_versions.id")
    prompt_version_id: Mapped[uuid.UUID] = fk("prompt_versions.id")
    baseline_run_id: Mapped[uuid.UUID | None] = fk("eval_runs.id", nullable=True)
    trigger: Mapped[RunTrigger] = mapped_column(pg_enum(RunTrigger))
    status: Mapped[RunStatus] = mapped_column(
        pg_enum(RunStatus), server_default=RunStatus.queued.value
    )
    max_failure_ratio: Mapped[Decimal] = mapped_column(Numeric(4, 3), server_default="0.02")
    total_items: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    succeeded_items: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    failed_items: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    cost_usd: Mapped[Decimal] = mapped_column(Money, server_default="0")
    git_sha: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = fk("users.id", nullable=True, ondelete="SET NULL")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RunEvaluator(Base):
    __tablename__ = "run_evaluators"
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("eval_runs.id", ondelete="CASCADE"), primary_key=True
    )
    evaluator_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("evaluator_versions.id"), primary_key=True
    )


class RunItem(CreatedAt, Base):
    __tablename__ = "run_items"
    __table_args__ = (
        UniqueConstraint("run_id", "dataset_item_id"),
        Index("ix_run_items_run_status", "run_id", "status"),
        Index(
            "ix_run_items_lease_expiry",
            "lease_expires_at",
            postgresql_where=text("status = 'leased'"),
        ),
    )
    id: Mapped[uuid.UUID] = pk()
    run_id: Mapped[uuid.UUID] = fk("eval_runs.id", ondelete="CASCADE")
    dataset_item_id: Mapped[uuid.UUID] = fk("dataset_items.id")
    status: Mapped[ItemStatus] = mapped_column(
        pg_enum(ItemStatus), server_default=ItemStatus.queued.value
    )
    attempt: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    leased_by: Mapped[str | None] = mapped_column(Text)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    output: Mapped[dict[str, Any] | None]
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[Decimal | None] = mapped_column(Money)
    last_error: Mapped[str | None] = mapped_column(Text)


class Score(CreatedAt, Base):
    __tablename__ = "scores"
    __table_args__ = (
        UniqueConstraint("run_item_id", "evaluator_version_id"),
        Index(
            "ix_scores_evaluator_item",
            "evaluator_version_id",
            "run_item_id",
            postgresql_include=["value"],
        ),
    )
    id: Mapped[uuid.UUID] = pk()
    run_item_id: Mapped[uuid.UUID] = fk("run_items.id", ondelete="CASCADE")
    evaluator_version_id: Mapped[uuid.UUID] = fk("evaluator_versions.id")
    value: Mapped[Decimal] = mapped_column(Numeric)
    passed: Mapped[bool | None] = mapped_column(Boolean)
    rationale: Mapped[str | None] = mapped_column(Text)
    judge_cost_usd: Mapped[Decimal | None] = mapped_column(Money)


class DeadLetter(CreatedAt, Base):
    __tablename__ = "dead_letters"
    id: Mapped[uuid.UUID] = pk()
    run_item_id: Mapped[uuid.UUID] = fk("run_items.id", ondelete="CASCADE")
    error_class: Mapped[str] = mapped_column(Text)
    error_detail: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict[str, Any]]
    replayed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ---------- gating & reporting ----------


class RegressionGate(CreatedAt, Base):
    __tablename__ = "regression_gates"
    id: Mapped[uuid.UUID] = pk()
    project_id: Mapped[uuid.UUID] = fk("projects.id", ondelete="CASCADE")
    name: Mapped[str] = mapped_column(Text)
    evaluator_version_id: Mapped[uuid.UUID] = fk("evaluator_versions.id")
    aggregate: Mapped[GateAggregate] = mapped_column(pg_enum(GateAggregate))
    comparator: Mapped[GateComparator] = mapped_column(pg_enum(GateComparator))
    threshold: Mapped[Decimal] = mapped_column(Numeric)
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))


class GateCheck(CreatedAt, Base):
    __tablename__ = "gate_checks"
    __table_args__ = (Index("ix_gate_checks_gate_created", "gate_id", text("created_at DESC")),)
    id: Mapped[uuid.UUID] = pk()
    gate_id: Mapped[uuid.UUID] = fk("regression_gates.id", ondelete="CASCADE")
    run_id: Mapped[uuid.UUID] = fk("eval_runs.id", ondelete="CASCADE")
    baseline_run_id: Mapped[uuid.UUID | None] = fk("eval_runs.id", nullable=True)
    observed: Mapped[Decimal] = mapped_column(Numeric)
    baseline: Mapped[Decimal | None] = mapped_column(Numeric)
    delta: Mapped[Decimal | None] = mapped_column(Numeric)
    verdict: Mapped[GateVerdict] = mapped_column(pg_enum(GateVerdict))
    repo: Mapped[str | None] = mapped_column(Text)
    pr_number: Mapped[int | None] = mapped_column(Integer)
    commit_sha: Mapped[str | None] = mapped_column(Text)


class Report(CreatedAt, Base):
    __tablename__ = "reports"
    id: Mapped[uuid.UUID] = pk()
    run_id: Mapped[uuid.UUID] = fk("eval_runs.id", ondelete="CASCADE")
    format: Mapped[str] = mapped_column(Text)
    s3_key: Mapped[str] = mapped_column(Text)
    bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(Text)
