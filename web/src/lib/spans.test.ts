import { describe, expect, it } from "vitest";
import type { Schemas } from "../api/client";
import { buildSpanRows } from "./spans";

const span = (
  id: string,
  parent: string | null,
  startMs: number,
  dur: number,
): Schemas["SpanOut"] => ({
  id,
  parent_span_id: parent,
  kind: "other",
  name: id,
  started_at: new Date(Date.UTC(2026, 9, 12, 12, 0, 0, startMs)).toISOString(),
  duration_ms: dur,
  attributes: {},
});

describe("buildSpanRows", () => {
  it("nests children under parents and orders siblings by start", () => {
    const rows = buildSpanRows([
      span("child-b", "root", 60, 10),
      span("root", null, 0, 100),
      span("child-a", "root", 10, 30),
      span("grandchild", "child-a", 15, 5),
    ]);
    expect(rows.map((r) => [r.span.id, r.depth])).toEqual([
      ["root", 0],
      ["child-a", 1],
      ["grandchild", 2],
      ["child-b", 1],
    ]);
  });

  it("positions bars as percentages of the trace window", () => {
    const rows = buildSpanRows([span("root", null, 0, 200), span("kid", "root", 50, 100)]);
    expect(rows[0]).toMatchObject({ leftPct: 0, widthPct: 100 });
    expect(rows[1]?.leftPct).toBeCloseTo(25);
    expect(rows[1]?.widthPct).toBeCloseTo(50);
  });

  it("shows orphans as roots and survives cycles", () => {
    const orphan = buildSpanRows([span("a", "missing", 0, 5)]);
    expect(orphan).toHaveLength(1);
    expect(orphan[0]?.depth).toBe(0);
    const cyc = buildSpanRows([span("a", "b", 0, 5), span("b", "a", 1, 5)]);
    expect(cyc.length).toBeLessThanOrEqual(2);
  });

  it("keeps zero-duration spans visible", () => {
    const rows = buildSpanRows([span("root", null, 0, 100), span("blip", "root", 10, 0)]);
    expect(rows[1]?.widthPct).toBeGreaterThanOrEqual(0.5);
  });

  it("returns nothing for no spans", () => {
    expect(buildSpanRows([])).toEqual([]);
  });
});
