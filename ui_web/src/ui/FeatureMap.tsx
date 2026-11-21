// SVG map of one scan's features in scan-local XY (lib/map.ts holds the contract and the geometry).
import type { PointerEvent } from "react";
import type { Feature } from "../api/types";
import { fromPx, layoutMap, minSpacingPx, pick, radiusPx, scaleBar, toPx } from "../lib/map";

/** A target's measurement outcome as a map class: go / nogo / unmeasured; "" before it is measured. */
export function outcome(f: Feature): string {
  const m = f.measurement;
  if (!f.is_target) return "";
  if (m.status === "measured") return m.verdict === "in_limits" ? "go" : "nogo";
  return m.status === "not_measured" ? "unmeasured" : "";
}

export function FeatureMap({ features, width, height, selected, onSelect, compact }: {
  features: Feature[]; width: number; height: number;
  selected?: number | null; onSelect?: (resultSeq: number | null) => void; compact?: boolean;
}) {
  const l = layoutMap(features, width, height, compact ? 14 : 26, 46);
  const onDown = (e: PointerEvent<SVGSVGElement>) => {
    if (!onSelect) return;
    const r = e.currentTarget.getBoundingClientRect();
    const i = pick(features, l, (e.clientX - r.left) * (width / r.width), (e.clientY - r.top) * (height / r.height));
    onSelect(i === null ? null : features[i].result_seq);
  };
  const bar = scaleBar(l.scale, compact ? 50 : 80);
  // ids only where they do not run into the neighbouring circles
  const showIds = !compact && features.length <= 80 && minSpacingPx(features, l) >= 30;
  const [cornerX, cornerY] = fromPx(l, 0, 0);
  return (
    <svg className="map" width={width} height={height} viewBox={`0 0 ${width} ${height}`} onPointerDown={onDown}
         role="img" aria-label={`Map of ${features.length} features`}>
      <rect className="map-bg" x={0} y={0} width={width} height={height} rx={6} />
      {l.empty ? (
        <text className="map-empty" x={width / 2} y={height / 2} textAnchor="middle">No features detected</text>
      ) : (
        <>
          {features.map((f) => {
            const [cx, cy] = toPx(l, f.x_mm, f.y_mm);
            const r = radiusPx(l, f);
            const cls = ["feat", f.is_target ? "target" : "other", f.partial ? "partial" : "", outcome(f),
              selected === f.result_seq ? "selected" : ""].join(" ");
            return (
              <g key={f.result_seq} className={cls} data-feature={f.feature_id}>
                {selected === f.result_seq && <circle className="sel-ring" cx={cx} cy={cy} r={r + 6} />}
                <circle cx={cx} cy={cy} r={r} />
                {showIds && <text className="feat-id" x={cx + r + 3} y={cy - r + 2}>{f.feature_id}</text>}
              </g>
            );
          })}
          {!compact && (
            <text className="map-corner" x={8} y={16}>
              top-left x {cornerX.toFixed(0)} · y {cornerY.toFixed(0)} mm (scan-local, not a part origin)
            </text>
          )}
          <g className="map-axes" transform={`translate(12 ${height - 12})`}>
            <line x1={0} y1={0} x2={28} y2={0} markerEnd="url(#arrow)" />
            <text x={32} y={4}>+X</text>
            <line x1={0} y1={-34} x2={0} y2={-6} markerEnd="url(#arrow)" />
            <text x={4} y={-36}>+Y</text>
          </g>
          <g className="map-scale" transform={`translate(${width - 12 - bar.px} ${height - 14})`}>
            <line x1={0} y1={0} x2={bar.px} y2={0} />
            <line x1={0} y1={-4} x2={0} y2={4} />
            <line x1={bar.px} y1={-4} x2={bar.px} y2={4} />
            <text x={bar.px / 2} y={-6} textAnchor="middle">{bar.mm} mm</text>
          </g>
        </>
      )}
      <defs>
        <marker id="arrow" viewBox="0 0 10 10" refX={8} refY={5} markerWidth={6} markerHeight={6} orient="auto-start-reverse">
          <path d="M 0 0 L 10 5 L 0 10 z" className="arrow-head" />
        </marker>
      </defs>
    </svg>
  );
}
