// Map contract (plan section 9), count circles (9.3) and display wording.
import { circleModel, circleSize, MAX_CIRCLES } from "../src/lib/circles";
import { calibrationOutcome, countText, measurementReason, measurementValue, mismatchText, rangeText, REASON_TEXT, STATUS } from "../src/lib/format";
import { clampView, FIT_VIEW, fromPx, keepInView, layoutMap, MAX_ZOOM, MIN_HIT_PX, MIN_SPAN_MM, panBy, pick, radiusPx, scaleBar, scanOrder,
  stepSelection, toPx, zoomAt, zoomed } from "../src/lib/map";
import { assert, type Test } from "./t";

const P = (x_mm: number, y_mm: number, diameter_mm: number | null = 8) => ({ x_mm, y_mm, diameter_mm });

export const tests: Test[] = [
  ["results stepper: walks the features in scan order and stops at the ends", () => {
    const fs = [{ x_mm: 0, y_mm: 40, result_seq: 7 }, { x_mm: 0, y_mm: 10, result_seq: 3 }, { x_mm: 2, y_mm: 25, result_seq: 5 }];
    assert.deepEqual(scanOrder(fs).map((f) => f.result_seq), [3, 5, 7]);
    assert.equal(stepSelection(fs, null, 1), 3, "from none, › selects the first along the scan");
    assert.equal(stepSelection(fs, null, -1), 7, "from none, ‹ selects the last");
    assert.equal(stepSelection(fs, 3, 1), 5);
    assert.equal(stepSelection(fs, 5, -1), 3);
    assert.equal(stepSelection(fs, 7, 1), 7, "stops at the last");
    assert.equal(stepSelection(fs, 3, -1), 3, "stops at the first");
    assert.equal(stepSelection([], null, 1), null);
  }],
  ["map: +x is drawn leftward and +y upward (the operator's view, the scan starts at the bottom)", () => {
    const l = layoutMap([P(0, 0), P(50, 0), P(0, 80)], 400, 300);
    const [x0, y0] = toPx(l, 0, 0), [x1] = toPx(l, 50, 0), [, y2] = toPx(l, 0, 80);
    assert.ok(x1 < x0, "larger x must be further left");
    assert.ok(y2 < y0, "larger y must be further up");
  }],
  ["map: one uniform scale on both axes (no distortion)", () => {
    const pts = [P(0, 0), P(100, 0), P(0, 20)];
    const l = layoutMap(pts, 470, 330);
    const [ax, ay] = toPx(l, 0, 0), [bx] = toPx(l, 100, 0), [, cy] = toPx(l, 0, 20);
    assert.near((ax - bx) / 100, (ay - cy) / 20, 1e-9);  // +x left, +y up: screen x and y fall as they grow
  }],
  ["map: signed y of a backwards scan is kept (negative y below the start)", () => {
    const l = layoutMap([P(0, -120), P(0, -20)], 300, 300);
    assert.ok(toPx(l, 0, -120)[1] > toPx(l, 0, -20)[1]);
    assert.ok(l.maxY < 0 && fromPx(l, 0, l.height)[1] < -120, "negative y stays negative");
  }],
  ["map zoom: scale grows uniformly; the point under the fingers stays put; zoom 1 cannot pan", () => {
    const l = layoutMap([P(0, 0), P(0, 540), P(-8, 270)], 470, 330, 26, 46);
    const v = zoomAt(l, FIT_VIEW, 4, 100, 200);
    const z = zoomed(l, v);
    assert.near(z.scale, 4 * l.scale, 1e-9);
    const [mx, my] = fromPx(l, 100, 200);
    const [px, py] = toPx(z, mx, my);
    assert.near(px, 100, 1e-6, "x under the pinch");
    assert.near(py, 200, 1e-6, "y under the pinch");
    assert.deepEqual(panBy(l, FIT_VIEW, 50, -30), FIT_VIEW);  // no panning at zoom 1
    assert.equal(zoomAt(l, FIT_VIEW, 0.5, 10, 10).zoom, 1, "never smaller than the whole scan");
    assert.equal(clampView(l, { zoom: 999, panX: 0, panY: 0 }).zoom, MAX_ZOOM);
    const far = clampView(l, { zoom: 2, panX: 1e6, panY: -1e6 });
    assert.ok(far.panX <= l.width / 2 && far.panY >= -l.height / 2, "cannot pan the scan out of sight");
  }],
  ["map zoom: close features become pickable apart; a stepped-to feature is panned into view", () => {
    const pts = [P(0, 0, 9), P(0, 21, 9)].concat(Array.from({ length: 19 }, (_, i) => P(-(i % 3), 42 + 21 * i, 9)));
    const l = layoutMap(pts, 470, 330, 26, 46);
    const [x0, y0] = toPx(l, 0, 0), [, y1] = toPx(l, 0, 21);
    assert.ok(Math.abs(y1 - y0) < MIN_HIT_PX, "fitted: 21 mm apart is under one tap radius");
    let v = zoomAt(l, FIT_VIEW, 8, x0, y0);
    const z = zoomed(l, v);
    const [zx, zy] = toPx(z, 0, 0), [, zy1] = toPx(z, 0, 21);
    assert.equal(pick(pts, z, zx, zy), 0);
    assert.equal(pick(pts, z, zx, zy1), 1, "zoomed in, the next hole is its own target");
    const last = pts[pts.length - 1];
    v = keepInView(l, v, last.x_mm, last.y_mm);
    const [lx, ly] = toPx(zoomed(l, v), last.x_mm, last.y_mm);
    assert.ok(lx >= 0 && lx <= l.width && ly >= 0 && ly <= l.height, `last feature in view: ${lx}, ${ly}`);
    assert.deepEqual(keepInView(l, FIT_VIEW, last.x_mm, last.y_mm), FIT_VIEW);  // the fitted view never moves
  }],
  ["map: every feature, including its circle, fits inside the padded view", () => {
    const pts = [P(-15, 15, 9.5), P(15, 265, 9.5), P(0, 140, 4.8)];
    const W = 470, H = 330, pad = 26, bottom = 46;
    const l = layoutMap(pts, W, H, pad, bottom);
    for (const p of pts) {
      const [x, y] = toPx(l, p.x_mm, p.y_mm), r = radiusPx(l, p, 0);
      assert.ok(x - r >= pad - 1e-6 && x + r <= W - pad + 1e-6, `x out of view: ${x}`);
      assert.ok(y - r >= pad - 1e-6 && y + r <= H - bottom + 1e-6, `y out of view: ${y}`);
    }
  }],
  ["map: a single feature does not zoom to infinity", () => {
    const l = layoutMap([P(3, 4, 6)], 300, 300, 20);
    assert.ok(l.scale <= (300 - 40) / MIN_SPAN_MM + 1e-9, `scale ${l.scale}`);
  }],
  ["map: the reported top-left is the real view corner in scan mm", () => {
    const l = layoutMap([P(0, 0), P(10, 200)], 400, 300);
    const [cx, cy] = fromPx(l, 0, 0);
    const [px, py] = toPx(l, cx, cy);
    assert.near(px, 0, 1e-9);
    assert.near(py, 0, 1e-9);
  }],
  ["map: tap targets are larger than drawn circles; the nearest feature wins", () => {
    const pts = [P(0, 0, 2), P(30, 0, 2)];
    const l = layoutMap(pts, 400, 200);
    const [x0, y0] = toPx(l, 0, 0);
    assert.ok(radiusPx(l, pts[0]) < MIN_HIT_PX, "the drawn circle is small");
    assert.equal(pick(pts, l, x0 + MIN_HIT_PX - 1, y0), 0, "a tap just inside the 24 px radius selects it");
    assert.equal(pick(pts, l, x0, y0 + MIN_HIT_PX + 2), null, "a tap outside selects nothing");
    const [x1] = toPx(l, 30, 0);
    assert.equal(pick(pts, l, x1 - 1, y0), 1, "nearest centre wins");
    assert.equal(pick([], layoutMap([], 100, 100), 50, 50), null);
  }],
  ["map: scale bar is a round number of mm", () => {
    for (const s of [0.37, 1.38, 4.2, 12]) {
      const b = scaleBar(s);
      const m = b.mm / Math.pow(10, Math.floor(Math.log10(b.mm)));
      assert.ok([1, 2, 5].some((k) => Math.abs(k - m) < 1e-9), `${b.mm}`);
      assert.near(b.px, b.mm * s, 1e-9);
    }
  }],
  ["circles: one per expected feature, filled in detection order", () => {
    const m = circleModel(20, 5, [{ partial: false }, { partial: true }, { partial: false }, { partial: false }, { partial: false }]);
    assert.equal(m.circles.length, 20);
    assert.deepEqual(m.circles.slice(0, 6), ["found", "partial", "found", "found", "found", "empty"]);
    assert.equal(m.overflow, 0);
  }],
  ["circles: more than 40 -> 40 circles plus the remaining number", () => {
    const m = circleModel(50, 12, []);
    assert.equal(m.circles.length, MAX_CIRCLES);
    assert.equal(m.overflow, 10);
    assert.equal(m.circles.filter((c) => c !== "empty").length, 12);
  }],
  ["circles: targets beyond the expected count are extra, never a stop", () => {
    const m = circleModel(3, 5, [{ partial: false }, { partial: false }, { partial: false }, { partial: true }, { partial: false }]);
    assert.deepEqual(m.circles, ["found", "found", "found", "extra_partial", "extra"]);
  }],
  ["circles: zero counts and a backend count ahead of the feature list", () => {
    assert.deepEqual(circleModel(null, 0, []), { circles: [], overflow: 0, total: 0 });
    assert.deepEqual(circleModel(4, 2, []).circles, ["found", "found", "empty", "empty"]);
  }],
  ["circles: the size shrinks so every circle fits the box", () => {
    for (const n of [1, 10, 20, 40, 42]) {
      const size = circleSize(n, 450, 230);
      const perRow = Math.floor((450 + 8) / (size + 8));
      assert.ok(Math.ceil(n / perRow) * (size + 8) - 8 <= 230 || size === 22, `n=${n} size=${size}`);
    }
  }],
  ["format: mismatch wording; statuses include UNDECIDED and failed / incomplete", () => {
    assert.equal(mismatchText(20, 18), "2 fewer than expected");
    assert.equal(mismatchText(20, 21), "1 more than expected");
    assert.equal(mismatchText(20, 20), null);
    assert.equal(mismatchText(null, 3), null);
    assert.equal(countText(20, 18), "18 / 20");
    for (const k of ["undecided", "failed", "incomplete_retained", "accepted_with_warning", "rescanned"] as const)
      assert.ok(STATUS[k].label.length > 0);
    assert.equal(STATUS.undecided.label, "Undecided");
  }],
  ["measurement wording: only a measured value is shown; every reason code has operator words", () => {
    const base = { name: "csk_depth", unit: "mm", reason: null, verdict: null, lower_mm: 0.947, upper_mm: 1.147 };
    assert.equal(measurementValue({ ...base, status: "measured", value_mm: 1.0024 }), "1.002 mm");
    // a number without a measurement (e.g. a detector value) is never shown
    assert.equal(measurementValue({ ...base, status: "pending", value_mm: 0.5 }), "Not measured");
    assert.equal(measurementValue({ ...base, status: "not_measured", value_mm: 0.5, reason: "failed" }), "Not measured");
    assert.equal(measurementValue({ ...base, name: "flushness", status: "measured", value_mm: 0.05 }), "+0.050 mm");
    assert.equal(measurementValue({ ...base, name: "flushness", status: "measured", value_mm: 0 }), "0.000 mm");
    assert.equal(rangeText({ ...base, status: "measured", value_mm: 1 }), "0.947 – 1.147 mm");
    assert.equal(rangeText({ ...base, status: "measured", value_mm: 1, lower_mm: null }), null);
    for (const code of ["too_few_points", "surface_not_flat", "unexpected_shape", "scan_distorted", "partial", "failed"])
      assert.ok(REASON_TEXT[code] && !REASON_TEXT[code].includes("_"), code);
    assert.equal(measurementReason({ ...base, status: "not_measured", value_mm: null, reason: "brand_new_code" }),
                 REASON_TEXT.failed);
    assert.equal(measurementReason({ ...base, status: "measured", value_mm: 1 }), null);
  }],
  ["calibration outcome: a failure is told even though the old calibration is kept", () => {
    const failed = calibrationOutcome("calibration failed: device never still long enough", true);
    assert.equal(failed.tone, "bad");
    assert.ok(failed.text.startsWith("Calibration failed"), failed.text);
    assert.equal(failed.detail, "device never still long enough");
    assert.equal(calibrationOutcome("gyro calibrated", true).tone, "ok");
    assert.equal(calibrationOutcome("gyro calibrated (could not write /x.yaml)", true).tone, "warn");
    assert.equal(calibrationOutcome("", false).tone, "bad");                // never reported done
    assert.equal(calibrationOutcome("FATAL laser: gone", true).tone, "ok");  // result message expired, calibrated
  }],
];
