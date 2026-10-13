import { Fragment, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  useCheckAllGates,
  useEvaluators,
  useGateChecks,
  useGates,
  useRunChoices,
} from "../api/queries";
import type { Schemas } from "../api/client";
import { Empty, ErrorBox, Loading, Mono, StatusPill } from "../components/ui";
import {
  formatDelta,
  formatMetric,
  formatPoints,
  formatRate,
  relativeTime,
  shortId,
  shortSha,
  toNumber,
} from "../lib/format";

type Gate = Schemas["GateOut"];
type Kind = Schemas["EvaluatorKind"];

const COMPARATORS: Record<Schemas["GateComparator"], string> = {
  min_absolute: "at least (absolute)",
  max_drop_vs_baseline: "max drop vs baseline",
};

/** Gate values arrive as decimal strings; pass_rate is a share, the rest use the metric's unit. */
export function formatGateValue(
  aggregate: Schemas["GateAggregate"],
  kind: Kind,
  v: string | null,
): string {
  const n = toNumber(v);
  if (n === null) return "—";
  return aggregate === "pass_rate" ? formatRate(n) : formatMetric(kind, n);
}

function GateChecks({ gate, kind, colSpan }: { gate: Gate; kind: Kind; colSpan: number }) {
  const q = useGateChecks(gate.name, true);
  return (
    <tr className="detail-row">
      <td colSpan={colSpan}>
        <div className="detail" data-testid={`checks-${gate.name}`}>
          <h4>Check history</h4>
          {q.isPending ? (
            <Loading label="Loading checks" />
          ) : q.isError ? (
            <ErrorBox error={q.error} onRetry={() => void q.refetch()} />
          ) : q.data.length === 0 ? (
            <Empty title="No checks yet">Run a check below or via the CI action.</Empty>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Verdict</th>
                    <th>Reason</th>
                    <th className="right">Observed</th>
                    <th className="right">Baseline</th>
                    <th className="right">Delta</th>
                    <th>PR</th>
                    <th>Commit</th>
                    <th>Run</th>
                    <th>When</th>
                  </tr>
                </thead>
                <tbody>
                  {q.data.map((c) => (
                    <tr key={c.id}>
                      <td>
                        <StatusPill status={c.verdict} />
                      </td>
                      <td className="wrap">{c.reason}</td>
                      <td className="num right">
                        {formatGateValue(c.aggregate, kind, c.observed)}
                      </td>
                      <td className="num right">
                        {formatGateValue(c.aggregate, kind, c.baseline)}
                      </td>
                      <td className="num right">
                        {c.aggregate === "pass_rate"
                          ? formatPoints(toNumber(c.delta))
                          : formatDelta(kind, toNumber(c.delta))}
                      </td>
                      <td className="num">{c.pr_number ? `#${c.pr_number}` : "—"}</td>
                      <td>
                        <Mono title={c.commit_sha ?? undefined}>{shortSha(c.commit_sha)}</Mono>
                      </td>
                      <td>
                        <Link to={`/runs/${c.run_id}`} className="mono">
                          {shortId(c.run_id)}
                        </Link>
                      </td>
                      <td title={new Date(c.created_at).toLocaleString()}>
                        {relativeTime(c.created_at)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </td>
    </tr>
  );
}

function CheckAll() {
  const runs = useRunChoices();
  const check = useCheckAllGates();
  const [runId, setRunId] = useState("");
  const finished = (runs.data ?? []).filter(
    (r) => r.status === "succeeded" || r.status === "partial",
  );

  return (
    <section className="panel" aria-label="Check all gates">
      <h2>Check all gates for a run</h2>
      <form
        className="filters"
        onSubmit={(e) => {
          e.preventDefault();
          if (runId) check.mutate(runId);
        }}
      >
        <label>
          <span>Finished run</span>
          <select value={runId} onChange={(e) => setRunId(e.target.value)}>
            <option value="">Choose a run</option>
            {finished.map((r) => (
              <option key={r.id} value={r.id}>
                {shortId(r.id)} · {r.dataset}@{r.dataset_version} · {r.prompt}@{r.prompt_version} ·{" "}
                {relativeTime(r.created_at)}
              </option>
            ))}
          </select>
        </label>
        <button type="submit" className="btn btn-primary" disabled={!runId || check.isPending}>
          {check.isPending ? "Checking…" : "Check gates"}
        </button>
      </form>
      {check.isError && <ErrorBox error={check.error} />}
      {check.isSuccess && (
        <div className="check-result" data-testid="check-result">
          <p>
            Overall verdict: <StatusPill status={check.data.verdict} />
          </p>
          <ul className="flips">
            {check.data.checks.map((c) => (
              <li key={c.id}>
                <Mono>{c.gate}</Mono>
                <StatusPill status={c.verdict} />
                <span className="wrap">{c.reason}</span>
              </li>
            ))}
          </ul>
          {check.data.checks.length === 0 && (
            <p className="muted">No enabled gate applies to this run's evaluators.</p>
          )}
        </div>
      )}
    </section>
  );
}

export function GatesPage() {
  const gates = useGates();
  const evaluators = useEvaluators();
  const [open, setOpen] = useState<Set<string>>(new Set());
  const kinds = useMemo(() => {
    const m = new Map<string, Kind>();
    for (const e of evaluators.data ?? []) m.set(e.name, e.kind);
    return m;
  }, [evaluators.data]);

  return (
    <>
      <div className="page-head">
        <h1>Gates</h1>
      </div>
      {gates.isPending ? (
        <Loading label="Loading gates" />
      ) : gates.isError ? (
        <ErrorBox error={gates.error} onRetry={() => void gates.refetch()} />
      ) : gates.data.length === 0 ? (
        <Empty title="No gates defined">
          Gates are created through the API (<span className="mono">POST /v1/gates</span>).
        </Empty>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th aria-label="Expand" />
                <th>Gate</th>
                <th>Evaluator</th>
                <th>Aggregate</th>
                <th>Comparator</th>
                <th className="right">Threshold</th>
                <th>Enabled</th>
              </tr>
            </thead>
            <tbody>
              {gates.data.map((g) => {
                const kind = kinds.get(g.evaluator) ?? "exact_match";
                const isOpen = open.has(g.name);
                return (
                  <Fragment key={g.id}>
                    <tr>
                      <td>
                        <button
                          type="button"
                          className="expander"
                          aria-expanded={isOpen}
                          aria-label={`${isOpen ? "Hide" : "Show"} checks for ${g.name}`}
                          onClick={() =>
                            setOpen((prev) => {
                              const next = new Set(prev);
                              if (next.has(g.name)) next.delete(g.name);
                              else next.add(g.name);
                              return next;
                            })
                          }
                        >
                          <span aria-hidden="true">{isOpen ? "▾" : "▸"}</span>
                        </button>
                      </td>
                      <td>
                        <Mono>{g.name}</Mono>
                      </td>
                      <td>
                        <Mono>
                          {g.evaluator}@{g.evaluator_version}
                        </Mono>
                      </td>
                      <td>{g.aggregate.replace(/_/g, " ")}</td>
                      <td>{COMPARATORS[g.comparator]}</td>
                      <td className="num right">
                        {formatGateValue(g.aggregate, kind, g.threshold)}
                      </td>
                      <td>{g.enabled ? "yes" : <span className="muted">no</span>}</td>
                    </tr>
                    {isOpen && <GateChecks gate={g} kind={kind} colSpan={7} />}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      <div className="spacer" />
      <CheckAll />
    </>
  );
}
