// Screens rendered from backend snapshots (react-dom/server: no browser, no extra dependencies).
// Behaviour in a real browser is covered by tools/ui_verify/phase4_screens.py.
import { renderToStaticMarkup } from "react-dom/server";
import type { Result } from "../src/api/client";
import type { Snapshot } from "../src/api/types";
import { Shell } from "../src/App";
import { Identify } from "../src/screens/Identify";
import { FeatureDetail } from "../src/ui/FeatureDetail";
import { FeatureMap } from "../src/ui/FeatureMap";
import type { Run } from "../src/screens/common";
import { feature, scan, snapshot } from "./fixtures";
import { assert, type Test } from "./t";

const run: Run = async <T,>(p: Promise<Result<T>>) => p;
const html = (s: Snapshot, connected = true) => renderToStaticMarkup(<Shell snap={s} connected={connected} run={run} />);
const text = (markup: string) => markup.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ");
const buttons = (markup: string) =>
  [...markup.matchAll(/<button[^>]*>(.*?)<\/button>/g)].map((m) => text(m[1]).trim());
const NO_PASS = /\bPASS(ED)?\b|\bPass(ed)?\b|In tolerance|Within tolerance/;

export const tests: Test[] = [
  ["identify: on-screen keyboard; Continue disabled until an ID is typed", () => {
    const m = html(snapshot({ screen: "identify", operator: null, inspection: null }));
    assert.includes(m, "Operator identification");
    assert.ok(/<button[^>]*disabled[^>]*>Continue<\/button>/.test(m), "Continue must start disabled");
    assert.ok(buttons(m).includes("Q") && buttons(m).includes("0") && buttons(m).includes("-"));
  }],
  ["identify: no way back without a previous operator; Back to <id> after Change operator", () => {
    const plain = renderToStaticMarkup(<Identify run={run} />);
    assert.ok(!buttons(plain).some((b) => b.startsWith("‹ Back")), "no Back button on a fresh identification");
    const back = renderToStaticMarkup(<Identify run={run} previous="ENG-1042" />);
    assert.ok(buttons(back).includes("‹ Back to ENG-1042"), "Back to the previous operator offered");
  }],
  ["setup: each mode card is a figure with a short label, no detail text", () => {
    const m = html(snapshot({ screen: "setup", inspection: null }));
    const cards = [...m.matchAll(/<button[^>]*class="mode-card[^"]*"[^>]*>(.*?)<\/button>/g)].map((c) => c[1]);
    assert.equal(cards.length, 2);
    assert.ok(cards[0].includes('data-testid="figure-csk_depth"') && cards[1].includes('data-testid="figure-rivet_flushness"'),
              "a figure in each card");
    assert.equal(text(cards[0]).trim(), "Countersink Depth");
    assert.equal(text(cards[1]).trim(), "Rivet Flushness");
  }],
  ["ready: hold-the-switch prompt; no on-screen start / stop", () => {
    const m = html(snapshot());
    assert.includes(text(m), "Hold the switch to scan");
    for (const b of buttons(m)) assert.ok(!/^(Start|Stop)( scan)?$/i.test(b), `scan control button ${b}`);
  }],
  ["not ready: every backend reason listed; Restart detector only on a detector error", () => {
    const reasons = ["Calibration required", "Encoder error"];
    let m = html(snapshot({ screen: "not_ready", ready: false, not_ready_reasons: reasons }));
    for (const r of reasons) assert.includes(m, r);
    assert.ok(!buttons(m).includes("Restart detector"));
    m = html(snapshot({ screen: "not_ready", ready: false, not_ready_reasons: ["Detector not ready: exited"],
                        detector: { mode: "csk_depth", alive: false, ready: false, error: "exited" } }));
    assert.ok(buttons(m).includes("Restart detector"));
  }],
  ["not ready: a missing expected count is asked for", () => {
    const s = snapshot({ screen: "not_ready", ready: false, not_ready_reasons: ["Enter the expected feature count"] });
    s.inspection!.next_expected_count = null;
    const m = html(s);
    assert.includes(m, 'class="count-panel needs"');
    assert.ok(buttons(m).includes("Set"));
  }],
  ["scanning: 50 expected -> 40 circles and +10", () => {
    const sc = scan({ acquisition: "scanning", analysis: "pending", status: "scanning", expected_count: 50,
                      features: Array.from({ length: 12 }, (_, i) => feature(i)) });
    const m = html(snapshot({ screen: "scanning", current_scan: sc }));
    assert.equal((m.match(/class="circle /g) ?? []).length, 40);
    assert.includes(m, "+10");
    assert.includes(text(m), "12 / 50");
  }],
  ["finishing: counts keep filling after release; partial and other features shown", () => {
    // a plain hole (not a target in CSK mode) arrives first: it must not take a count circle
    const feats = [feature(0, { kind: "hole", is_target: false }),
      ...Array.from({ length: 6 }, (_, i) => feature(i + 1)), feature(7, { partial: true })];
    const sc = scan({ acquisition: "ended", analysis: "pending", status: "analyzing", features: feats });
    const m = html(snapshot({ screen: "finishing_analysis", current_scan: sc }));
    assert.includes(text(m), "Finishing analysis");
    assert.includes(text(m), "7 / 20");
    assert.equal((m.match(/data-state="partial"/g) ?? []).length, 1);
    assert.includes(text(m), "1 partial");
    assert.includes(text(m), "1 other feature not counted");
  }],
  ["results: map draws every feature; Accept / Rescan; no pass wording", () => {
    const m = html(snapshot({ screen: "results", current_scan: scan() }));
    assert.equal((m.match(/data-feature=/g) ?? []).length, 20);
    assert.ok(buttons(m).includes("Accept") && buttons(m).includes("Rescan"));
    assert.excludes(text(m), NO_PASS);
  }],
  ["results: count mismatch warns but still allows Accept", () => {
    const sc = scan({ features: Array.from({ length: 18 }, (_, i) => feature(i)), count_mismatch: true });
    const m = html(snapshot({ screen: "results", current_scan: sc }));
    assert.includes(text(m), "2 fewer than expected");
    assert.ok(buttons(m).includes("Accept with warning"));
  }],
  ["results: a completed zero-feature scan is honest, not a failure", () => {
    const sc = scan({ features: [], count_mismatch: true });
    const m = text(html(snapshot({ screen: "results", current_scan: sc })));
    assert.includes(m, "No features detected");
    assert.includes(m, "Analysis complete: nothing found");
    assert.excludes(m, "Analysis failed");
  }],
  ["analysis failed: never Accept; Retry only when it can run, otherwise the reason", () => {
    const failed = { analysis: "failed" as const, status: "failed" as const, analysis_error: "missing frames: 3 of 900" };
    let m = html(snapshot({ screen: "analysis_failed", current_scan: scan({ ...failed, raw: {
      session_id: null, dir: null, state: null, frame_count: null, note: "no recording linked" } }) }));
    assert.ok(!buttons(m).some((b) => b.startsWith("Accept")), "no Accept on a failed analysis");
    assert.ok(!buttons(m).includes("Retry analysis"));
    assert.includes(text(m), "Retry unavailable: no recording linked");
    assert.includes(text(m), "missing frames: 3 of 900");
    assert.ok(buttons(m).includes("Rescan") && buttons(m).includes("Keep incomplete"));
    m = html(snapshot({ screen: "analysis_failed",
                        current_scan: scan({ ...failed, replay_eligible: true, retry_available: false }) }));
    assert.includes(text(m), "replay into the detector is not implemented yet");
    m = html(snapshot({ screen: "analysis_failed",
                        current_scan: scan({ ...failed, replay_eligible: true, retry_available: true }) }));
    assert.ok(buttons(m).includes("Retry analysis"));
  }],
  ["feature detail: id, position, the measured value with its GO verdict and range; no detector figures", () => {
    const m = text(renderToStaticMarkup(<FeatureDetail f={feature(0)} />));
    assert.includes(m, "#1 Countersink GO");
    assert.includes(m, "CSK depth 1.002 mm");
    assert.includes(m, "GO range 0.947 – 1.147 mm");
    assert.excludes(m, /Diameter|coverage|Scanned share|RMS/);
    assert.excludes(m, NO_PASS);
  }],
  ["feature detail: NO-GO, signed flushness, operator wording for not measured, nothing on non-targets", () => {
    const flush = (over: object) => ({ name: "flushness", unit: "mm", reason: null, lower_mm: -0.15, upper_mm: 0.05,
                                       value_mm: null, verdict: null, status: "measured" as const, ...over });
    let r = text(renderToStaticMarkup(<FeatureDetail f={feature(0, { kind: "rivet",
      measurement: flush({ value_mm: 0.067, verdict: "out_of_limits" }) })} />));
    assert.includes(r, "#1 Rivet head NO-GO");
    assert.includes(r, "Flushness +0.067 mm");
    assert.includes(r, "GO range −0.150 – +0.050 mm");
    r = text(renderToStaticMarkup(<FeatureDetail f={feature(0, { kind: "rivet",
      measurement: flush({ value_mm: -0.012, verdict: "in_limits" }) })} />));
    assert.includes(r, "Flushness −0.012 mm");
    r = text(renderToStaticMarkup(<FeatureDetail f={feature(0, { kind: "rivet",
      measurement: flush({ status: "not_measured", reason: "scan_distorted" }) })} />));
    assert.includes(r, "Flushness Not measured Why Scan distorted: rescan");
    assert.excludes(r, /\bGO\b(?! range)|NO-GO/);
    r = text(renderToStaticMarkup(<FeatureDetail f={feature(0, { partial: true,
      measurement: { ...feature(0).measurement, status: "not_measured", value_mm: null, verdict: null, reason: "partial" } })} />));
    assert.includes(r, "Partial");
    assert.includes(r, "Why Only partly scanned");
    r = text(renderToStaticMarkup(<FeatureDetail f={feature(0, { kind: "hole", is_target: false,
      measurement: { ...feature(0).measurement, status: "pending", value_mm: null, verdict: null } })} />));
    assert.includes(r, "Not counted");
    assert.excludes(r, /CSK depth|GO/);
  }],
  ["results: GO / NO-GO / not measured counts from the backend", () => {
    const fs = [feature(0), feature(1, { measurement: { ...feature(1).measurement, value_mm: 1.2, verdict: "out_of_limits" } }),
                feature(2, { measurement: { ...feature(2).measurement, status: "not_measured", value_mm: null, verdict: null,
                                            reason: "too_few_points" } })];
    const m = html(snapshot({ screen: "results", current_scan: scan({ features: fs, expected_count: 3 }) }));
    assert.includes(text(m), "1 GO");
    assert.includes(text(m), "1 NO-GO");
    assert.includes(text(m), "1 not measured");
    const failed = html(snapshot({ screen: "analysis_failed", current_scan: scan({ features: fs, analysis: "failed",
                                                                                  status: "failed", analysis_error: "x" }) }));
    assert.excludes(text(failed), /\d GO|NO-GO/);   // a failed scan has no verdicts to count
  }],
  ["map: empty scan says so; non-targets drawn but distinct", () => {
    let m = renderToStaticMarkup(<FeatureMap features={[]} width={300} height={200} />);
    assert.includes(m, "No features detected");
    m = renderToStaticMarkup(<FeatureMap features={[feature(0), feature(1, { kind: "hole", is_target: false })]}
                                         width={300} height={200} />);
    assert.includes(m, 'class="feat target');
    assert.includes(m, 'class="feat other');
    const nogo = { ...feature(1).measurement, value_mm: 1.3, verdict: "out_of_limits" as const };
    const none = { ...feature(2).measurement, status: "not_measured" as const, value_mm: null, verdict: null, reason: "failed" };
    m = renderToStaticMarkup(<FeatureMap features={[feature(0), feature(1, { measurement: nogo }),
                                                    feature(2, { measurement: none })]} width={300} height={200} />);
    assert.ok(/class="feat target\s+go/.test(m) && /class="feat target\s+nogo/.test(m)
              && /class="feat target\s+unmeasured/.test(m), "targets coloured by measurement outcome");
    assert.includes(m, "+Y");
    assert.includes(m, "+X");
  }],
  ["header: a light / dark switch on every screen", () => {
    for (const screen of ["identify", "setup", "ready", "scanning", "results"] as const) {
      const m = html(snapshot({ screen }));
      assert.ok(/<button[^>]*aria-label="Light mode"[^>]*data-testid="theme-toggle"/.test(m), `theme switch on ${screen}`);
    }
  }],
  ["header: clock not verified and offline are visible", () => {
    let m = text(html(snapshot({ clock_quality: "unverified" })));
    assert.includes(m, "Time not verified");
    m = text(html(snapshot(), false));
    assert.includes(m, "OFFLINE");
    assert.includes(m, "Connection to the backend lost");
  }],
  ["alerts: sticky backend alerts and warnings are shown", () => {
    const m = text(html(snapshot({ alerts: [{ code: "unassigned_scan", message: "Scan started but not recorded",
      scan_id: "x", at: "" }], warnings: ["Analysis is taking longer than expected"] })));
    assert.includes(m, "Scan started but not recorded");
    assert.includes(m, "+1 more");
  }],
  ["developer bar appears only with the mock source", () => {
    assert.excludes(html(snapshot({ source: "ros" })), "DEV");
    assert.includes(html(snapshot({ source: "mock" })), "DEV");
  }],
];
