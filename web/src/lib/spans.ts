import type { Schemas } from "../api/client";

type Span = Schemas["SpanOut"];

export interface SpanRow {
  span: Span;
  depth: number;
  /** Offset and width as percentages of the whole trace window. */
  leftPct: number;
  widthPct: number;
}

/** Depth-first span tree: children under their parent, siblings by start time. */
export function buildSpanRows(spans: Span[]): SpanRow[] {
  if (spans.length === 0) return [];
  const start = (s: Span) => Date.parse(s.started_at);
  const t0 = Math.min(...spans.map(start));
  const t1 = Math.max(...spans.map((s) => start(s) + s.duration_ms));
  const total = Math.max(1, t1 - t0);

  const ids = new Set(spans.map((s) => s.id));
  const byParent = new Map<string | null, Span[]>();
  for (const s of spans) {
    // A span whose parent is missing from the payload is shown as a root.
    const key = s.parent_span_id && ids.has(s.parent_span_id) ? s.parent_span_id : null;
    const list = byParent.get(key) ?? [];
    list.push(s);
    byParent.set(key, list);
  }
  for (const list of byParent.values()) list.sort((a, b) => start(a) - start(b));

  const rows: SpanRow[] = [];
  const seen = new Set<string>();
  const walk = (parent: string | null, depth: number) => {
    for (const span of byParent.get(parent) ?? []) {
      if (seen.has(span.id)) continue; // guards against cycles in bad data
      seen.add(span.id);
      rows.push({
        span,
        depth,
        leftPct: ((start(span) - t0) / total) * 100,
        widthPct: Math.max(0.5, (span.duration_ms / total) * 100),
      });
      walk(span.id, depth + 1);
    }
  };
  walk(null, 0);
  return rows;
}
