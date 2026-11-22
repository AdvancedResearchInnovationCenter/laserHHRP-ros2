// SVG map of one scan's features in scan-local XY (lib/map.ts holds the contract and the geometry).
import { useEffect, useRef, useState, type PointerEvent, type WheelEvent } from "react";
import type { Feature } from "../api/types";
import { FIT_VIEW, fromPx, keepInView, layoutMap, MAX_ZOOM, minSpacingPx, panBy, pick, radiusPx, scaleBar, toPx,
  zoomAt, zoomed, type MapView } from "../lib/map";

const TAP_SLOP_PX = 8;   // a touch that moves less than this is a tap (select), more is a pan

/** A target's measurement outcome as a map class: go / nogo / unmeasured; "" before it is measured. */
export function outcome(f: Feature): string {
  const m = f.measurement;
  if (!f.is_target) return "";
  if (m.status === "measured") return m.verdict === "in_limits" ? "go" : "nogo";
  return m.status === "not_measured" ? "unmeasured" : "";
}

/** The scan's map. `zoomable` (Results): pinch or + / − to zoom, drag to pan, Fit to see the whole scan again; a
 *  tap still selects. Without it (Scanning, History) the map always shows the whole scan. */
export function FeatureMap({ features, width, height, selected, onSelect, compact, zoomable }: {
  features: Feature[]; width: number; height: number;
  selected?: number | null; onSelect?: (resultSeq: number | null) => void; compact?: boolean; zoomable?: boolean;
}) {
  const fitted = layoutMap(features, width, height, compact ? 14 : 26, 46);
  const [view, setView] = useState<MapView>(FIT_VIEW);
  const v = zoomable ? view : FIT_VIEW;
  const l = zoomed(fitted, v);
  // pointers down (pinch: two), where each started and was last, and whether this gesture moved (pan, not a tap)
  const touch = useRef({ pts: new Map<number, [number, number]>(), start: [0, 0] as [number, number], moved: false });

  // a feature chosen with ‹ › comes into view when zoomed in
  const chosen = features.find((f) => f.result_seq === selected);
  useEffect(() => {
    if (zoomable && chosen) setView((cur) => keepInView(fitted, cur, chosen.x_mm, chosen.y_mm));
  }, [selected]);

  const local = (e: PointerEvent<SVGSVGElement> | WheelEvent<SVGSVGElement>): [number, number] => {
    const r = e.currentTarget.getBoundingClientRect();
    return [(e.clientX - r.left) * (width / r.width), (e.clientY - r.top) * (height / r.height)];
  };
  const select = (p: [number, number]) => {
    if (!onSelect) return;
    const i = pick(features, l, p[0], p[1]);
    onSelect(i === null ? null : features[i].result_seq);
  };
  const onDown = (e: PointerEvent<SVGSVGElement>) => {
    if (!zoomable) return select(local(e));
    e.currentTarget.setPointerCapture?.(e.pointerId);
    const t = touch.current;
    if (t.pts.size === 0) { t.start = local(e); t.moved = false; }
    t.pts.set(e.pointerId, local(e));
  };
  const onMove = (e: PointerEvent<SVGSVGElement>) => {
    const t = touch.current;
    const before = t.pts.get(e.pointerId);
    if (!zoomable || !before) return;
    const now = local(e);
    if (t.pts.size >= 2) {               // pinch: zoom about the fingers' midpoint, follow its motion
      const [a, b] = [...t.pts.entries()].slice(0, 2).map(([id, p]) => (id === e.pointerId ? now : p));
      const [a0, b0] = [...t.pts.values()].slice(0, 2);
      const d0 = Math.hypot(a0[0] - b0[0], a0[1] - b0[1]), d1 = Math.hypot(a[0] - b[0], a[1] - b[1]);
      const mid: [number, number] = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
      const mid0: [number, number] = [(a0[0] + b0[0]) / 2, (a0[1] + b0[1]) / 2];
      if (d0 > 0) setView((cur) => panBy(fitted, zoomAt(fitted, cur, d1 / d0, mid[0], mid[1]), mid[0] - mid0[0], mid[1] - mid0[1]));
      t.moved = true;
    } else if (t.moved || Math.hypot(now[0] - t.start[0], now[1] - t.start[1]) > TAP_SLOP_PX) {
      setView((cur) => panBy(fitted, cur, now[0] - before[0], now[1] - before[1]));
      t.moved = true;
    }
    t.pts.set(e.pointerId, now);
  };
  const onUp = (e: PointerEvent<SVGSVGElement>) => {
    const t = touch.current;
    if (!zoomable || !t.pts.has(e.pointerId)) return;
    t.pts.delete(e.pointerId);
    if (t.pts.size === 0 && !t.moved) select(local(e));
  };
  const onWheel = (e: WheelEvent<SVGSVGElement>) => {
    if (!zoomable) return;
    const [px, py] = local(e);
    setView((cur) => zoomAt(fitted, cur, Math.pow(1.25, -e.deltaY / 100), px, py));
  };
  const step = (factor: number) => setView((cur) => zoomAt(fitted, cur, factor, width / 2, height / 2));
  const bar = scaleBar(l.scale, compact ? 50 : 80);
  // ids only where they do not run into the neighbouring circles
  const showIds = !compact && features.length <= 80 && minSpacingPx(features, l) >= 30;
  const [cornerX, cornerY] = fromPx(l, 0, 0);
  const svg = (
    <svg className="map" width={width} height={height} viewBox={`0 0 ${width} ${height}`} onPointerDown={onDown}
         onPointerMove={onMove} onPointerUp={onUp} onPointerCancel={onUp} onWheel={onWheel}
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
          <g className="map-axes" transform={`translate(44 ${height - 12})`}>
            <line x1={0} y1={0} x2={-28} y2={0} markerEnd="url(#arrow)" />
            <text x={-32} y={-6}>+X</text>
            <line x1={0} y1={-6} x2={0} y2={-34} markerEnd="url(#arrow)" />
            <text x={4} y={-38}>+Y</text>
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
  if (!zoomable || l.empty) return svg;
  return (
    <div className="map-wrap" style={{ width, height }}>
      {svg}
      <div className="map-zoom">
        <button className="secondary" aria-label="Zoom in" disabled={v.zoom >= MAX_ZOOM} onClick={() => step(2)}>+</button>
        <button className="secondary" aria-label="Zoom out" disabled={v.zoom <= 1} onClick={() => step(0.5)}>−</button>
        <button className="secondary map-fit" aria-label="Show the whole scan" disabled={v.zoom <= 1}
                onClick={() => setView(FIT_VIEW)}>Fit</button>
      </div>
    </div>
  );
}
