import { describe, expect, it } from "vitest";
import { classifyDirection, intervalGeometry } from "./ci";

describe("classifyDirection", () => {
  it("is unchanged when the interval straddles zero", () => {
    expect(classifyDirection(0.02, -0.05, 0.09, true)).toBe("unchanged");
    expect(classifyDirection(-0.02, -0.09, 0.05, false)).toBe("unchanged");
  });

  it("treats an interval touching zero as unchanged", () => {
    expect(classifyDirection(0.1, 0, 0.2, true)).toBe("unchanged");
  });

  it("colours by polarity when the interval excludes zero", () => {
    // Higher is better: up is good, down is bad.
    expect(classifyDirection(0.1, 0.05, 0.15, true)).toBe("improved");
    expect(classifyDirection(-0.1, -0.15, -0.05, true)).toBe("regressed");
    // Lower is better (latency, cost): up is bad, down is good.
    expect(classifyDirection(40, 20, 60, false)).toBe("regressed");
    expect(classifyDirection(-40, -60, -20, false)).toBe("improved");
  });

  it("defers to the server's significance flag when given", () => {
    expect(classifyDirection(0.1, 0.05, 0.15, true, false)).toBe("unchanged");
    expect(classifyDirection(0, -1, 1, true, true)).toBe("unchanged");
  });
});

describe("intervalGeometry", () => {
  it("returns null without an interval", () => {
    expect(intervalGeometry({ delta: 0.1, low: null, high: 0.2, higherIsBetter: true })).toBeNull();
    expect(intervalGeometry({ delta: null, low: 0, high: 0.2, higherIsBetter: true })).toBeNull();
  });

  it("keeps the zero line centred and orders low < delta < high", () => {
    const g = intervalGeometry({ delta: -0.1, low: -0.3, high: 0.05, higherIsBetter: true })!;
    expect(g.zeroX).toBeCloseTo(g.width / 2);
    expect(g.lowX).toBeLessThan(g.deltaX);
    expect(g.deltaX).toBeLessThan(g.highX);
    expect(g.crossesZero).toBe(true);
    expect(g.direction).toBe("unchanged");
  });

  it("puts a wholly negative interval left of zero", () => {
    const g = intervalGeometry({ delta: -0.5, low: -0.7, high: -0.3, higherIsBetter: true })!;
    expect(g.highX).toBeLessThan(g.zeroX);
    expect(g.crossesZero).toBe(false);
    expect(g.direction).toBe("regressed");
  });

  it("flips colour with polarity for the same geometry", () => {
    const base = { delta: -30, low: -50, high: -10 };
    expect(intervalGeometry({ ...base, higherIsBetter: false })!.direction).toBe("improved");
    expect(intervalGeometry({ ...base, higherIsBetter: true })!.direction).toBe("regressed");
  });

  it("stays inside the drawing area", () => {
    const g = intervalGeometry({
      delta: 5,
      low: -100,
      high: 9,
      higherIsBetter: true,
      width: 200,
      pad: 10,
    })!;
    for (const x of [g.lowX, g.highX, g.deltaX, g.zeroX]) {
      expect(x).toBeGreaterThanOrEqual(10);
      expect(x).toBeLessThanOrEqual(190);
    }
  });

  it("handles a degenerate zero-width interval at zero", () => {
    const g = intervalGeometry({ delta: 0, low: 0, high: 0, higherIsBetter: true })!;
    expect(g.lowX).toBe(g.highX);
    expect(g.lowX).toBeCloseTo(g.zeroX);
    expect(g.direction).toBe("unchanged");
  });

  it("treats a zero-width interval away from zero as a real change", () => {
    const g = intervalGeometry({ delta: -1, low: -1, high: -1, higherIsBetter: true })!;
    expect(g.crossesZero).toBe(false);
    expect(g.direction).toBe("regressed");
  });
});
