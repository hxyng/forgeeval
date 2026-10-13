// Geometry for the 95% confidence-interval bar on the compare page. Kept free of React so the
// zero-crossing and polarity rules can be unit tested.

export type Direction = "improved" | "regressed" | "unchanged";

export interface BarInput {
  delta: number | null;
  low: number | null;
  high: number | null;
  higherIsBetter: boolean;
  /** Server verdict on significance; derived from the interval when omitted. */
  significant?: boolean;
  width?: number;
  pad?: number;
}

export interface BarGeometry {
  width: number;
  /** Symmetric value range shown: [-extent, +extent]. Zero is always in the middle. */
  extent: number;
  zeroX: number;
  lowX: number;
  highX: number;
  deltaX: number;
  crossesZero: boolean;
  direction: Direction;
}

/**
 * Improved/regressed only when the interval excludes zero, and then by polarity:
 * for higher-is-better metrics a positive delta improves, for lower-is-better a negative one.
 */
export function classifyDirection(
  delta: number,
  low: number,
  high: number,
  higherIsBetter: boolean,
  significant?: boolean,
): Direction {
  const crosses = Math.min(low, high) <= 0 && Math.max(low, high) >= 0;
  const sig = significant ?? !crosses;
  if (!sig || delta === 0) return "unchanged";
  const better = higherIsBetter ? delta > 0 : delta < 0;
  return better ? "improved" : "regressed";
}

export function intervalGeometry(input: BarInput): BarGeometry | null {
  const { delta, higherIsBetter, significant, width = 240, pad = 10 } = input;
  if (delta === null || input.low === null || input.high === null) return null;
  const low = Math.min(input.low, input.high);
  const high = Math.max(input.low, input.high);
  const biggest = Math.max(Math.abs(low), Math.abs(high), Math.abs(delta));
  const extent = biggest > 0 ? biggest * 1.15 : 1;
  const inner = width - 2 * pad;
  const x = (v: number) => pad + ((v + extent) / (2 * extent)) * inner;
  return {
    width,
    extent,
    zeroX: x(0),
    lowX: x(low),
    highX: x(high),
    deltaX: x(delta),
    crossesZero: low <= 0 && high >= 0,
    direction: classifyDirection(delta, low, high, higherIsBetter, significant),
  };
}
