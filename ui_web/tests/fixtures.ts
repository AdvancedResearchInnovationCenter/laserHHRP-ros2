import type { Feature, Scan, Snapshot } from "../src/api/types";

export function feature(i: number, over: Partial<Feature> = {}): Feature {
  return {
    feature_id: i + 1, result_seq: i, kind: "countersink", is_target: true,
    x_mm: (i % 2) * 30 - 15, y_mm: 15 + Math.floor(i / 2) * 25, z_mm: 0.2, diameter_mm: 9.5, partial: false,
    quality: { coverage: 0.9, scanned_share: 1, rms_mm: 0.012 },
    measurement: { name: "csk_depth", value_mm: null, unit: "mm", status: "not_measured" },
    ...over,
  };
}

export function scan(over: Partial<Scan> = {}): Scan {
  const features = over.features ?? Array.from({ length: 20 }, (_, i) => feature(i));
  const targets = features.filter((f) => f.is_target);
  return {
    scan_id: "scan-1", scan_number: 1, display_name: "Scan 001", inspection_uuid: "insp-1", mode: "csk_depth",
    operator_id: "ENG-1042", expected_count: 20, detected_target_count: targets.length,
    non_target_count: features.length - targets.length, partial_target_count: targets.filter((f) => f.partial).length,
    count_mismatch: targets.length !== (over.expected_count ?? 20), acquisition: "ended", acquisition_note: null,
    analysis: "complete", analysis_error: null, decision: null, decided_by: null, decided_at: null,
    status: "undecided", unassigned_reason: null, started_at: "2026-11-17T09:00:00.000Z",
    ended_at: "2026-11-17T09:00:10.000Z", clock_quality: "synced", frame_count: 900, replay_eligible: false,
    retry_available: false,
    raw: { session_id: null, dir: null, state: null, frame_count: null, note: null },
    ...over, features,
  };
}

export function snapshot(over: Partial<Snapshot> = {}): Snapshot {
  return {
    screen: "ready",
    operator: { operator_id: "ENG-1042", started_at: "2026-11-17T08:00:00.000Z" },
    inspection: {
      uuid: "insp-1", label: "INS-20261117-001", mode: "csk_depth", status: "open", operator_id: "ENG-1042",
      next_expected_count: 20, created_at: "2026-11-17T08:30:00.000Z", closed_at: null, clock_quality: "synced",
      scan_count: 0,
    },
    ready: true, not_ready_reasons: [], warnings: [], alerts: [],
    hardware: {
      fresh: true, scan_state: "IDLE", calibrated: true, laser_ok: true, nano_ok: true, encoder_ok: true,
      free_mb: 9000, storage_write_failed: false, distance_mm: 0, message: "", fatal: [], warnings: [],
    },
    detector: { mode: "csk_depth", alive: true, ready: true, error: "" },
    source: "ros", clock_quality: "synced", current_scan: null, recent_scans: [], pending_scans: [],
    preview: null, preview_notice: null,
    ...over,
  };
}
