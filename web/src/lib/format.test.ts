import { describe, expect, it } from "vitest";
import {
  formatDelta,
  formatMetric,
  formatMs,
  formatRate,
  formatScore,
  formatSpan,
  formatUsd,
  relativeTime,
  shortId,
  shortSha,
} from "./format";
import { summarizeProgress } from "./progress";

const NOW = Date.parse("2026-10-12T12:00:00Z");
const ago = (s: number) => new Date(NOW - s * 1000).toISOString();

describe("relativeTime", () => {
  it("handles recent, singular and plural units", () => {
    expect(relativeTime(ago(2), NOW)).toBe("just now");
    expect(relativeTime(ago(30), NOW)).toBe("30 seconds ago");
    expect(relativeTime(ago(60), NOW)).toBe("1 minute ago");
    expect(relativeTime(ago(5 * 3600), NOW)).toBe("5 hours ago");
    expect(relativeTime(ago(3 * 86400), NOW)).toBe("3 days ago");
  });
  it("handles the future and bad input", () => {
    expect(relativeTime(ago(-120), NOW)).toBe("in 2 minutes");
    expect(relativeTime(null, NOW)).toBe("—");
    expect(relativeTime("not a date", NOW)).toBe("—");
  });
});

describe("number formatting", () => {
  it("formats milliseconds and seconds", () => {
    expect(formatMs(5)).toBe("5 ms");
    expect(formatMs(12.5)).toBe("12.5 ms");
    expect(formatMs(1500)).toBe("1.5 s");
    expect(formatMs(null)).toBe("—");
  });
  it("keeps sub-cent costs readable", () => {
    expect(formatUsd("0.000889")).toBe("$0.000889");
    expect(formatUsd("0.00003")).toBe("$0.000030");
    expect(formatUsd("0.05")).toBe("$0.050");
    expect(formatUsd(12.3456)).toBe("$12.35");
    expect(formatUsd(0)).toBe("$0.00");
    expect(formatUsd(null)).toBe("—");
  });
  it("formats rates and scores", () => {
    expect(formatRate(0.9667)).toBe("96.7%");
    expect(formatRate(1)).toBe("100%");
    expect(formatRate(null)).toBe("—");
    expect(formatScore("0.500")).toBe("0.5");
    expect(formatScore("1")).toBe("1");
    expect(formatScore(0.33333)).toBe("0.333");
  });
  it("is polarity-aware by evaluator kind", () => {
    expect(formatMetric("latency", 42)).toBe("42 ms");
    expect(formatMetric("cost", "0.0012")).toBe("$0.001200");
    expect(formatMetric("exact_match", 0.75)).toBe("0.75");
    expect(formatMetric("exact_match", null)).toBe("—");
  });
  it("signs deltas in the metric's own unit", () => {
    expect(formatDelta("exact_match", -0.05)).toBe("−0.05");
    expect(formatDelta("exact_match", 0.1)).toBe("+0.1");
    expect(formatDelta("latency", 120)).toBe("+120 ms");
    expect(formatDelta("latency", 0)).toBe("0 ms");
    expect(formatDelta("cost", null)).toBe("—");
  });
});

describe("misc helpers", () => {
  it("shortens ids and shas", () => {
    expect(shortSha("9585a58d1f2c9e")).toBe("9585a58");
    expect(shortSha(null)).toBe("—");
    expect(shortId("01a12ea5-133e-7abe-a474-a0fea4c485f4")).toBe("a4c485f4");
  });
  it("formats run durations", () => {
    expect(formatSpan("2026-10-12T12:00:00Z", "2026-10-12T12:00:06Z")).toBe("6.0 s");
    expect(formatSpan("2026-10-12T12:00:00Z", "2026-10-12T12:02:05Z")).toBe("2 min 5 s");
    expect(formatSpan(null, null)).toBe("—");
  });
  it("summarises run progress", () => {
    const s = summarizeProgress(
      { queued: 4, leased: 1, retry_wait: 0, succeeded: 20, dead_lettered: 3, cancelled: 2 },
      30,
    );
    expect(s).toMatchObject({ done: 25, inFlight: 5, failed: 3, total: 30 });
    expect(s.fraction).toBeCloseTo(25 / 30);
  });
});
