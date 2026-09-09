/**
 * Dependency-free inline-SVG line chart for a `{t, v}` time series — PS bullet 6 names
 * "charts" explicitly, and until now nothing in `frontend/src` drew one (verified: no
 * chart library in package.json, no chart component anywhere). No npm install needed:
 * one series, one polyline, real retrieved points — exactly what
 * `get_productivity_history`'s `payload.series.argo_temperature` already is
 * (backend/foreshore/tools/productivity.py), never an LLM-drawn picture of a trend.
 */
interface Point {
  t: string;
  v: number;
}

interface TimeSeriesChartProps {
  title: string;
  unit: string;
  points: Point[];
  color?: string;
}

const WIDTH = 480;
const HEIGHT = 140;
const PAD = { top: 12, right: 12, bottom: 22, left: 44 };

export default function TimeSeriesChart({ title, unit, points, color = "#3fb8b8" }: TimeSeriesChartProps) {
  if (points.length < 2) return null;

  const times = points.map((p) => new Date(p.t).getTime());
  const values = points.map((p) => p.v);
  const minT = Math.min(...times);
  const maxT = Math.max(...times);
  const minV = Math.min(...values);
  const maxV = Math.max(...values);
  const spanV = maxV - minV || 1;
  const spanT = maxT - minT || 1;

  const plotW = WIDTH - PAD.left - PAD.right;
  const plotH = HEIGHT - PAD.top - PAD.bottom;

  const x = (t: number) => PAD.left + ((t - minT) / spanT) * plotW;
  const y = (v: number) => PAD.top + plotH - ((v - minV) / spanV) * plotH;

  const path = points
    .map((p, i) => `${i === 0 ? "M" : "L"}${x(new Date(p.t).getTime()).toFixed(1)},${y(p.v).toFixed(1)}`)
    .join(" ");

  const fmtDate = (ms: number) => new Date(ms).toLocaleDateString(undefined, { year: "numeric", month: "short" });

  return (
    <figure className="ts-chart">
      <figcaption className="ts-chart__caption">
        {title} <span className="ts-chart__unit">({unit})</span>
      </figcaption>
      <svg
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        role="img"
        aria-label={`${title}: ${points.length} points from ${fmtDate(minT)} to ${fmtDate(maxT)}, ranging ${minV.toFixed(2)} to ${maxV.toFixed(2)} ${unit}`}
      >
        {/* Two horizontal gridlines: min and max value, labelled — enough to read the
            chart without a full axis, which would crowd a 140px-tall card. */}
        <line x1={PAD.left} x2={WIDTH - PAD.right} y1={y(minV)} y2={y(minV)} className="ts-chart__grid" />
        <line x1={PAD.left} x2={WIDTH - PAD.right} y1={y(maxV)} y2={y(maxV)} className="ts-chart__grid" />
        <text x={PAD.left - 6} y={y(maxV)} className="ts-chart__axis-label" textAnchor="end" dominantBaseline="middle">
          {maxV.toFixed(2)}
        </text>
        <text x={PAD.left - 6} y={y(minV)} className="ts-chart__axis-label" textAnchor="end" dominantBaseline="middle">
          {minV.toFixed(2)}
        </text>
        <text x={PAD.left} y={HEIGHT - 4} className="ts-chart__axis-label">
          {fmtDate(minT)}
        </text>
        <text x={WIDTH - PAD.right} y={HEIGHT - 4} className="ts-chart__axis-label" textAnchor="end">
          {fmtDate(maxT)}
        </text>
        <path d={path} className="ts-chart__line" style={{ stroke: color }} />
        {points.map((p, i) => (
          <circle key={i} cx={x(new Date(p.t).getTime())} cy={y(p.v)} r={2} className="ts-chart__point" style={{ fill: color }} />
        ))}
      </svg>
    </figure>
  );
}
