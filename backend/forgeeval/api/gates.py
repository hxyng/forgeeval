"""Regression gates: turn a finished run into a pass / fail / inconclusive verdict for CI.

Two comparators:
- ``min_absolute``: the aggregate must be on the good side of ``threshold`` (>= when higher is
  better, <= for latency/cost). Example: p95 latency <= 1500 ms.
- ``max_drop_vs_baseline``: the aggregate may get worse than the baseline by at most
  ``threshold``. For the ``mean`` aggregate the drop must also be statistically significant
  (paired 95% CI, see stats.py), so a noisy run doesn't block a PR.

Inconclusive means "no evidence either way" (run failed or was cancelled, metric missing, no
baseline); CI decides whether that blocks. Every check is stored with its reason.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, status
from pydantic import BaseModel, Field
from sqlalchemy import exists, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from forgeeval.api.compare import (
    EvaluatorComparison,
    EvaluatorMetrics,
    compare_runs,
    get_run,
    run_metrics,
)
from forgeeval.api.registry import NAME_PATTERN
from forgeeval.api.runs import VersionRef, resolve_evaluator_version
from forgeeval.auth import AuthContext, Scope, require
from forgeeval.db.models import (
    EvalRun,
    Evaluator,
    EvaluatorVersion,
    GateAggregate,
    GateCheck,
    GateComparator,
    GateVerdict,
    PromptVersion,
    RegressionGate,
    RunEvaluator,
    RunStatus,
)
from forgeeval.db.session import get_session

router = APIRouter(prefix="/v1/gates", tags=["gates"])

DbSession = Annotated[Session, Depends(get_session)]
Name = Annotated[str, Path(pattern=NAME_PATTERN)]
FINISHED = {RunStatus.succeeded, RunStatus.partial, RunStatus.failed, RunStatus.cancelled}
USABLE = {RunStatus.succeeded, RunStatus.partial}


# ---------- contract ----------


class GateIn(BaseModel):
    name: str = Field(pattern=NAME_PATTERN)
    evaluator: VersionRef
    aggregate: GateAggregate
    comparator: GateComparator
    threshold: Decimal
    enabled: bool = True


class GatePatch(BaseModel):
    threshold: Decimal | None = None
    enabled: bool | None = None


class GateOut(BaseModel):
    id: uuid.UUID
    name: str
    evaluator: str
    evaluator_version: int
    evaluator_version_id: uuid.UUID
    higher_is_better: bool
    aggregate: GateAggregate
    comparator: GateComparator
    threshold: Decimal
    enabled: bool


class CheckIn(BaseModel):
    run_id: uuid.UUID
    baseline_run_id: uuid.UUID | None = Field(
        None,
        description="Defaults to the run's baseline, else the latest usable run of the same "
        "prompt over the same dataset version",
    )
    repo: str | None = Field(None, max_length=200, examples=["acme/support-bot"])
    pr_number: int | None = None
    commit_sha: str | None = Field(None, max_length=64)


class CheckOut(BaseModel):
    id: uuid.UUID
    gate: str
    verdict: GateVerdict
    reason: str
    aggregate: GateAggregate
    comparator: GateComparator
    threshold: Decimal
    observed: Decimal | None
    baseline: Decimal | None
    delta: Decimal | None
    run_id: uuid.UUID
    baseline_run_id: uuid.UUID | None
    repo: str | None
    pr_number: int | None
    commit_sha: str | None
    created_at: datetime


class CheckAllOut(BaseModel):
    verdict: GateVerdict
    checks: list[CheckOut]


# ---------- verdict logic (pure) ----------


@dataclass(frozen=True)
class Verdict:
    verdict: GateVerdict
    reason: str
    observed: Decimal | None = None
    baseline: Decimal | None = None
    delta: Decimal | None = None


def _aggregate(m: EvaluatorMetrics, agg: GateAggregate) -> float | None:
    return {
        GateAggregate.mean: m.mean,
        GateAggregate.p50: m.p50,
        GateAggregate.p95: m.p95,
        GateAggregate.pass_rate: m.pass_rate,
    }[agg]


def _d(x: float) -> Decimal:
    return Decimal(str(round(x, 6)))


def decide(
    gate: RegressionGate,
    higher_is_better: bool,
    candidate: EvaluatorMetrics | None,
    baseline: EvaluatorMetrics | None,
    comparison: EvaluatorComparison | None,
) -> Verdict:
    label = f"{gate.aggregate.value}"
    if candidate is None:
        return Verdict(GateVerdict.inconclusive, "run has no scores for this evaluator version")
    observed = _aggregate(candidate, gate.aggregate)
    if observed is None:
        return Verdict(GateVerdict.inconclusive, f"run has no {label} for this evaluator")
    threshold = float(gate.threshold)

    if gate.comparator is GateComparator.min_absolute:
        ok = observed >= threshold if higher_is_better else observed <= threshold
        op = ">=" if higher_is_better else "<="
        return Verdict(
            GateVerdict.passed if ok else GateVerdict.failed,
            f"{label} {observed:.4g} {'meets' if ok else 'misses'} required {op} {threshold:g}",
            observed=_d(observed),
        )

    if baseline is None:
        return Verdict(
            GateVerdict.inconclusive,
            "no comparable baseline run with scores for this evaluator",
            observed=_d(observed),
        )
    base = _aggregate(baseline, gate.aggregate)
    if base is None:
        return Verdict(GateVerdict.inconclusive, f"baseline has no {label}", observed=_d(observed))
    delta = observed - base
    worsening = -delta if higher_is_better else delta
    common = {"observed": _d(observed), "baseline": _d(base), "delta": _d(delta)}
    if worsening <= threshold:
        return Verdict(
            GateVerdict.passed,
            f"{label} {base:.4g} -> {observed:.4g}; worsened by {max(worsening, 0):.4g}, "
            f"allowed {threshold:g}",
            **common,
        )
    if (
        gate.aggregate is GateAggregate.mean
        and comparison is not None
        and not comparison.significant
    ):
        return Verdict(
            GateVerdict.passed,
            f"mean worsened by {worsening:.4g} (> {threshold:g}) but the paired 95% CI "
            f"[{comparison.ci95_low:.4g}, {comparison.ci95_high:.4g}] includes 0 "
            f"over {comparison.paired_n} items; treated as noise",
            **common,
        )
    return Verdict(
        GateVerdict.failed,
        f"{label} {base:.4g} -> {observed:.4g}; worsened by {worsening:.4g}, allowed {threshold:g}",
        **common,
    )


# ---------- persistence helpers ----------


def _gate(db: Session, ctx: AuthContext, name: str) -> RegressionGate:
    gate = db.scalar(
        select(RegressionGate).where(
            RegressionGate.project_id == ctx.project_id, RegressionGate.name == name
        )
    )
    if gate is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Gate '{name}' not found.")
    return gate


def _gate_out(db: Session, gate: RegressionGate) -> GateOut:
    name, version, hib = db.execute(
        select(Evaluator.name, EvaluatorVersion.version, Evaluator.higher_is_better)
        .join(EvaluatorVersion, EvaluatorVersion.evaluator_id == Evaluator.id)
        .where(EvaluatorVersion.id == gate.evaluator_version_id)
    ).one()
    return GateOut(
        id=gate.id,
        name=gate.name,
        evaluator=name,
        evaluator_version=version,
        evaluator_version_id=gate.evaluator_version_id,
        higher_is_better=hib,
        aggregate=gate.aggregate,
        comparator=gate.comparator,
        threshold=gate.threshold,
        enabled=gate.enabled,
    )


def _check_out(check: GateCheck, gate: RegressionGate) -> CheckOut:
    return CheckOut(
        id=check.id,
        gate=gate.name,
        verdict=check.verdict,
        reason=check.reason,
        aggregate=gate.aggregate,
        comparator=gate.comparator,
        threshold=gate.threshold,
        observed=check.observed if check.verdict is not GateVerdict.inconclusive else None,
        baseline=check.baseline,
        delta=check.delta,
        run_id=check.run_id,
        baseline_run_id=check.baseline_run_id,
        repo=check.repo,
        pr_number=check.pr_number,
        commit_sha=check.commit_sha,
        created_at=check.created_at,
    )


def find_baseline(db: Session, run: EvalRun, evaluator_version_id: uuid.UUID) -> EvalRun | None:
    """Latest usable earlier run of the same prompt over the same dataset version that also
    used this evaluator version."""
    prompt_name = db.scalar(
        select(PromptVersion.name).where(PromptVersion.id == run.prompt_version_id)
    )
    return db.scalar(
        select(EvalRun)
        .join(PromptVersion, PromptVersion.id == EvalRun.prompt_version_id)
        .where(
            EvalRun.project_id == run.project_id,
            EvalRun.dataset_version_id == run.dataset_version_id,
            EvalRun.id != run.id,
            EvalRun.id < run.id,  # UUIDv7: earlier
            EvalRun.status.in_(USABLE),
            PromptVersion.name == prompt_name,
            exists().where(
                RunEvaluator.run_id == EvalRun.id,
                RunEvaluator.evaluator_version_id == evaluator_version_id,
            ),
        )
        .order_by(EvalRun.id.desc())
        .limit(1)
    )


def evaluate_gate(db: Session, gate: RegressionGate, run: EvalRun, body: CheckIn) -> GateCheck:
    if run.status not in FINISHED:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Run is still {run.status.value}; check the gate once it finishes.",
        )
    hib = bool(
        db.scalar(
            select(Evaluator.higher_is_better)
            .join(EvaluatorVersion, EvaluatorVersion.evaluator_id == Evaluator.id)
            .where(EvaluatorVersion.id == gate.evaluator_version_id)
        )
    )
    baseline_run: EvalRun | None = None
    if run.status not in USABLE:
        result = Verdict(GateVerdict.inconclusive, f"run {run.status.value}; no usable scores")
    else:
        cand = {m.evaluator_version_id: m for m in run_metrics(db, run).evaluators}
        base_m: EvaluatorMetrics | None = None
        comparison: EvaluatorComparison | None = None
        if gate.comparator is GateComparator.max_drop_vs_baseline:
            baseline_id = body.baseline_run_id or run.baseline_run_id
            baseline_run = (
                get_run(db, run.project_id, baseline_id)
                if baseline_id
                else find_baseline(db, run, gate.evaluator_version_id)
            )
            if baseline_run is not None:
                cmp = compare_runs(db, run, baseline_run)
                comparison = next(
                    (
                        c
                        for c in cmp.evaluators
                        if c.candidate.evaluator_version_id == gate.evaluator_version_id
                    ),
                    None,
                )
                base_m = comparison.baseline if comparison else None
        result = decide(gate, hib, cand.get(gate.evaluator_version_id), base_m, comparison)

    check = GateCheck(
        gate_id=gate.id,
        run_id=run.id,
        baseline_run_id=baseline_run.id if baseline_run else None,
        observed=result.observed if result.observed is not None else Decimal(0),
        baseline=result.baseline,
        delta=result.delta,
        verdict=result.verdict,
        reason=result.reason,
        repo=body.repo,
        pr_number=body.pr_number,
        commit_sha=body.commit_sha,
    )
    db.add(check)
    db.flush()
    return check


# ---------- routes ----------


@router.post("", status_code=status.HTTP_201_CREATED)
def create_gate(
    body: GateIn, db: DbSession, ctx: Annotated[AuthContext, require(Scope.registry_write)]
) -> GateOut:
    ev = resolve_evaluator_version(db, ctx.project_id, body.evaluator)
    gate = RegressionGate(
        project_id=ctx.project_id,
        name=body.name,
        evaluator_version_id=ev.id,
        aggregate=body.aggregate,
        comparator=body.comparator,
        threshold=body.threshold,
        enabled=body.enabled,
    )
    db.add(gate)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, f"Gate '{body.name}' exists.") from exc
    return _gate_out(db, gate)


@router.get("")
def list_gates(
    db: DbSession, ctx: Annotated[AuthContext, require(Scope.project_read)]
) -> list[GateOut]:
    gates = db.scalars(
        select(RegressionGate)
        .where(RegressionGate.project_id == ctx.project_id)
        .order_by(RegressionGate.name)
    )
    return [_gate_out(db, g) for g in gates]


@router.patch("/{name}")
def update_gate(
    name: Name,
    body: GatePatch,
    db: DbSession,
    ctx: Annotated[AuthContext, require(Scope.registry_write)],
) -> GateOut:
    gate = _gate(db, ctx, name)
    if body.threshold is not None:
        gate.threshold = body.threshold
    if body.enabled is not None:
        gate.enabled = body.enabled
    db.commit()
    return _gate_out(db, gate)


@router.post("/{name}/check")
def check_gate(
    name: Name,
    body: CheckIn,
    db: DbSession,
    ctx: Annotated[AuthContext, require(Scope.gates_read)],
) -> CheckOut:
    gate = _gate(db, ctx, name)
    check = evaluate_gate(db, gate, get_run(db, ctx.project_id, body.run_id), body)
    db.commit()
    return _check_out(check, gate)


@router.post(":check")
def check_all_gates(
    body: CheckIn,
    db: DbSession,
    ctx: Annotated[AuthContext, require(Scope.gates_read)],
) -> CheckAllOut:
    """Check every enabled gate whose evaluator version the run used. Overall verdict: fail if
    any gate fails, else inconclusive if any is inconclusive or none apply, else pass."""
    run = get_run(db, ctx.project_id, body.run_id)
    used = select(RunEvaluator.evaluator_version_id).where(RunEvaluator.run_id == run.id)
    gates = list(
        db.scalars(
            select(RegressionGate)
            .where(
                RegressionGate.project_id == ctx.project_id,
                RegressionGate.enabled.is_(True),
                RegressionGate.evaluator_version_id.in_(used),
            )
            .order_by(RegressionGate.name)
        )
    )
    checks = [(evaluate_gate(db, g, run, body), g) for g in gates]
    db.commit()
    verdicts = {c.verdict for c, _ in checks}
    if GateVerdict.failed in verdicts:
        overall = GateVerdict.failed
    elif GateVerdict.inconclusive in verdicts or not checks:
        overall = GateVerdict.inconclusive
    else:
        overall = GateVerdict.passed
    return CheckAllOut(verdict=overall, checks=[_check_out(c, g) for c, g in checks])


@router.get("/{name}/checks")
def list_checks(
    name: Name, db: DbSession, ctx: Annotated[AuthContext, require(Scope.project_read)]
) -> list[CheckOut]:
    gate = _gate(db, ctx, name)
    rows = db.scalars(
        select(GateCheck)
        .where(GateCheck.gate_id == gate.id)
        .order_by(GateCheck.created_at.desc())
        .limit(100)
    )
    return [_check_out(c, gate) for c in rows]
