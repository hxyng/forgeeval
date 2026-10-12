import { Link } from "react-router-dom";
import { isActive, useRuns } from "../api/queries";
import type { Run } from "../api/queries";
import { Empty, ErrorBox, Loading, Mono, ProgressBar, StatusPill } from "../components/ui";
import { formatUsd, relativeTime, shortId, shortSha } from "../lib/format";

export function RunRow({ run }: { run: Run }) {
  return (
    <tr>
      <td>
        <StatusPill status={run.status} />
      </td>
      <td>
        <Link to={`/runs/${run.id}`} className="mono" title={run.id}>
          {shortId(run.id)}
        </Link>
      </td>
      <td className="col-progress">
        <ProgressBar progress={run.progress} total={run.total_items} />
      </td>
      <td>
        <Mono>
          {run.dataset}@{run.dataset_version}
        </Mono>
      </td>
      <td>
        <Mono>
          {run.prompt}@{run.prompt_version}
        </Mono>
      </td>
      <td>{run.trigger}</td>
      <td>
        <Mono title={run.git_sha ?? undefined}>{shortSha(run.git_sha)}</Mono>
      </td>
      <td title={new Date(run.created_at).toLocaleString()}>{relativeTime(run.created_at)}</td>
      <td className="num right">{formatUsd(run.cost_usd)}</td>
    </tr>
  );
}

export function RunsPage() {
  const q = useRuns();
  const runs = q.data?.pages.flatMap((p) => p.items) ?? [];
  const live = runs.some((r) => isActive(r.status));

  return (
    <>
      <div className="page-head">
        <h1>Runs</h1>
        <div className="page-actions">
          {live && <span className="live muted">Refreshing every 2 s while runs are active</span>}
        </div>
      </div>
      {q.isPending ? (
        <Loading label="Loading runs" />
      ) : q.isError ? (
        <ErrorBox error={q.error} onRetry={() => void q.refetch()} />
      ) : runs.length === 0 ? (
        <Empty title="No runs yet">
          Start one from the SDK or CLI, for example <span className="mono">forgeeval gate</span>.
        </Empty>
      ) : (
        <>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Status</th>
                  <th>Run</th>
                  <th>Progress</th>
                  <th>Dataset</th>
                  <th>Prompt</th>
                  <th>Trigger</th>
                  <th>Git</th>
                  <th>Created</th>
                  <th className="right">Cost</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((r) => (
                  <RunRow key={r.id} run={r} />
                ))}
              </tbody>
            </table>
          </div>
          <div className="pager">
            {q.hasNextPage ? (
              <button
                type="button"
                className="btn"
                onClick={() => void q.fetchNextPage()}
                disabled={q.isFetchingNextPage}
              >
                {q.isFetchingNextPage ? "Loading…" : "Load more"}
              </button>
            ) : (
              <span className="muted">{runs.length} runs, end of list</span>
            )}
          </div>
        </>
      )}
    </>
  );
}
