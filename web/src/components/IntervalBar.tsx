import { intervalGeometry } from "../lib/ci";

interface Props {
  delta: number | null;
  low: number | null;
  high: number | null;
  higherIsBetter: boolean;
  significant?: boolean;
  /** Text used for the accessible name, e.g. "−0.05". */
  label: string;
}

/**
 * Inline SVG interval: the 95% CI as a whisker, the paired delta as a dot, and a vertical line at
 * zero. Colour follows direction (improved/regressed/unchanged), which already accounts for polarity.
 */
export function IntervalBar({ delta, low, high, higherIsBetter, significant, label }: Props) {
  const g = intervalGeometry({ delta, low, high, higherIsBetter, significant });
  if (!g) {
    return <span className="muted small">no interval (too few pairs)</span>;
  }
  const h = 26;
  const mid = h / 2;
  return (
    <svg
      className={`ci-bar ci-${g.direction}`}
      width={g.width}
      height={h}
      viewBox={`0 0 ${g.width} ${h}`}
      role="img"
      aria-label={`${g.direction}: delta ${label}`}
      data-direction={g.direction}
      data-crosses-zero={g.crossesZero}
    >
      <line className="ci-axis" x1={0} x2={g.width} y1={mid} y2={mid} />
      <line className="ci-zero" x1={g.zeroX} x2={g.zeroX} y1={2} y2={h - 2} />
      <line className="ci-whisker" x1={g.lowX} x2={g.highX} y1={mid} y2={mid} />
      <line className="ci-cap" x1={g.lowX} x2={g.lowX} y1={mid - 5} y2={mid + 5} />
      <line className="ci-cap" x1={g.highX} x2={g.highX} y1={mid - 5} y2={mid + 5} />
      <circle className="ci-point" cx={g.deltaX} cy={mid} r={3.5} />
    </svg>
  );
}
