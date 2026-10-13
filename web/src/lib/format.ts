import type { Schemas } from "../api/client";

const DASH = "—";
const MINUS = "−";

export function toNumber(v: string | number | null | undefined): number | null {
  if (v === null || v === undefined || v === "") return null;
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : null;
}

/** "5 minutes ago", "in 2 hours", "just now". Pure: `now` is injectable for tests. */
export function relativeTime(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return DASH;
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return DASH;
  const diff = Math.round((now - t) / 1000);
  const abs = Math.abs(diff);
  if (abs < 5) return "just now";
  const units: [number, string][] = [
    [60, "second"],
    [3600, "minute"],
    [86400, "hour"],
    [86400 * 30, "day"],
    [86400 * 365, "month"],
    [Infinity, "year"],
  ];
  const divisors = [1, 60, 3600, 86400, 86400 * 30, 86400 * 365];
  let i = 0;
  while (i < units.length - 1 && abs >= (units[i] as [number, string])[0]) i++;
  const n = Math.floor(abs / (divisors[i] as number));
  const label = `${n} ${(units[i] as [number, string])[1]}${n === 1 ? "" : "s"}`;
  return diff >= 0 ? `${label} ago` : `in ${label}`;
}

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return DASH;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? DASH : d.toLocaleString();
}

/** Wall-clock span between two timestamps, e.g. "6.1 s" or "2 min 5 s". */
export function formatSpan(
  start: string | null,
  end: string | null,
  now: number = Date.now(),
): string {
  if (!start) return DASH;
  const s = Date.parse(start);
  const e = end ? Date.parse(end) : now;
  if (Number.isNaN(s) || Number.isNaN(e)) return DASH;
  const sec = Math.max(0, (e - s) / 1000);
  if (sec < 60) return `${sec < 10 ? sec.toFixed(1) : Math.round(sec)} s`;
  const m = Math.floor(sec / 60);
  const r = Math.round(sec % 60);
  if (m < 60) return `${m} min ${r} s`;
  return `${Math.floor(m / 60)} h ${m % 60} min`;
}

export function formatMs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return DASH;
  if (Math.abs(ms) >= 1000) return `${trim((ms / 1000).toFixed(2))} s`;
  return `${trim(ms.toFixed(ms % 1 === 0 ? 0 : 1))} ms`;
}

/** USD with enough precision for sub-cent LLM costs. */
export function formatUsd(v: string | number | null | undefined): string {
  const n = toNumber(v);
  if (n === null) return DASH;
  if (n === 0) return "$0.00";
  const abs = Math.abs(n);
  if (abs >= 1) return `${n < 0 ? MINUS : ""}$${abs.toFixed(2)}`;
  if (abs >= 0.01) return `${n < 0 ? MINUS : ""}$${abs.toFixed(3)}`;
  return `${n < 0 ? MINUS : ""}$${abs.toFixed(6)}`;
}

export function formatRate(rate: number | null | undefined): string {
  if (rate === null || rate === undefined || !Number.isFinite(rate)) return DASH;
  return `${trim((rate * 100).toFixed(1))}%`;
}

/** Plain score, up to 3 decimals, no trailing zeros. */
export function formatScore(v: string | number | null | undefined): string {
  const n = toNumber(v);
  if (n === null) return DASH;
  return trim(n.toFixed(3));
}

function trim(s: string): string {
  return s.includes(".") ? s.replace(/0+$/, "").replace(/\.$/, "") : s;
}

type Kind = Schemas["EvaluatorKind"];

/**
 * Polarity-aware value formatting: latency is milliseconds, cost is USD,
 * everything else is a 0..1-style score.
 */
export function formatMetric(kind: Kind, v: number | string | null | undefined): string {
  const n = toNumber(v);
  if (n === null) return DASH;
  if (kind === "latency") return formatMs(n);
  if (kind === "cost") return formatUsd(n);
  return formatScore(n);
}

/** Signed delta in the same unit as the metric. */
export function formatDelta(kind: Kind, v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return DASH;
  if (v === 0) return formatMetric(kind, 0);
  const sign = v > 0 ? "+" : MINUS;
  return `${sign}${formatMetric(kind, Math.abs(v))}`;
}

export function shortSha(sha: string | null | undefined): string {
  return sha ? sha.slice(0, 7) : DASH;
}

/** UUIDv7 ids share their leading timestamp bits, so the random tail is what tells them apart. */
export function shortId(id: string | null | undefined): string {
  return id ? id.slice(-8) : DASH;
}

export function pluralize(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`;
}
