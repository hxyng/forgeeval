import type { Schemas } from "../api/client";

export interface ProgressSummary {
  total: number;
  succeeded: number;
  failed: number;
  cancelled: number;
  inFlight: number;
  done: number;
  /** 0..1 share of items that reached a final state. */
  fraction: number;
}

export function summarizeProgress(p: Schemas["Progress"], total: number): ProgressSummary {
  const succeeded = p.succeeded ?? 0;
  const failed = p.dead_lettered ?? 0;
  const cancelled = p.cancelled ?? 0;
  const inFlight = (p.queued ?? 0) + (p.leased ?? 0) + (p.retry_wait ?? 0);
  const done = succeeded + failed + cancelled;
  const denom = total > 0 ? total : done + inFlight;
  return {
    total: denom,
    succeeded,
    failed,
    cancelled,
    inFlight,
    done,
    fraction: denom > 0 ? Math.min(1, done / denom) : 0,
  };
}
