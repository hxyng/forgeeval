"""Stop an evaluation run as soon as its gates are decided.

A run created with ``early_stop`` processes its items in a random but fixed order
(``run_items.position``). Every few seconds the reaper looks at the longest *contiguous* prefix of
finished items in that order — never "whatever finished first", because slow or retried items
could correlate with their outcome — pairs them with the baseline's scores, and evaluates the
project's eligible gates with an anytime-valid confidence sequence (stats.confidence_sequence).

- Any eligible gate decisively failing stops the run: the PR is blocked either way.
- All applicable gates decisively passing stops the run.
- Otherwise the run continues; if it finishes, gates use the ordinary fixed-n interval.

Error budget: ALPHA_SEQUENTIAL for decisions taken while the run is in flight, ALPHA_FIXED for the
final fixed-n test, so the total stays within 5% (union bound) and runs that go the distance keep
nearly all of their fixed-n precision. Bonferroni across gates applies within each budget.
"""

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import exists, func, select, update
from sqlalchemy.orm import Session, aliased

from forgeeval.db.models import (
    EvalRun,
    Evaluator,
    EvaluatorKind,
    EvaluatorVersion,
    GateAggregate,
    GateComparator,
    GateVerdict,
    ItemStatus,
    RegressionGate,
    RunEvaluator,
    RunItem,
    RunStatus,
    Score,
)
from forgeeval.stats import PairedDelta, confidence_sequence

log = logging.getLogger("forgeeval.early_stop")

ALPHA_SEQUENTIAL = 0.01
ALPHA_FIXED = 0.04
MIN_ITEMS = 10
# Scores normalised to [0, 1], so paired differences lie in [-1, 1].
BOUNDED_KINDS = {
    EvaluatorKind.exact_match,
    EvaluatorKind.json_schema,
    EvaluatorKind.retrieval_relevance,
    EvaluatorKind.llm_rubric,
}


@dataclass(frozen=True)
class Decision:
    stop: bool
    reason: str


def applicable_gates(db: Session, run: EvalRun) -> list[tuple[RegressionGate, EvaluatorKind, bool]]:
    """Enabled gates for evaluators this run uses: (gate, evaluator kind, higher_is_better)."""
    rows = db.execute(
        select(RegressionGate, Evaluator.kind, Evaluator.higher_is_better)
        .join(EvaluatorVersion, EvaluatorVersion.id == RegressionGate.evaluator_version_id)
        .join(Evaluator, Evaluator.id == EvaluatorVersion.evaluator_id)
        .where(
            RegressionGate.project_id == run.project_id,
            RegressionGate.enabled.is_(True),
            exists().where(
                RunEvaluator.run_id == run.id,
                RunEvaluator.evaluator_version_id == RegressionGate.evaluator_version_id,
            ),
        )
        .order_by(RegressionGate.name)
    ).all()
    return [(g, k, h) for g, k, h in rows]


def is_sequential_eligible(gate: RegressionGate, kind: EvaluatorKind) -> bool:
    return (
        gate.comparator is GateComparator.max_drop_vs_baseline
        and gate.aggregate in (GateAggregate.mean, GateAggregate.pass_rate)
        and kind in BOUNDED_KINDS
    )


def prefix_diffs(
    db: Session, run: EvalRun, baseline_id: uuid.UUID, evaluator_version_id: uuid.UUID
) -> list[float]:
    """candidate - baseline score per item, over the contiguous finished prefix in position
    order. Stops at the first item that isn't finished yet."""
    base_item, base_score = aliased(RunItem), aliased(Score)
    rows = db.execute(
        select(RunItem.position, RunItem.status, Score.value, base_score.value)
        .outerjoin(
            Score,
            (Score.run_item_id == RunItem.id)
            & (Score.evaluator_version_id == evaluator_version_id),
        )
        .outerjoin(
            base_item,
            (base_item.run_id == baseline_id)
            & (base_item.dataset_item_id == RunItem.dataset_item_id),
        )
        .outerjoin(
            base_score,
            (base_score.run_item_id == base_item.id)
            & (base_score.evaluator_version_id == evaluator_version_id),
        )
        .where(RunItem.run_id == run.id)
        .order_by(RunItem.position)
    ).all()
    diffs: list[float] = []
    for _pos, status, cand, base in rows:
        if status is not ItemStatus.succeeded:
            break  # first unfinished (or failed) item ends the usable prefix
        if cand is None or base is None:
            continue  # item not scored by both runs: not comparable, skip it
        diffs.append(float(cand) - float(base))
    return diffs


def sequential_interval(diffs: list[float], alpha: float) -> PairedDelta | None:
    if any(d < -1 or d > 1 for d in diffs):
        return None
    cs = confidence_sequence(diffs, alpha)
    if cs is None:
        return None
    return PairedDelta(
        n=cs.n,
        mean=cs.mean,
        ci_low=cs.low,
        ci_high=cs.high,
        p_value=None,
        method="confidence-sequence",
        confidence=1 - alpha,
    )


def evaluate(db: Session, run: EvalRun) -> Decision:
    """Decide whether the run can stop now."""
    from forgeeval.api.compare import get_run, run_metrics
    from forgeeval.api.gates import decide

    if run.baseline_run_id is None:
        return Decision(False, "no baseline")
    gates = applicable_gates(db, run)
    eligible = [(g, k, h) for g, k, h in gates if is_sequential_eligible(g, k)]
    if not eligible:
        return Decision(False, "no gate can be decided sequentially")
    # Same Bonferroni split as POST /v1/gates:check, which counts every applicable gate.
    alpha = ALPHA_SEQUENTIAL / len(gates)
    baseline = get_run(db, run.project_id, run.baseline_run_id)
    base_metrics = {m.evaluator_version_id: m for m in run_metrics(db, baseline).evaluators}
    cand_metrics = {m.evaluator_version_id: m for m in run_metrics(db, run).evaluators}

    verdicts: list[tuple[str, GateVerdict, str, int]] = []
    for gate, _kind, hib in eligible:
        diffs = prefix_diffs(db, run, baseline.id, gate.evaluator_version_id)
        if len(diffs) < MIN_ITEMS:
            return Decision(False, f"waiting for {MIN_ITEMS} paired items")
        interval = sequential_interval(diffs, alpha)
        v = decide(
            gate,
            hib,
            cand_metrics.get(gate.evaluator_version_id),
            base_metrics.get(gate.evaluator_version_id),
            interval,
        )
        verdicts.append((gate.name, v.verdict, v.reason, len(diffs)))

    failed = [x for x in verdicts if x[1] is GateVerdict.failed]
    if failed:
        name, _, reason, n = failed[0]
        return Decision(True, f"gate '{name}' failed after {n} items: {reason}")
    all_eligible = len(eligible) == len(gates)
    if all_eligible and all(v is GateVerdict.passed for _, v, _, _ in verdicts):
        n = min(x[3] for x in verdicts)
        return Decision(True, f"all {len(verdicts)} gates passed after {n} items")
    return Decision(False, "not decided yet")


def stop_run(db: Session, run: EvalRun, reason: str) -> int:
    """Record the decision and cancel the items that haven't started. Returns cancelled count."""
    cancelled = len(
        db.execute(
            update(RunItem)
            .where(
                RunItem.run_id == run.id,
                RunItem.status.in_([ItemStatus.queued, ItemStatus.retry_wait]),
            )
            .values(status=ItemStatus.cancelled)
            .returning(RunItem.id)
        ).all()
    )
    db.execute(
        update(EvalRun)
        .where(EvalRun.id == run.id, EvalRun.stopped_early_at.is_(None))
        .values(stopped_early_at=func.now(), early_stop_reason=reason[:2000])
    )
    db.commit()
    log.info("run %s stopped early (%d items cancelled): %s", run.id, cancelled, reason)
    return cancelled


def check_early_stops(db: Session) -> list[uuid.UUID]:
    """Called from the reaper: evaluate every in-flight early-stop run once."""
    from forgeeval.worker.processor import finalize_run

    stopped: list[uuid.UUID] = []
    runs = db.scalars(
        select(EvalRun).where(
            EvalRun.early_stop.is_(True),
            EvalRun.stopped_early_at.is_(None),
            EvalRun.status.in_([RunStatus.queued, RunStatus.running]),
        )
    ).all()
    for run in runs:
        try:
            decision = evaluate(db, run)
        except Exception:
            log.exception("early-stop check failed for run %s", run.id)
            db.rollback()
            continue
        if decision.stop:
            stop_run(db, run, decision.reason)
            finalize_run(db, run.id)
            stopped.append(run.id)
    return stopped
