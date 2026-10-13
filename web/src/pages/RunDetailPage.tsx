import { Link, useParams } from "react-router-dom";
import { isActive, useCancelRun, useRun, useRunMetrics } from "../api/queries";
import type { Schemas } from "../api/client";
import { Empty, ErrorBox, Facts, Loading, Mono, ProgressBar, StatusPill } from "../components/ui";
import {
  formatDateTime,
  formatMetric,
  formatRate,
  formatSpan,
  formatUsd,
  relativeTime,
  shortId,
  shortSha,
} from "../lib/format";
import { summarizeProgress } from "../lib/progress";

type Metric = Schemas["EvaluatorMetrics"];

/** `pass_n` counts graded scores (not passes), so passes = pass_rate x pass_n. */
export function passCounts(m: Metric): { passed: number; graded: number; failing: number } {
  const graded = m.pass_n;
  const passed = m.pass_rate === null ? 0 : Math.round(m.pass_rate * graded);
  return { passed, graded, failing: Math.max(0, graded - passed) };
}

function polarityLabel(m: Metric): string {
  return m.higher_is_better ? "higher is better" : "lower is better";
}

export function MetricsTable({ runId, metrics }: { runId: string; metrics: Metric[] }) {
  if (metrics.length === 0) {
    return <Empty title="No metrics yet">Scores appear as items are evaluated.</Empty>;
  }
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Evaluator</th>
            <th>Polarity</th>
            <th className="right">n</th>
            <th className="right">Mean</th>
            <th className="right">p50</th>
            <th className="right">p95</th>
            <th className="right">Pass rate</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {metrics.map((m) => {
            const c = passCounts(m);
            return (
              <tr key={m.evaluator_version_id}>
                <td>
                  <Mono>
                    {m.evaluator}@{m.version}
                  </Mono>{" "}
                  <span className="muted small">{m.kind.replace(/_/g, " ")}</span>
                </td>
                <td className="muted small">{polarityLabel(m)}</td>
                <td className="num right">{m.n}</td>
                <td className="num right">{formatMetric(m.kind, m.mean)}</td>
                <td className="num right">{formatMetric(m.kind, m.p50)}</td>
                <td className="num right">{formatMetric(m.kind, m.p95)}</td>
                <td className="num right">
                  {m.pass_rate === null ? (
                    formatRate(null)
                  ) : (
                    <>
                      {formatRate(m.pass_rate)}{" "}
                      <span className="muted small">{`(${c.passed}/${c.graded})`}</span>
                    </>
                  )}
                </td>
                <td className="right">
                  {c.failing > 0 && (
                    <Link
                      to={`/runs/${runId}/items?failed_evaluator=${encodeURIComponent(m.evaluator)}`}
                    >
                      {c.failing} failing {c.failing === 1 ? "case" : "cases"}
                    </Link>
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

export function RunDetailPage() {
  const { id = "" } = useParams();
  const run = useRun(id);
  const metrics = useRunMetrics(id, run.data?.status);
  const cancel = useCancelRun(id);

  if (run.isPending) return <Loading label="Loading run" />;
  if (run.isError) return <ErrorBox error={run.error} onRetry={() => void run.refetch()} />;

  const r = run.data;
  const active = isActive(r.status);
  const prog = summarizeProgress(r.progress, r.total_items);

  return (
    <>
      <nav className="crumbs" aria-label="Breadcrumb">
        <Link to="/">Runs</Link> <span aria-hidden="true">/</span> <Mono>{r.id}</Mono>
      </nav>
      <div className="page-head">
        <h1>
          Run <Mono>{shortId(r.id)}</Mono> <StatusPill status={r.status} />
        </h1>
        <div className="page-actions">
          <Link className="btn" to={`/runs/${r.id}/items`}>
            Items
          </Link>
          {active && (
            <button
              type="button"
              className="btn btn-danger"
              onClick={() => cancel.mutate()}
              disabled={cancel.isPending}
            >
              {cancel.isPending ? "Cancelling…" : "Cancel run"}
            </button>
          )}
        </div>
      </div>
      {cancel.isError && <ErrorBox error={cancel.error} />}

      <section aria-label="Progress" className="panel">
        <div className="panel-head">
          <h2>Progress</h2>
          {active && <span className="muted small">live, polling every 2 s</span>}
        </div>
        <ProgressBar progress={r.progress} total={r.total_items} />
        <p className="muted small num">
          {prog.succeeded} succeeded · {prog.failed} dead-lettered · {prog.cancelled} cancelled ·{" "}
          {prog.inFlight} pending
        </p>
        {prog.failed > 0 && (
          <p>
            <Link to={`/runs/${r.id}/items?status=dead_lettered`}>
              View {prog.failed} dead-lettered {prog.failed === 1 ? "item" : "items"}
            </Link>
          </p>
        )}
      </section>

      <section aria-label="Run details" className="panel">
        <h2>Details</h2>
        <Facts
          items={[
            [
              "Dataset",
              <Mono key="d">
                {r.dataset}@{r.dataset_version}
              </Mono>,
            ],
            [
              "Prompt",
              <Mono key="p">
                {r.prompt}@{r.prompt_version}
              </Mono>,
            ],
            ["Evaluators", r.evaluators.map((e) => `${e.name}@${e.version}`).join(", ") || "none"],
            ["Trigger", r.trigger],
            [
              "Git SHA",
              <Mono key="g" title={r.git_sha ?? undefined}>
                {shortSha(r.git_sha)}
              </Mono>,
            ],
            [
              "Baseline",
              r.baseline_run_id ? (
                <Link key="b" to={`/runs/${r.baseline_run_id}`} className="mono">
                  {shortId(r.baseline_run_id)}
                </Link>
              ) : (
                "none"
              ),
            ],
            ["Items", String(r.total_items)],
            [
              "Cost",
              <span key="c" className="num">
                {formatUsd(r.cost_usd)}
              </span>,
            ],
            ["Created", `${relativeTime(r.created_at)} (${formatDateTime(r.created_at)})`],
            ["Started", formatDateTime(r.started_at)],
            ["Finished", formatDateTime(r.finished_at)],
            ["Duration", formatSpan(r.started_at, r.finished_at)],
          ]}
        />
      </section>

      <section aria-label="Metrics" className="panel">
        <div className="panel-head">
          <h2>Metrics</h2>
          {metrics.data && !metrics.data.complete && (
            <span className="muted small">partial: run not finished</span>
          )}
        </div>
        {metrics.isPending ? (
          <Loading label="Loading metrics" />
        ) : metrics.isError ? (
          <ErrorBox error={metrics.error} onRetry={() => void metrics.refetch()} />
        ) : (
          <MetricsTable runId={r.id} metrics={metrics.data.evaluators} />
        )}
      </section>
    </>
  );
}
