"""Run metrics and run-vs-baseline comparison.

Metrics aggregate one run's scores per evaluator version. A comparison pairs two runs over the
same dataset version item by item, so it can say whether a change exceeds noise (see stats.py)
and list exactly which cases flipped from pass to fail.
"""

import uuid
from collections import defaultdict
from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, aliased

from forgeeval.auth import AuthContext, Scope, require
from forgeeval.db.models import (
    EvalRun,
    Evaluator,
    EvaluatorKind,
    EvaluatorVersion,
    RunItem,
    RunStatus,
    Score,
)
from forgeeval.db.session import get_session
from forgeeval.stats import PairedDelta, direction, paired_delta

router = APIRouter(prefix="/v1/runs", tags=["compare"])

DbSession = Annotated[Session, Depends(get_session)]
Reader = Annotated[AuthContext, require(Scope.project_read)]
TERMINAL = {RunStatus.succeeded, RunStatus.partial, RunStatus.failed, RunStatus.cancelled}
MAX_FLIPS = 50


class EvaluatorMetrics(BaseModel):
    evaluator: str
    version: int
    evaluator_version_id: uuid.UUID
    kind: EvaluatorKind
    higher_is_better: bool
    n: int
    mean: float | None
    p50: float | None
    p95: float | None
    pass_rate: float | None
    pass_n: int


class RunMetrics(BaseModel):
    run_id: uuid.UUID
    status: RunStatus
    complete: bool
    evaluators: list[EvaluatorMetrics]


class EvaluatorComparison(BaseModel):
    evaluator: str
    version: int
    higher_is_better: bool
    candidate: EvaluatorMetrics
    baseline: EvaluatorMetrics
    paired_n: int
    delta_mean: float | None
    ci95_low: float | None
    ci95_high: float | None
    p_value: float | None
    method: str | None = Field(
        None, description="mcnemar-newcombe (binary scores) or paired-t; see forgeeval/stats.py"
    )
    delta_pass_rate: float | None
    significant: bool
    direction: str


class Flip(BaseModel):
    dataset_item_id: uuid.UUID
    evaluator: str
    baseline_value: float
    candidate_value: float


class Comparison(BaseModel):
    run_id: uuid.UUID
    baseline_run_id: uuid.UUID
    complete: bool
    evaluators: list[EvaluatorComparison]
    not_comparable: list[str]
    newly_failing: list[Flip]
    newly_passing: list[Flip]


def get_run(db: Session, project_id: uuid.UUID, run_id: uuid.UUID) -> EvalRun:
    run = db.scalar(select(EvalRun).where(EvalRun.id == run_id, EvalRun.project_id == project_id))
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Run {run_id} not found.")
    return run


def run_metrics(db: Session, run: EvalRun) -> RunMetrics:
    value = Score.value
    rows = db.execute(
        select(
            Evaluator.name,
            EvaluatorVersion.version,
            EvaluatorVersion.id,
            Evaluator.kind,
            Evaluator.higher_is_better,
            func.count(Score.id),
            func.avg(value),
            func.percentile_cont(0.5).within_group(value),
            func.percentile_cont(0.95).within_group(value),
            func.avg(case((Score.passed.is_(True), 1.0), (Score.passed.is_(False), 0.0))),
            func.count(Score.passed),
        )
        .join(RunItem, RunItem.id == Score.run_item_id)
        .join(EvaluatorVersion, EvaluatorVersion.id == Score.evaluator_version_id)
        .join(Evaluator, Evaluator.id == EvaluatorVersion.evaluator_id)
        .where(RunItem.run_id == run.id)
        .group_by(
            Evaluator.name,
            EvaluatorVersion.version,
            EvaluatorVersion.id,
            Evaluator.kind,
            Evaluator.higher_is_better,
        )
        .order_by(Evaluator.name)
    ).all()

    def f(x: object) -> float | None:
        return None if x is None else float(x)  # type: ignore[arg-type]

    return RunMetrics(
        run_id=run.id,
        status=run.status,
        complete=run.status in TERMINAL,
        evaluators=[
            EvaluatorMetrics(
                evaluator=name,
                version=version,
                evaluator_version_id=evid,
                kind=kind,
                higher_is_better=hib,
                n=n,
                mean=f(mean),
                p50=f(p50),
                p95=f(p95),
                pass_rate=f(pass_rate),
                pass_n=pass_n,
            )
            for name, version, evid, kind, hib, n, mean, p50, p95, pass_rate, pass_n in rows
        ],
    )


@dataclass
class _Pair:
    dataset_item_id: uuid.UUID
    baseline: float
    candidate: float
    baseline_passed: bool | None
    candidate_passed: bool | None


def _pairs(db: Session, run: EvalRun, baseline: EvalRun) -> dict[uuid.UUID, list[_Pair]]:
    """Per evaluator version: items scored in both runs, joined on dataset item."""
    ri_c, ri_b = aliased(RunItem), aliased(RunItem)
    s_c, s_b = aliased(Score), aliased(Score)
    rows = db.execute(
        select(
            s_c.evaluator_version_id,
            ri_c.dataset_item_id,
            s_b.value,
            s_c.value,
            s_b.passed,
            s_c.passed,
        )
        .select_from(ri_c)
        .join(s_c, s_c.run_item_id == ri_c.id)
        .join(ri_b, (ri_b.dataset_item_id == ri_c.dataset_item_id) & (ri_b.run_id == baseline.id))
        .join(
            s_b,
            (s_b.run_item_id == ri_b.id) & (s_b.evaluator_version_id == s_c.evaluator_version_id),
        )
        .where(ri_c.run_id == run.id)
    ).all()
    out: dict[uuid.UUID, list[_Pair]] = defaultdict(list)
    for evid, item_id, b, c, bp, cp in rows:
        out[evid].append(_Pair(item_id, float(b), float(c), bp, cp))
    return out


def paired_stats(
    db: Session,
    run: EvalRun,
    baseline: EvalRun,
    evaluator_version_id: uuid.UUID,
    confidence: float = 0.95,
) -> PairedDelta | None:
    """Paired difference for one evaluator version at the requested confidence level."""
    evp = _pairs(db, run, baseline).get(evaluator_version_id, [])
    return paired_delta([(p.baseline, p.candidate) for p in evp], confidence)


def compare_runs(db: Session, run: EvalRun, baseline: EvalRun) -> Comparison:
    if run.id == baseline.id:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "A run cannot be its own baseline."
        )
    if run.dataset_version_id != baseline.dataset_version_id:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Runs used different dataset versions; their scores are not comparable.",
        )
    cand = {m.evaluator_version_id: m for m in run_metrics(db, run).evaluators}
    base = {m.evaluator_version_id: m for m in run_metrics(db, baseline).evaluators}
    pairs = _pairs(db, run, baseline)

    comparisons: list[EvaluatorComparison] = []
    failing: list[Flip] = []
    passing: list[Flip] = []
    for evid in sorted(cand.keys() & base.keys(), key=lambda k: cand[k].evaluator):
        c, b = cand[evid], base[evid]
        evp = pairs.get(evid, [])
        delta = paired_delta([(p.baseline, p.candidate) for p in evp])
        significant = delta.significant if delta else False
        comparisons.append(
            EvaluatorComparison(
                evaluator=c.evaluator,
                version=c.version,
                higher_is_better=c.higher_is_better,
                candidate=c,
                baseline=b,
                paired_n=len(evp),
                delta_mean=delta.mean if delta else None,
                ci95_low=delta.ci_low if delta else None,
                ci95_high=delta.ci_high if delta else None,
                p_value=delta.p_value if delta else None,
                method=delta.method if delta else None,
                delta_pass_rate=(
                    c.pass_rate - b.pass_rate
                    if c.pass_rate is not None and b.pass_rate is not None
                    else None
                ),
                significant=significant,
                direction=direction(delta.mean if delta else 0.0, c.higher_is_better, significant),
            )
        )
        for p in evp:
            flip = Flip(
                dataset_item_id=p.dataset_item_id,
                evaluator=c.evaluator,
                baseline_value=p.baseline,
                candidate_value=p.candidate,
            )
            if p.baseline_passed is True and p.candidate_passed is False:
                failing.append(flip)
            elif p.baseline_passed is False and p.candidate_passed is True:
                passing.append(flip)

    names = {
        m.evaluator_version_id: f"{m.evaluator}@v{m.version}"
        for m in [*cand.values(), *base.values()]
    }
    not_comparable = sorted(names[k] for k in cand.keys() ^ base.keys())
    return Comparison(
        run_id=run.id,
        baseline_run_id=baseline.id,
        complete=run.status in TERMINAL and baseline.status in TERMINAL,
        evaluators=comparisons,
        not_comparable=not_comparable,
        newly_failing=failing[:MAX_FLIPS],
        newly_passing=passing[:MAX_FLIPS],
    )


@router.get("/{run_id}/metrics")
def get_metrics(run_id: uuid.UUID, db: DbSession, ctx: Reader) -> RunMetrics:
    """Per evaluator: score count, mean, p50, p95 and pass rate."""
    return run_metrics(db, get_run(db, ctx.project_id, run_id))


@router.get("/{run_id}/compare")
def compare(
    run_id: uuid.UUID, db: DbSession, ctx: Reader, baseline_run_id: uuid.UUID | None = None
) -> Comparison:
    """Compare against `baseline_run_id`, or the baseline recorded on the run."""
    run = get_run(db, ctx.project_id, run_id)
    baseline_id = baseline_run_id or run.baseline_run_id
    if baseline_id is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "No baseline: pass baseline_run_id or create the run with one.",
        )
    return compare_runs(db, run, get_run(db, ctx.project_id, baseline_id))
