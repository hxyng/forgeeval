import { Fragment, useState } from "react";
import type { FormEvent } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { useRun, useRunItems } from "../api/queries";
import type { Schemas } from "../api/client";
import { Empty, ErrorBox, JsonView, Loading, Mono, StatusPill } from "../components/ui";
import { formatMs, formatScore, formatUsd, shortId, toNumber } from "../lib/format";

type Item = Schemas["RunItemOut"];
const STATUSES: Schemas["ItemStatus"][] = [
  "queued",
  "leased",
  "retry_wait",
  "succeeded",
  "dead_lettered",
  "cancelled",
];

function ScoreChip({ score }: { score: Schemas["ItemScore"] }) {
  const state = score.passed === null ? "na" : score.passed ? "pass" : "fail";
  return (
    <span className={`chip chip-${state}`} title={score.rationale ?? undefined}>
      <span className="chip-name">{score.evaluator}</span>{" "}
      <span className="num">{formatScore(score.value)}</span>
      <span className="sr-only">
        {" "}
        {state === "na" ? "not graded" : state === "pass" ? "passed" : "failed"}
      </span>
    </span>
  );
}

function ItemDetail({ item, colSpan }: { item: Item; colSpan: number }) {
  return (
    <tr className="detail-row">
      <td colSpan={colSpan}>
        <div className="detail" data-testid="item-detail">
          <div className="json-grid">
            <JsonView label="Input" value={item.input} />
            <JsonView label="Expected" value={item.expected_output} />
            <JsonView label="Actual output" value={item.output} />
          </div>
          <h4>Scores</h4>
          {item.scores.length === 0 ? (
            <p className="muted">No scores recorded for this item.</p>
          ) : (
            <ul className="scores">
              {item.scores.map((s) => (
                <li key={`${s.evaluator}@${s.version}`}>
                  <Mono>
                    {s.evaluator}@{s.version}
                  </Mono>
                  <span className="num score-value">{formatScore(s.value)}</span>
                  <span
                    className={`verdict ${s.passed === null ? "verdict-na" : s.passed ? "verdict-pass" : "verdict-fail"}`}
                  >
                    {s.passed === null ? "n/a" : s.passed ? "pass" : "fail"}
                  </span>
                  <span className="rationale">
                    {s.rationale ?? <span className="muted">no rationale</span>}
                  </span>
                </li>
              ))}
            </ul>
          )}
          <dl className="facts facts-inline">
            <div>
              <dt>Attempts</dt>
              <dd className="num">{item.attempt}</dd>
            </div>
            <div>
              <dt>Dataset item</dt>
              <dd className="mono">{item.dataset_item_id}</dd>
            </div>
            {item.tags.length > 0 && (
              <div>
                <dt>Tags</dt>
                <dd>{item.tags.join(", ")}</dd>
              </div>
            )}
          </dl>
          {item.last_error ? (
            <div className="state state-error" role="note">
              <strong>Last error</strong>
              <pre className="mono">{item.last_error}</pre>
            </div>
          ) : (
            <p className="muted small">No error recorded.</p>
          )}
        </div>
      </td>
    </tr>
  );
}

function ItemRow({
  item,
  open,
  toggle,
  colSpan,
}: {
  item: Item;
  open: boolean;
  toggle: () => void;
  colSpan: number;
}) {
  const failing = item.scores.some((s) => s.passed === false);
  return (
    <Fragment>
      <tr className={failing || item.status === "dead_lettered" ? "row-fail" : undefined}>
        <td>
          <button
            type="button"
            className="expander"
            aria-expanded={open}
            aria-label={`${open ? "Collapse" : "Expand"} item ${shortId(item.dataset_item_id)}`}
            onClick={toggle}
          >
            <span aria-hidden="true">{open ? "▾" : "▸"}</span>
          </button>
        </td>
        <td>
          <Mono title={item.dataset_item_id}>{shortId(item.dataset_item_id)}</Mono>
        </td>
        <td>
          <StatusPill status={item.status} />
        </td>
        <td>
          <div className="chips">
            {item.scores.map((s) => (
              <ScoreChip key={`${s.evaluator}@${s.version}`} score={s} />
            ))}
            {item.scores.length === 0 && <span className="muted">no scores</span>}
          </div>
        </td>
        <td>{item.tags.join(", ") || <span className="muted">-</span>}</td>
        <td className="num right">{item.attempt}</td>
        <td className="num right">{formatMs(item.latency_ms)}</td>
        <td className="num right">{formatUsd(toNumber(item.cost_usd))}</td>
      </tr>
      {open && <ItemDetail item={item} colSpan={colSpan} />}
    </Fragment>
  );
}

export function ItemsPage() {
  const { id = "" } = useParams();
  const [params, setParams] = useSearchParams();
  const filters = {
    status: params.get("status") ?? "",
    failed_evaluator: params.get("failed_evaluator") ?? "",
    tag: params.get("tag") ?? "",
  };
  const run = useRun(id);
  const items = useRunItems(id, filters);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [tagDraft, setTagDraft] = useState(filters.tag);

  function setFilter(name: string, value: string) {
    const next = new URLSearchParams(params);
    if (value) next.set(name, value);
    else next.delete(name);
    setParams(next, { replace: true });
    setExpanded(new Set());
  }

  function submitTag(e: FormEvent) {
    e.preventDefault();
    setFilter("tag", tagDraft.trim());
  }

  const rows = items.data?.pages.flatMap((p) => p.items) ?? [];
  const filtered = Boolean(filters.status || filters.failed_evaluator || filters.tag);
  const colSpan = 8;

  return (
    <>
      <nav className="crumbs" aria-label="Breadcrumb">
        <Link to="/">Runs</Link> <span aria-hidden="true">/</span>{" "}
        <Link to={`/runs/${id}`} className="mono">
          {shortId(id)}
        </Link>{" "}
        <span aria-hidden="true">/</span> Items
      </nav>
      <div className="page-head">
        <h1>Items</h1>
      </div>

      <form className="filters" onSubmit={submitTag} aria-label="Item filters">
        <label>
          <span>Failed evaluator</span>
          <select
            value={filters.failed_evaluator}
            onChange={(e) => setFilter("failed_evaluator", e.target.value)}
          >
            <option value="">Any</option>
            {run.data?.evaluators.map((e) => (
              <option key={e.name} value={e.name}>
                {e.name}
              </option>
            ))}
            {filters.failed_evaluator &&
              !run.data?.evaluators.some((e) => e.name === filters.failed_evaluator) && (
                <option value={filters.failed_evaluator}>{filters.failed_evaluator}</option>
              )}
          </select>
        </label>
        <label>
          <span>Status</span>
          <select value={filters.status} onChange={(e) => setFilter("status", e.target.value)}>
            <option value="">Any</option>
            {STATUSES.map((s) => (
              <option key={s} value={s}>
                {s.replace(/_/g, " ")}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>Tag</span>
          <input
            type="text"
            value={tagDraft}
            onChange={(e) => setTagDraft(e.target.value)}
            onBlur={() => tagDraft.trim() !== filters.tag && setFilter("tag", tagDraft.trim())}
            placeholder="e.g. billing"
          />
        </label>
        <button type="submit" className="btn">
          Apply
        </button>
        {filtered && (
          <button
            type="button"
            className="btn btn-quiet"
            onClick={() => {
              setTagDraft("");
              setParams({}, { replace: true });
              setExpanded(new Set());
            }}
          >
            Clear filters
          </button>
        )}
      </form>

      {items.isPending ? (
        <Loading label="Loading items" />
      ) : items.isError ? (
        <ErrorBox error={items.error} onRetry={() => void items.refetch()} />
      ) : rows.length === 0 ? (
        <Empty title={filtered ? "No items match these filters" : "No items in this run"}>
          {filtered ? "Try clearing a filter." : "Items appear once the run is queued."}
        </Empty>
      ) : (
        <>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th aria-label="Expand" />
                  <th>Item</th>
                  <th>Status</th>
                  <th>Scores</th>
                  <th>Tags</th>
                  <th className="right">Attempts</th>
                  <th className="right">Latency</th>
                  <th className="right">Cost</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((item) => (
                  <ItemRow
                    key={item.id}
                    item={item}
                    colSpan={colSpan}
                    open={expanded.has(item.id)}
                    toggle={() =>
                      setExpanded((prev) => {
                        const next = new Set(prev);
                        if (next.has(item.id)) next.delete(item.id);
                        else next.add(item.id);
                        return next;
                      })
                    }
                  />
                ))}
              </tbody>
            </table>
          </div>
          <div className="pager">
            {items.hasNextPage ? (
              <button
                type="button"
                className="btn"
                onClick={() => void items.fetchNextPage()}
                disabled={items.isFetchingNextPage}
              >
                {items.isFetchingNextPage ? "Loading…" : "Load more"}
              </button>
            ) : (
              <span className="muted">{rows.length} items, end of list</span>
            )}
          </div>
        </>
      )}
    </>
  );
}
