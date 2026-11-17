// Spatial results map (plan section 9, map contract). Pure geometry, unit-tested.
//   screen x = feature x_mm (position_m[0] * 1000) rightward, across the laser line
//   screen y = feature y_mm (position_m[1] * 1000) downward, along the roll / scan axis
// Uniform scale (no distortion). The top-left is the bounding minimum of this scan's features, not a
// workpiece origin; there is no registration between scans.

export interface MapPoint { x_mm: number; y_mm: number; diameter_mm: number | null }

export interface MapLayout {
  width: number; height: number;
  scale: number;                       // px per mm, the same on both axes
  minX: number; minY: number;          // mm at the content's top-left
  ox: number; oy: number;              // px offset of (minX, minY)
  empty: boolean;
}

export const DEFAULT_DIAMETER_MM = 6;  // drawing size when the detector gave no diameter
export const MIN_SPAN_MM = 40;         // one feature (or a single row) does not zoom to infinity
export const MIN_HIT_PX = 24;          // tap radius: larger than the drawn circles (48 px target)

const radiusMm = (p: MapPoint) => (p.diameter_mm && p.diameter_mm > 0 ? p.diameter_mm : DEFAULT_DIAMETER_MM) / 2;

/** `pad` all round; `padBottom` keeps the axis marker and scale bar clear of features. */
export function layoutMap(points: MapPoint[], width: number, height: number, pad = 28, padBottom = pad): MapLayout {
  if (points.length === 0) return { width, height, scale: 1, minX: 0, minY: 0, ox: pad, oy: pad, empty: true };
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
  return { width, height, scale, minX, minY, ox, oy, empty: false };
}

export function toPx(l: MapLayout, x_mm: number, y_mm: number): [number, number] {
  return [l.ox + (x_mm - l.minX) * l.scale, l.oy + (y_mm - l.minY) * l.scale];
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
  return [l.minX + (px - l.ox) / l.scale, l.minY + (py - l.oy) / l.scale];
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
