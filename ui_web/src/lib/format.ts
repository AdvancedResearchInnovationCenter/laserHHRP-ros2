// Display wording only. Status, counts and target filtering come from the backend.
import type { Measurement, Mode, Scan, ScanStatus } from "../api/types";

export const MODE_LABEL: Record<Mode, string> = { csk_depth: "CSK Depth", rivet_flushness: "Rivet Flushness" };
export const TARGET_NOUN: Record<Mode, [string, string]> = {
  csk_depth: ["countersink", "countersinks"],
  rivet_flushness: ["rivet", "rivets"],
};
export const MEASUREMENT_LABEL: Record<string, string> = { csk_depth: "CSK depth", flushness: "Flushness" };
export const KIND_LABEL: Record<string, string> = { countersink: "Countersink", hole: "Plain hole", rivet: "Rivet head" };

export type Tone = "ok" | "warn" | "bad" | "info" | "neutral";

// The measurement algorithms' reason codes, worded for the operator (MEASUREMENT_INTEGRATION.md).
export const REASON_TEXT: Record<string, string> = {
  too_few_points: "Not enough scan data",
  surface_not_flat: "Surface around it is not flat",
  unexpected_shape: "Shape not recognised",
  scan_distorted: "Scan distorted: rescan",
  partial: "Only partly scanned",
  failed: "Could not be measured",
};

export const VERDICT: Record<"in_limits" | "out_of_limits", { label: string; tone: Tone }> = {
  in_limits: { label: "GO", tone: "ok" },
  out_of_limits: { label: "NO-GO", tone: "bad" },
};

/** "1.012 mm" (depth), "+0.067 mm" / "−0.012 mm" (flushness: the sign is the point). */
export function measurementValue(m: Measurement): string {
  if (m.status !== "measured" || m.value_mm === null) return "Not measured";
  const v = m.value_mm;
  if (m.name !== "flushness") return `${v.toFixed(3)} mm`;
  return `${v > 0 ? "+" : v < 0 ? "\u2212" : ""}${Math.abs(v).toFixed(3)} mm`;
}

/** Why a target was not measured, in operator words; null when measured. */
export function measurementReason(m: Measurement): string | null {
  if (m.status === "measured") return null;
  return m.status === "not_measured" ? REASON_TEXT[m.reason ?? "failed"] ?? REASON_TEXT.failed : null;
}

/** "0.947 – 1.147 mm" for depth, "−0.150 – +0.050 mm" for flushness; null without limits. */
export function rangeText(m: Measurement): string | null {
  if (m.lower_mm === null || m.lower_mm === undefined || m.upper_mm === null || m.upper_mm === undefined) return null;
  const f = (v: number) => measurementValue({ ...m, status: "measured", value_mm: v }).replace(" mm", "");
  return `${f(m.lower_mm)} \u2013 ${f(m.upper_mm)} mm`;
}

export const STATUS: Record<ScanStatus, { label: string; tone: Tone }> = {
  accepted: { label: "Accepted", tone: "ok" },
  accepted_with_warning: { label: "Accepted · count warning", tone: "warn" },
  rescanned: { label: "Rescanned", tone: "neutral" },
  undecided: { label: "Undecided", tone: "info" },
  failed: { label: "Analysis failed", tone: "bad" },
  incomplete_retained: { label: "Incomplete (kept)", tone: "bad" },
  analyzing: { label: "Analyzing", tone: "warn" },
  scanning: { label: "Scanning", tone: "info" },
  unassigned: { label: "Unassigned", tone: "bad" },
};

export const statusOf = (s: Scan) => STATUS[s.status] ?? { label: s.status, tone: "neutral" as Tone };

export const plural = (n: number, [one, many]: [string, string]) => `${n} ${n === 1 ? one : many}`;

export function mm(v: number | null | undefined, digits = 1): string {
  return v === null || v === undefined ? "—" : `${v.toFixed(digits)} mm`;
}

export function pct(v: number | null | undefined): string {
  return v === null || v === undefined ? "—" : `${Math.round(v * 100)} %`;
}

/** Local wall time; marked when the clock was not verified when it was recorded. */
export function clockTime(iso: string | null | undefined, quality?: string): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  let t = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  if (d.toDateString() !== new Date().toDateString())
    t = `${d.toLocaleDateString([], { day: "numeric", month: "short" })} ${t}`;
  return quality === "unverified" ? `${t}?` : t;
}

/** "2 fewer than expected" / "3 more than expected"; null when equal or no expectation. */
export function mismatchText(expected: number | null, detected: number): string | null {
  if (expected === null || expected === detected) return null;
  const d = detected - expected;
  return d < 0 ? `${-d} fewer than expected` : `${d} more than expected`;
}

export function countText(expected: number | null, detected: number): string {
  return expected === null ? String(detected) : `${detected} / ${expected}`;
}

/** Outcome of a gyro calibration in operator words, from the controller's message (scan/status), which
 *  holds the calibration's result for 20 s: "gyro calibrated", "gyro calibrated (could not write …)" or
 *  "calibration failed: <why>". A failed calibration keeps the previous values, so `calibrated` alone does
 *  not tell success. */
export function calibrationOutcome(message: string, calibrated: boolean): { tone: Tone; text: string; detail: string } {
  const m = message.trim();
  if (m.startsWith("calibration failed")) {
    return { tone: "bad", detail: m.replace(/^calibration failed:\s*/, ""),
             text: "Calibration failed: the device was not still. Put it down on a steady surface and try again." };
  }
  if (m.startsWith("gyro calibrated") && m.includes("could not write")) {
    return { tone: "warn", detail: m, text: "Calibrated, but it could not be saved. Calibrate again after the next start." };
  }
  if (m.startsWith("gyro calibrated") || calibrated) return { tone: "ok", detail: "", text: "Calibration done." };
  return { tone: "bad", detail: m, text: "Calibration did not finish. Try again." };
}
