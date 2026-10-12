import type { ReactNode } from "react";
import type { Schemas } from "../api/client";
import { summarizeProgress } from "../lib/progress";

export function StatusPill({ status }: { status: string }) {
  return (
    <span className={`pill pill-${status.replace(/_/g, "-")}`} data-status={status}>
      <span className="pill-dot" aria-hidden="true" />
      {status.replace(/_/g, " ")}
    </span>
  );
}

export function Loading({ label = "Loading" }: { label?: string }) {
  return (
    <div className="state" role="status" aria-live="polite">
      <span className="spinner" aria-hidden="true" />
      {label}…
    </div>
  );
}

export function ErrorBox({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const message = error instanceof Error ? error.message : "Something went wrong.";
  return (
    <div className="state state-error" role="alert">
      <strong>Could not load this.</strong> <span>{message}</span>
      {onRetry && (
        <button type="button" className="btn btn-small" onClick={onRetry}>
          Retry
        </button>
      )}
    </div>
  );
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="state state-empty">
      <strong>{title}</strong>
      {children && <p>{children}</p>}
    </div>
  );
}

export function ProgressBar({ progress, total }: { progress: Schemas["Progress"]; total: number }) {
  const s = summarizeProgress(progress, total);
  const pct = (n: number) => `${s.total > 0 ? (n / s.total) * 100 : 0}%`;
  return (
    <div className="progress">
      <div
        className="progress-bar"
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={s.total}
        aria-valuenow={s.done}
        aria-label={`${s.done} of ${s.total} items finished`}
        title={`${s.succeeded} succeeded, ${s.failed} dead-lettered, ${s.cancelled} cancelled, ${s.inFlight} pending`}
      >
        <span className="seg seg-good" style={{ width: pct(s.succeeded) }} />
        <span className="seg seg-bad" style={{ width: pct(s.failed) }} />
        <span className="seg seg-muted" style={{ width: pct(s.cancelled) }} />
      </div>
      <span className="progress-label num">
        {s.done}/{s.total}
      </span>
    </div>
  );
}

/** Formatted JSON; plain strings are shown as text so long outputs stay readable. */
export function JsonView({ value, label }: { value: unknown; label: string }) {
  let body: string;
  let muted = false;
  if (value === null || value === undefined) {
    body = "null";
    muted = true;
  } else if (typeof value === "string") {
    body = value;
  } else {
    body = JSON.stringify(value, null, 2);
  }
  return (
    <section className="json-pane" aria-label={label}>
      <h4>{label}</h4>
      <pre className={`mono${muted ? " muted" : ""}`} tabIndex={0}>
        {body}
      </pre>
    </section>
  );
}

export function Mono({ children, title }: { children: ReactNode; title?: string }) {
  return (
    <span className="mono" title={title}>
      {children}
    </span>
  );
}

export function Facts({ items }: { items: [string, ReactNode][] }) {
  return (
    <dl className="facts">
      {items.map(([k, v]) => (
        <div key={k}>
          <dt>{k}</dt>
          <dd>{v}</dd>
        </div>
      ))}
    </dl>
  );
}
