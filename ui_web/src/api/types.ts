// Backend contract v1 (ui_backend/laser_hhrp_ui/api.py, state.py). The backend is authoritative;
// the frontend never derives lifecycle, counts or target filtering itself.

export type Screen =
  | "identify" | "setup" | "not_ready" | "ready" | "scanning"
  | "finishing_analysis" | "results" | "analysis_failed";

export type Mode = "csk_depth" | "rivet_flushness";

// The measurement of a target feature by inspection_node (after the scan's detection finished). `pending`: not
// (yet) measured, e.g. the scan failed first, or a non-target feature. The verdict is the backend's, against the
// inclusive GO range the value was judged with.
export interface Measurement {
  name: string; value_mm: number | null; unit: string;
  status: "pending" | "measured" | "not_measured";
  reason?: string | null;                       // not_measured: algorithm reason code (lib/format REASON_TEXT)
  verdict?: "in_limits" | "out_of_limits" | null;
  lower_mm?: number | null; upper_mm?: number | null;
}

export interface MeasurementCounts { in_limits: number; out_of_limits: number; not_measured: number; pending: number }

export interface Feature {
  feature_id: number; result_seq: number; kind: string; is_target: boolean;
  x_mm: number; y_mm: number; z_mm: number; diameter_mm: number | null; partial: boolean;
  quality: { coverage: number | null; scanned_share: number | null; rms_mm: number | null };
  measurement: Measurement;
  detected_at?: string;
}

export interface RawRecording {
  session_id: string | null; dir: string | null; state: string | null; frame_count: number | null; note: string | null;
}

export type ScanStatus =
  | "unassigned" | "scanning" | "analyzing" | "accepted" | "accepted_with_warning" | "rescanned"
  | "incomplete_retained" | "undecided" | "failed";

export interface Scan {
  scan_id: string; scan_number: number | null; display_name: string; inspection_uuid: string | null;
  mode: Mode | null; operator_id: string | null; expected_count: number | null;
  detected_target_count: number; non_target_count: number; partial_target_count: number;
  measurement?: MeasurementCounts;   // target features by measurement outcome
  count_mismatch: boolean | null; acquisition: string; acquisition_note: string | null;
  analysis: "pending" | "complete" | "failed" | "interrupted"; analysis_error: string | null;
  decision: string | null; decided_by: string | null; decided_at: string | null;
  status: ScanStatus; unassigned_reason: string | null;
  started_at: string; ended_at: string | null; clock_quality: string; frame_count: number | null;
  replay_eligible: boolean;
  retry_available: boolean;          // verified raw data AND a replay path: only then offer Retry
  raw: RawRecording;
  features?: Feature[];
}

export interface Inspection {
  uuid: string; label: string; mode: Mode; status: "open" | "closed"; operator_id: string;
  next_expected_count: number | null; created_at: string; closed_at: string | null;
  clock_quality: string; scan_count: number;
}

export interface Alert { code: string; message: string; scan_id: string | null; at: string }

export interface Snapshot {
  screen: Screen;
  operator: { operator_id: string; started_at: string } | null;
  inspection: Inspection | null;
  ready: boolean; not_ready_reasons: string[]; warnings: string[]; alerts: Alert[];
  hardware: Hardware | null;
  detector: { mode: Mode | null; alive: boolean; ready: boolean; error: string };
  source: "none" | "mock" | "ros"; clock_quality: string;
  // operator device actions (backend device.py): which are offered, and whether the device is turning off
  device: {
    can_calibrate: boolean; can_power_off: boolean; powering_off: boolean;
    can_service: boolean; switching: "desktop" | "restart" | null;    // developer service menu (PIN)
  };
  current_scan: Scan | null; recent_scans: Scan[]; pending_scans: Scan[];
  preview: Preview | null;            // Phase 2 live view: shown, never recorded
  preview_notice: string | null;
  event_seq?: number;
}

export interface Hardware {
  fresh: boolean; scan_state: string; calibrated: boolean; calibrating: boolean; laser_ok: boolean; nano_ok: boolean; encoder_ok: boolean;
  free_mb: number | null; storage_write_failed: boolean; distance_mm: number; message: string;
  fatal: string[]; warnings: string[];
}

export interface UsbTarget { path: string; name: string; writable: boolean; free_mb: number | null; reason: string }

export interface Preview {
  number: number; inspection_uuid: string | null; mode: Mode | null; scanning: boolean;
  started_at: string; ended_at: string | null; stop_state: string | null; expected_count: number | null;
  detected_target_count: number; non_target_count: number; partial_target_count: number;
  ignored_results: number; distance_mm: number; unassigned_reason: string | null;
  features: (Feature & { after_release: boolean })[];
}

export interface Envelope<T = unknown> {
  version: number; event_seq: number; type: string; timestamp: string;
  inspection_id: string | null; scan_id: string | null; payload: T;
}

export interface ApiError { code: string; message: string; field: string | null }

export interface NetworkDevice { device: string; type: string; state: string; connection: string; ipv4: string[] }
