import { useState } from "react";
import { api } from "../api/client";
import type { Scan, Snapshot } from "../api/types";
import { countText, mismatchText, plural, TARGET_NOUN } from "../lib/format";
import { FeatureDetail } from "../ui/FeatureDetail";
import { FeatureMap } from "../ui/FeatureMap";
import { scanOrder, stepSelection } from "../lib/map";
import { MeasureCounts } from "../ui/MeasureCounts";
import { Confirm } from "../ui/Modal";
import { useBoxSize } from "../ui/useBoxSize";
import type { Run } from "./common";

export function retryUnavailableReason(scan: Scan): string {
  if (scan.retry_available) return "";
  if (scan.replay_eligible) return "the raw recording is verified, but replay into the detector is not implemented yet";
  return scan.raw?.note || "no verified raw recording";
}

/** Results (analysis complete) and Analysis Failed (never acceptable). */
export function Results({ snap, run, openHistory }: { snap: Snapshot; run: Run; openHistory: () => void }) {
  const scan = snap.current_scan!;
  const failed = snap.screen === "analysis_failed";
  return (
    <div className="screen results-screen">
      <ScanReview scan={scan} failed={failed} />
      <div className="bar">
        <button className="secondary" onClick={openHistory}>History</button>
        <span className="spacer" />
        <ScanActions scan={scan} failed={failed} run={run} />
      </div>
    </div>
  );
}

export function ScanReview({ scan, failed, mapW = 470, mapH = 330 }: {
  scan: Scan; failed: boolean; mapW?: number; mapH?: number;
}) {
  const [sel, setSel] = useState<number | null>(null);
  const [mapRef, w, h] = useBoxSize<HTMLDivElement>(mapW, mapH);
  const features = scan.features ?? [];
  const selected = features.find((f) => f.result_seq === sel) ?? null;
  const mode = scan.mode ?? "csk_depth";
  const mismatch = mismatchText(scan.expected_count, scan.detected_target_count);
  return (
    <div className="review">
      <div className="review-map" ref={mapRef}>
        <FeatureMap key={scan.scan_id} features={features} width={w} height={h} selected={sel} onSelect={setSel} zoomable />
      </div>
      <div className="review-side">
        <div className="review-name">{scan.display_name}</div>
        {failed ? (
          <div className="banner bad" data-testid="failed-banner">
            <strong>Analysis failed</strong>
            <span>{scan.analysis_error ?? "no reason recorded"}</span>
            <span className="small">This scan cannot be accepted. Features below are what arrived before the failure.</span>
          </div>
        ) : null}
        <div className="count-mid" data-testid="count">
          {countText(scan.expected_count, scan.detected_target_count)}
          <span className="count-unit"> {TARGET_NOUN[mode][1]}</span>
        </div>
        {!failed && mismatch && (
          <div className="banner warn" data-testid="mismatch">
            <strong>{mismatch}</strong>
            <span className="small">You can still accept. Both counts are recorded.</span>
          </div>
        )}
        <div className="scan-notes">
          <MeasureCounts scan={scan} />
          {scan.partial_target_count > 0 && <span className="pill warn">{scan.partial_target_count} partial</span>}
          {scan.non_target_count > 0 && (
            <span className="pill neutral">{plural(scan.non_target_count, ["other feature", "other features"])} not counted</span>
          )}
          {!failed && scan.detected_target_count === 0 && <span className="pill neutral">Analysis complete: nothing found</span>}
        </div>
        {selected ? <FeatureDetail f={selected} />
          : features.length > 0 && <div className="muted small tap-hint">
              {features.length > 1 ? "Tap a feature on the map, or step with ‹ ›." : "Tap the feature on the map for details."}
            </div>}
        {features.length > 1 && (
          <div className="feature-step">
            <button className="secondary" aria-label="Previous feature" onClick={() => setSel(stepSelection(features, sel, -1))}>‹</button>
            <span data-testid="feature-pos">
              {selected ? `Feature ${scanOrder(features).indexOf(selected) + 1} of ${features.length}` : `${features.length} features`}
            </span>
            <button className="secondary" aria-label="Next feature" onClick={() => setSel(stepSelection(features, sel, 1))}>›</button>
          </div>
        )}
      </div>
    </div>
  );
}

export function ScanActions({ scan, failed, run }: { scan: Scan; failed: boolean; run: Run }) {
  const [keep, setKeep] = useState(false);
  if (!failed) {
    return (
      <>
        <span className="decide-hint small muted" data-testid="decide-hint">
          Accept keeps this scan. Rescan discards it: scan the row again.
        </span>
        <button className="secondary big" onClick={() => void run(api.decide(scan.scan_id, "rescan"))}>Rescan</button>
        <button className="primary big" onClick={() => void run(api.decide(scan.scan_id, "accept"))}>
          {scan.count_mismatch ? "Accept with warning" : "Accept"}
        </button>
      </>
    );
  }
  const why = retryUnavailableReason(scan);
  return (
    <>
      {scan.retry_available
        ? <button className="secondary big" onClick={() => void run(api.failureAction(scan.scan_id, "retry"))}>Retry analysis</button>
        : <span className="retry-note small muted" data-testid="retry-note">Retry unavailable: {why}</span>}
      <button className="secondary big" onClick={() => setKeep(true)}>Keep incomplete</button>
      <button className="primary big" onClick={() => void run(api.failureAction(scan.scan_id, "rescan"))}>Rescan</button>
      {keep && (
        <Confirm title="Keep as incomplete?" confirmLabel="Keep incomplete" tone="danger"
                 text={<>{scan.display_name} stays in history marked <strong>Incomplete</strong>. It is not accepted. This is recorded in the audit log.</>}
                 onCancel={() => setKeep(false)}
                 onConfirm={() => { setKeep(false); void run(api.failureAction(scan.scan_id, "keep_incomplete")); }} />
      )}
    </>
  );
}
