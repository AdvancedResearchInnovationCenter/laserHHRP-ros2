// Count circles (plan section 9.3). The detected count comes from the backend; this only lays it out.
//   - one circle per expected feature, filled in detection order as targets arrive (also after release);
//   - targets beyond the expected count are drawn as "extra" (count mismatch, never a stop);
//   - more than `max` circles: `max` circles plus the remaining number.

export type CircleState = "empty" | "found" | "partial" | "extra" | "extra_partial";

export interface CircleModel { circles: CircleState[]; overflow: number; total: number }

export const MAX_CIRCLES = 40;

/** `targets`: the scan's target features in detection order (partial flag from the detector);
 *  `detected`: the backend's detected target count (authoritative; may lead the feature list). */
export function circleModel(expected: number | null, detected: number, targets: { partial: boolean }[],
                            max = MAX_CIRCLES): CircleModel {
  const total = Math.max(expected ?? 0, detected);
  const shown = Math.min(total, max);
  const circles: CircleState[] = [];
  for (let i = 0; i < shown; i++) {
    if (i >= detected) { circles.push("empty"); continue; }
    const partial = targets[i]?.partial === true;
    const extra = expected !== null && i >= expected;
    circles.push(extra ? (partial ? "extra_partial" : "extra") : partial ? "partial" : "found");
  }
  return { circles, overflow: total - shown, total };
}

/** Circle diameter in px that fits `n` circles into a w x h box. */
export function circleSize(n: number, w: number, h: number, gap = 8, maxSize = 56, minSize = 22): number {
  for (let size = maxSize; size > minSize; size -= 2) {
    const perRow = Math.max(1, Math.floor((w + gap) / (size + gap)));
    const rows = Math.ceil(n / perRow);
    if (rows * (size + gap) - gap <= h) return size;
  }
  return minSize;
}
