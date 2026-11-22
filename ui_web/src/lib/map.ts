// Spatial results map (plan section 9, map contract). Pure geometry, unit-tested.
//   screen x = feature x_mm (position_m[0] * 1000) leftward, across the laser line: laser +x is the operator's
//   left (checked 2026-11-22: holes1's row left the line on the -x side, the operator's right)
//   screen y = feature y_mm (position_m[1] * 1000) upward, along the roll / scan axis: the scan's start at the
//   bottom, the way the operator rolls away from themselves (signed: a backwards scan runs downward)
// Together: the operator's own view of the part, from behind the device (a rotation of x / y, not a mirror).
// Uniform scale (no distortion). The view's corner is set by this scan's features, not a workpiece origin; there
// is no registration between scans. Results can zoom and pan (MapView): zoomed() gives the layout as drawn.

export interface MapPoint { x_mm: number; y_mm: number; diameter_mm: number | null }

export interface MapLayout {
  width: number; height: number;
  scale: number;                       // px per mm, the same on both axes
  maxX: number; maxY: number;          // mm at the content's top-left (+x is left, +y is up)
  ox: number; oy: number;              // px offset of (maxX, maxY)
  empty: boolean;
}

export const DEFAULT_DIAMETER_MM = 6;  // drawing size when the detector gave no diameter
export const MIN_SPAN_MM = 40;         // one feature (or a single row) does not zoom to infinity
export const MIN_HIT_PX = 24;          // tap radius: larger than the drawn circles (48 px target)

const radiusMm = (p: MapPoint) => (p.diameter_mm && p.diameter_mm > 0 ? p.diameter_mm : DEFAULT_DIAMETER_MM) / 2;

/** `pad` all round; `padBottom` keeps the axis marker and scale bar clear of features. */
export function layoutMap(points: MapPoint[], width: number, height: number, pad = 28, padBottom = pad): MapLayout {
  if (points.length === 0) return { width, height, scale: 1, maxX: 0, maxY: 0, ox: pad, oy: pad, empty: true };
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const p of points) {
    const r = radiusMm(p);
    minX = Math.min(minX, p.x_mm - r); maxX = Math.max(maxX, p.x_mm + r);
    minY = Math.min(minY, p.y_mm - r); maxY = Math.max(maxY, p.y_mm + r);
  }
  // widen a degenerate span around its centre
  const grow = (lo: number, hi: number): [number, number] => {
    const span = hi - lo;
    if (span >= MIN_SPAN_MM) return [lo, hi];
    const c = (lo + hi) / 2;
    return [c - MIN_SPAN_MM / 2, c + MIN_SPAN_MM / 2];
  };
  [minX, maxX] = grow(minX, maxX);
  [minY, maxY] = grow(minY, maxY);
  const innerH = Math.max(1, height - pad - padBottom);
  const scale = Math.min((width - 2 * pad) / (maxX - minX), innerH / (maxY - minY));
  // centre the content on the axis with slack
  const ox = (width - (maxX - minX) * scale) / 2;
  const oy = pad + (innerH - (maxY - minY) * scale) / 2;
  return { width, height, scale, maxX, maxY, ox, oy, empty: false };
}

export function toPx(l: MapLayout, x_mm: number, y_mm: number): [number, number] {
  return [l.ox + (l.maxX - x_mm) * l.scale, l.oy + (l.maxY - y_mm) * l.scale];
}

export function radiusPx(l: MapLayout, p: MapPoint, minPx = 5): number {
  return Math.max(minPx, radiusMm(p) * l.scale);
}

/** Index of the feature a tap at (px, py) selects: the nearest centre within its tap radius. */
export function pick(points: MapPoint[], l: MapLayout, px: number, py: number, minHit = MIN_HIT_PX): number | null {
  let best: number | null = null;
  let bestD = Infinity;
  points.forEach((p, i) => {
    const [cx, cy] = toPx(l, p.x_mm, p.y_mm);
    const d = Math.hypot(px - cx, py - cy);
    if (d <= Math.max(minHit, radiusPx(l, p)) && d < bestD) {
      best = i;
      bestD = d;
    }
  });
  return best;
}

/** Scan-local mm at a screen point (the map's real top-left is fromPx(l, 0, 0)). */
export function fromPx(l: MapLayout, px: number, py: number): [number, number] {
  return [l.maxX - (px - l.ox) / l.scale, l.maxY - (py - l.oy) / l.scale];
}

/** Zoom and pan on top of the fitted layout: drawn = centre + (fitted - centre) * zoom + pan (px). */
export interface MapView { zoom: number; panX: number; panY: number }
export const FIT_VIEW: MapView = { zoom: 1, panX: 0, panY: 0 };
export const MAX_ZOOM = 16;

/** The layout as drawn in `v`: every function above (toPx, pick, radiusPx, scaleBar, fromPx) works on it. */
export function zoomed(l: MapLayout, v: MapView): MapLayout {
  if (v.zoom === 1 && v.panX === 0 && v.panY === 0) return l;
  const cx = l.width / 2, cy = l.height / 2;
  return { ...l, scale: l.scale * v.zoom, ox: cx + (l.ox - cx) * v.zoom + v.panX, oy: cy + (l.oy - cy) * v.zoom + v.panY };
}

/** Keeps the fitted view's area covering the map: no panning the scan out of sight, none at zoom 1. */
export function clampView(l: MapLayout, v: MapView): MapView {
  const zoom = Math.min(MAX_ZOOM, Math.max(1, v.zoom));
  const mx = (zoom - 1) * l.width / 2, my = (zoom - 1) * l.height / 2;
  return { zoom, panX: Math.min(mx, Math.max(-mx, v.panX)), panY: Math.min(my, Math.max(-my, v.panY)) };
}

/** Zoom by `factor` about the screen point (px, py): the scan point under it stays under it (then clamped). */
export function zoomAt(l: MapLayout, v: MapView, factor: number, px: number, py: number): MapView {
  const zoom = Math.min(MAX_ZOOM, Math.max(1, v.zoom * factor));
  const k = zoom / v.zoom, cx = l.width / 2, cy = l.height / 2;
  // drawn = c + (fitted - c) * zoom + pan: keep drawn(p) fixed for the point now at (px, py)
  return clampView(l, { zoom, panX: px - cx - (px - cx - v.panX) * k, panY: py - cy - (py - cy - v.panY) * k });
}

export function panBy(l: MapLayout, v: MapView, dx: number, dy: number): MapView {
  return clampView(l, { ...v, panX: v.panX + dx, panY: v.panY + dy });
}

/** When zoomed in, pan so the feature at (x_mm, y_mm) is in view (`margin` px inside the edge), else unchanged. */
export function keepInView(l: MapLayout, v: MapView, x_mm: number, y_mm: number, margin = 40): MapView {
  if (v.zoom === 1) return v;
  const [px, py] = toPx(zoomed(l, v), x_mm, y_mm);
  const inside = px >= margin && px <= l.width - margin && py >= margin && py <= l.height - margin;
  return inside ? v : clampView(l, { ...v, panX: v.panX + l.width / 2 - px, panY: v.panY + l.height / 2 - py });
}

/** Smallest distance in px between two feature centres (Infinity for < 2 features). */
export function minSpacingPx(points: MapPoint[], l: MapLayout): number {
  let best = Infinity;
  for (let i = 0; i < points.length; i++)
    for (let j = i + 1; j < points.length; j++)
      best = Math.min(best, Math.hypot(points[i].x_mm - points[j].x_mm, points[i].y_mm - points[j].y_mm) * l.scale);
  return best;
}

/** A round scale-bar length (1, 2, 5 x 10^n mm) about `targetPx` long. */
export function scaleBar(scale: number, targetPx = 80): { mm: number; px: number } {
  const raw = targetPx / scale;
  const pow = Math.pow(10, Math.floor(Math.log10(raw)));
  const mm = [1, 2, 5, 10].map((m) => m * pow).reduce((a, b) => (Math.abs(b - raw) < Math.abs(a - raw) ? b : a));
  return { mm, px: mm * scale };
}

/** Features in scan order (along y, then x): the order the ‹ › stepper walks on Results. */
export function scanOrder<T extends { x_mm: number; y_mm: number }>(features: readonly T[]): T[] {
  return [...features].sort((a, b) => a.y_mm - b.y_mm || a.x_mm - b.x_mm);
}

/** The feature after (dir 1) or before (dir -1) the selected one in scan order; from none: the first or the last.
 *  Stops at the ends. Returns its result_seq, or null when there are no features. On a long scan the map's
 *  circles sit too close to pick one by finger: the stepper reaches each one. */
export function stepSelection(features: readonly { x_mm: number; y_mm: number; result_seq: number }[],
                              selected: number | null, dir: 1 | -1): number | null {
  const order = scanOrder(features);
  if (order.length === 0) return null;
  const i = order.findIndex((f) => f.result_seq === selected);
  if (i < 0) return (dir > 0 ? order[0] : order[order.length - 1]).result_seq;
  return order[Math.min(order.length - 1, Math.max(0, i + dir))].result_seq;
}
