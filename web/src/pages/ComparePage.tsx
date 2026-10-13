import { useMemo, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useComparison, useRun, useRunChoices } from "../api/queries";
import type { Schemas } from "../api/client";
import { IntervalBar } from "../components/IntervalBar";
import { Empty, ErrorBox, Loading, Mono } from "../components/ui";
import { formatDelta, formatMetric, formatPoints, formatRate, shortId } from "../lib/format";

type EvalCompare = Schemas["EvaluatorComparison"];
type Flip = Schemas["Flip"];

const PAGE = 20;

export function ComparisonTable({ rows }: { rows: EvalCompare[] }) {
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Evaluator</th>
            <th className="right">Baseline</th>
            <th className="right">Candidate</th>
            <th className="right">Delta</th>
            <th>95% CI around zero</th>
            <th className="right">Pass rate</th>
            <th className="right">Pairs</th>
            <th>Verdict</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const kind = r.candidate.kind;
            const direction = r.direction;
            return (
              <tr key={`${r.evaluator}@${r.version}`} data-direction={direction}>
                <td>
                  <Mono>
                    {r.evaluator}@{r.version}
                  </Mono>
                  <div className="muted small">
                    {r.higher_is_better ? "higher is better" : "lower is better"}
                  </div>
                </td>
                <td className="num right">{formatMetric(kind, r.baseline.mean)}</td>
                <td className="num right">{formatMetric(kind, r.candidate.mean)}</td>
                <td className="num right">
                  <strong>{formatDelta(kind, r.delta_mean)}</strong>
                  {r.ci95_low !== null && r.ci95_high !== null && (
                    <div className="muted small">
                      [{formatDelta(kind, r.ci95_low)}, {formatDelta(kind, r.ci95_high)}]
                    </div>
                  )}
                </td>
                <td>
                  <IntervalBar
                    delta={r.delta_mean}
                    low={r.ci95_low}
                    high={r.ci95_high}
                    higherIsBetter={r.higher_is_better}
                    significant={r.significant}
                    label={formatDelta(kind, r.delta_mean)}
                  />
                </td>
                <td className="num right">
                  {formatRate(r.baseline.pass_rate)} {"→"} {formatRate(r.candidate.pass_rate)}
                  <div className="muted small">{formatPoints(r.delta_pass_rate)}</div>
                </td>
                <td className="num right">{r.paired_n}</td>
                <td>
                  <span className={`dir dir-${direction}`}>{direction}</span>
                  {!r.significant && direction !== "unchanged" && (
                    <span className="muted small"> (not significant)</span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function FlipList({
  title,
  testId,
  flips,
  runId,
  kind,
}: {
  title: string;
  testId: string;
  flips: Flip[];
  runId: string;
  kind: "failing" | "passing";
}) {
  const [shown, setShown] = useState(PAGE);
  const byEvaluator = useMemo(() => {
    const m = new Map<string, number>();
    for (const f of flips) m.set(f.evaluator, (m.get(f.evaluator) ?? 0) + 1);
    return [...m.entries()];
  }, [flips]);

  const itemsLink = (evaluator: string, focus?: string) => {
    const q = new URLSearchParams();
    if (kind === "failing") q.set("failed_evaluator", evaluator);
    else q.set("status", "succeeded");
    if (focus) q.set("focus", focus);
    return `/runs/${runId}/items?${q.toString()}`;
  };

  return (
    <section className="panel" aria-label={title} data-testid={testId}>
      <div className="panel-head">
        <h2>
          {title} <span className="num">({flips.length})</span>
        </h2>
        {kind === "failing" && (
          <span className="small">
            {byEvaluator.map(([ev, n]) => (
              <Link key={ev} to={itemsLink(ev)} className="gap-left">
                {n} in {ev}: open in items
              </Link>
            ))}
          </span>
        )}
      </div>
      {flips.length === 0 ? (
        <p className="muted">None.</p>
      ) : (
        <>
          <ul className="flips">
            {flips.slice(0, shown).map((f) => (
              <li key={`${f.evaluator}:${f.dataset_item_id}`}>
                <Link to={itemsLink(f.evaluator, f.dataset_item_id)} className="mono">
                  {shortId(f.dataset_item_id)}
                </Link>
                <span>{f.evaluator}</span>
                <span className="num muted">
                  {f.baseline_value} {"→"} {f.candidate_value}
                </span>
              </li>
            ))}
          </ul>
          {flips.length > shown && (
            <button
              type="button"
              className="btn btn-small"
              onClick={() => setShown((n) => n + PAGE)}
            >
              Show {Math.min(PAGE, flips.length - shown)} more
            </button>
          )}
        </>
      )}
    </section>
  );
}

export function ComparePage() {
  const { id = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const run = useRun(id);
  const choices = useRunChoices();

  const baselineId = params.get("baseline") ?? run.data?.baseline_run_id ?? null;
  const cmp = useComparison(id, baselineId);

  const candidates = useMemo(() => {
    const r = run.data;
    if (!r) return [];
    const same = (choices.data ?? []).filter(
      (c) => c.id !== r.id && c.dataset === r.dataset && c.dataset_version === r.dataset_version,
    );
    return same;
  }, [run.data, choices.data]);

  if (run.isPending) return <Loading label="Loading run" />;
  if (run.isError) return <ErrorBox error={run.error} onRetry={() => void run.refetch()} />;
  const r = run.data;

  const known = candidates.some((c) => c.id === baselineId);

  return (
    <>
      <nav className="crumbs" aria-label="Breadcrumb">
        <Link to="/">Runs</Link> <span aria-hidden="true">/</span>{" "}
        <Link to={`/runs/${id}`} className="mono">
          {shortId(id)}
        </Link>{" "}
        <span aria-hidden="true">/</span> Compare
      </nav>
      <div className="page-head">
        <h1>
          Compare <Mono>{shortId(id)}</Mono> with baseline
        </h1>
      </div>

      <div className="filters">
        <label>
          <span>
            Baseline run (same dataset version: {r.dataset}@{r.dataset_version})
          </span>
          <select
            value={baselineId ?? ""}
            onChange={(e) => {
              const next = new URLSearchParams(params);
              if (e.target.value) next.set("baseline", e.target.value);
              else next.delete("baseline");
              setParams(next, { replace: true });
            }}
          >
            <option value="">Choose a baseline run</option>
            {baselineId && !known && <option value={baselineId}>{shortId(baselineId)}</option>}
            {candidates.map((c) => (
              <option key={c.id} value={c.id}>
                {shortId(c.id)} · {c.prompt}@{c.prompt_version} · {c.status} ·{" "}
                {new Date(c.created_at).toLocaleString()}
              </option>
            ))}
          </select>
        </label>
        {r.baseline_run_id && baselineId !== r.baseline_run_id && (
          <button
            type="button"
            className="btn btn-quiet"
            onClick={() => setParams({}, { replace: true })}
          >
            Use the run's own baseline
          </button>
        )}
      </div>

      {baselineId === null ? (
        candidates.length === 0 && choices.isSuccess ? (
          <Empty title="No baseline to compare against">
            This run has no baseline, and no other run used {r.dataset}@{r.dataset_version}.
          </Empty>
        ) : (
          <Empty title="Pick a baseline run">
            Only runs over the same dataset version can be compared.
          </Empty>
        )
      ) : cmp.isPending ? (
        <Loading label="Comparing" />
      ) : cmp.isError ? (
        <ErrorBox error={cmp.error} onRetry={() => void cmp.refetch()} />
      ) : (
        <>
          {!cmp.data.complete && (
            <div className="state state-warn" role="status">
              <strong>Incomplete comparison.</strong> One or both runs have not finished, so deltas
              and intervals can still change.
            </div>
          )}
          {cmp.data.evaluators.length === 0 ? (
            <Empty title="No comparable evaluators">
              The two runs share no evaluator versions with scores.
            </Empty>
          ) : (
            <ComparisonTable rows={cmp.data.evaluators} />
          )}
          {cmp.data.not_comparable.length > 0 && (
            <div className="state state-warn not-comparable" role="note">
              <strong>Not comparable:</strong>
              <span>
                {cmp.data.not_comparable.join(", ")} (evaluator version missing from one of the
                runs).
              </span>
            </div>
          )}
          <div className="two-col">
            <FlipList
              title="Newly failing"
              testId="newly-failing"
              flips={cmp.data.newly_failing}
              runId={id}
              kind="failing"
            />
            <FlipList
              title="Newly passing"
              testId="newly-passing"
              flips={cmp.data.newly_passing}
              runId={id}
              kind="passing"
            />
          </div>
        </>
      )}
    </>
  );
}

export function CompareIndexPage() {
  const runs = useRunChoices();
  const navigate = useNavigate();
  const [pick, setPick] = useState("");

  return (
    <>
      <div className="page-head">
        <h1>Compare</h1>
      </div>
      <p className="muted">
        Choose a run to compare against its baseline: paired deltas with 95% confidence intervals
        per evaluator.
      </p>
      {runs.isPending ? (
        <Loading label="Loading runs" />
      ) : runs.isError ? (
        <ErrorBox error={runs.error} onRetry={() => void runs.refetch()} />
      ) : runs.data.length === 0 ? (
        <Empty title="No runs to compare yet" />
      ) : (
        <form
          className="filters"
          onSubmit={(e) => {
            e.preventDefault();
            if (pick) navigate(`/runs/${pick}/compare`);
          }}
        >
          <label>
            <span>Run</span>
            <select value={pick} onChange={(e) => setPick(e.target.value)}>
              <option value="">Choose a run</option>
              {runs.data.map((r) => (
                <option key={r.id} value={r.id}>
                  {shortId(r.id)} · {r.dataset}@{r.dataset_version} · {r.prompt}@{r.prompt_version}{" "}
                  · {r.status}
                </option>
              ))}
            </select>
          </label>
          <button type="submit" className="btn btn-primary" disabled={!pick}>
            Compare
          </button>
        </form>
      )}
    </>
  );
}
