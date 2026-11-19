// Display wording only. Status, counts and target filtering come from the backend.
import type { Mode, Scan, ScanStatus } from "../api/types";

export const MODE_LABEL: Record<Mode, string> = { csk_depth: "CSK Depth", rivet_flushness: "Rivet Flushness" };
export const TARGET_NOUN: Record<Mode, [string, string]> = {
  csk_depth: ["countersink", "countersinks"],
  rivet_flushness: ["rivet", "rivets"],
};
export const MEASUREMENT_LABEL: Record<string, string> = { csk_depth: "CSK depth", flushness: "Flushness" };
export const KIND_LABEL: Record<string, string> = { countersink: "Countersink", hole: "Plain hole", rivet: "Rivet head" };

export type Tone = "ok" | "warn" | "bad" | "info" | "neutral";

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
