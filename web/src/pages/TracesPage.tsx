import { useState } from "react";
import type { FormEvent } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { useTrace, useTraces } from "../api/queries";
import { Empty, ErrorBox, Facts, JsonView, Loading, Mono, StatusPill } from "../components/ui";
import { formatDateTime, formatMs, formatUsd, relativeTime, shortId } from "../lib/format";
import { buildSpanRows } from "../lib/spans";

export function TracesPage() {
  const [params, setParams] = useSearchParams();
  const filters = { status: params.get("status") ?? "", model: params.get("model") ?? "" };
  const [modelDraft, setModelDraft] = useState(filters.model);
  const q = useTraces(filters);
  const rows = q.data?.pages.flatMap((p) => p.items) ?? [];
  const filtered = Boolean(filters.status || filters.model);

  function setFilter(name: string, value: string) {
    const next = new URLSearchParams(params);
    if (value) next.set(name, value);
    else next.delete(name);
    setParams(next, { replace: true });
  }

  function submit(e: FormEvent) {
    e.preventDefault();
    setFilter("model", modelDraft.trim());
  }

  return (
    <>
      <div className="page-head">
        <h1>Traces</h1>
      </div>
      <form className="filters" onSubmit={submit} aria-label="Trace filters">
        <label>
          <span>Status</span>
          <select value={filters.status} onChange={(e) => setFilter("status", e.target.value)}>
            <option value="">Any</option>
            <option value="ok">ok</option>
            <option value="error">error</option>
          </select>
        </label>
        <label>
          <span>Model</span>
          <input
            type="text"
            value={modelDraft}
            onChange={(e) => setModelDraft(e.target.value)}
            onBlur={() =>
              modelDraft.trim() !== filters.model && setFilter("model", modelDraft.trim())
            }
            placeholder="e.g. fake/echo"
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
              setModelDraft("");
              setParams({}, { replace: true });
            }}
          >
            Clear filters
          </button>
        )}
      </form>

      {q.isPending ? (
        <Loading label="Loading traces" />
      ) : q.isError ? (
        <ErrorBox error={q.error} onRetry={() => void q.refetch()} />
      ) : rows.length === 0 ? (
        <Empty title={filtered ? "No traces match these filters" : "No traces captured yet"}>
          {filtered
            ? "Try clearing a filter."
            : "Instrument an app with the SDK to capture traces."}
        </Empty>
      ) : (
        <>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Status</th>
                  <th>Trace</th>
                  <th>Model</th>
                  <th className="right">Latency</th>
                  <th className="right">Tokens in / out</th>
                  <th className="right">Cost</th>
                  <th>Started</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((t) => (
                  <tr key={t.id}>
                    <td>
                      <StatusPill status={t.status} />
                    </td>
                    <td>
                      <Link to={`/traces/${t.id}`} className="mono" title={t.id}>
                        {shortId(t.id)}
                      </Link>
                    </td>
                    <td>
                      <Mono>{t.model}</Mono>
                    </td>
                    <td className="num right">{formatMs(t.latency_ms)}</td>
                    <td className="num right">
                      {t.input_tokens ?? "—"} / {t.output_tokens ?? "—"}
                    </td>
                    <td className="num right">{formatUsd(t.cost_usd)}</td>
                    <td title={formatDateTime(t.started_at)}>{relativeTime(t.started_at)}</td>
                  </tr>
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
              <span className="muted">{rows.length} traces, end of list</span>
            )}
          </div>
        </>
      )}
    </>
  );
}

export function TraceDetailPage() {
  const { id = "" } = useParams();
  const q = useTrace(id);
  if (q.isPending) return <Loading label="Loading trace" />;
  if (q.isError) return <ErrorBox error={q.error} onRetry={() => void q.refetch()} />;
  const t = q.data;
  const rows = buildSpanRows(t.spans);

  return (
    <>
      <nav className="crumbs" aria-label="Breadcrumb">
        <Link to="/traces">Traces</Link> <span aria-hidden="true">/</span> <Mono>{t.id}</Mono>
      </nav>
      <div className="page-head">
        <h1>
          Trace <Mono>{shortId(t.id)}</Mono> <StatusPill status={t.status} />
        </h1>
      </div>
      <section className="panel" aria-label="Trace details">
        <h2>Details</h2>
        <Facts
          items={[
            ["Model", <Mono key="m">{t.model}</Mono>],
            [
              "Latency",
              <span key="l" className="num">
                {formatMs(t.latency_ms)}
              </span>,
            ],
            [
              "Tokens in / out",
              <span key="t" className="num">
                {t.input_tokens ?? "—"} / {t.output_tokens ?? "—"}
              </span>,
            ],
            [
              "Cost",
              <span key="c" className="num">
                {formatUsd(t.cost_usd)}
              </span>,
            ],
            ["Started", formatDateTime(t.started_at)],
            ["Ingested", formatDateTime(t.ingested_at)],
            ["Client id", <Mono key="ci">{t.client_id}</Mono>],
            ["OTel trace", t.otel_trace_id ? <Mono key="o">{t.otel_trace_id}</Mono> : "none"],
          ]}
        />
      </section>
      <div className="json-grid json-grid-2">
        <JsonView label="Input" value={t.input} />
        <JsonView label="Output" value={t.output} />
      </div>
      <section className="panel" aria-label="Spans">
        <div className="panel-head">
          <h2>Spans</h2>
          <span className="muted small num">{t.spans.length} total</span>
        </div>
        {rows.length === 0 ? (
          <Empty title="No spans recorded" />
        ) : (
          <ul className="spans" data-testid="span-tree">
            {rows.map(({ span, depth, leftPct, widthPct }) => (
              <li key={span.id} data-depth={depth}>
                <div className="span-name" style={{ paddingLeft: `${depth * 16}px` }}>
                  <span className={`tag mono kind-${span.kind}`}>{span.kind}</span> {span.name}
                </div>
                <div className="span-track" aria-hidden="true">
                  <span
                    className={`span-bar kind-bar-${span.kind}`}
                    style={{ left: `${leftPct}%`, width: `${widthPct}%` }}
                  />
                </div>
                <div className="num muted span-dur">{formatMs(span.duration_ms)}</div>
              </li>
            ))}
          </ul>
        )}
      </section>
    </>
  );
}
