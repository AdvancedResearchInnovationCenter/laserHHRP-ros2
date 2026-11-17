import type { Snapshot } from "../api/types";
import { countText, mismatchText, plural, TARGET_NOUN } from "../lib/format";
import { CountCircles } from "../ui/CountCircles";
import { FeatureMap } from "../ui/FeatureMap";
import { useBoxSize } from "../ui/useBoxSize";

// Scanning and Finishing Analysis: counts fill asynchronously, also after the switch is released.
export function Scanning({ snap }: { snap: Snapshot }) {
  const scan = snap.current_scan;
  const finishing = snap.screen === "finishing_analysis";
  const [circRef, cw, ch] = useBoxSize<HTMLDivElement>(440, 230);
  const [mapRef, mw, mh] = useBoxSize<HTMLDivElement>(300, 300);
  const features = scan?.features ?? [];
  const targets = features.filter((f) => f.is_target);
  const mode = scan?.mode ?? snap.inspection?.mode ?? "csk_depth";
  const others = snap.pending_scans.filter((s) => s.scan_id !== scan?.scan_id);
  const mismatch = scan ? mismatchText(scan.expected_count, scan.detected_target_count) : null;

  return (
    <div className="screen scanning-screen">
      <div className="scan-left">
        <div className={finishing ? "scan-state warn" : "scan-state info"}>
          <span className="scan-title">{finishing ? "Finishing analysis" : "Scanning"}</span>
          <span className="scan-sub">
            {finishing
              ? "Switch released · detections can still arrive"
              : `${(snap.hardware?.distance_mm ?? 0).toFixed(0)} mm`}
          </span>
        </div>
        <div className="scan-name muted">{scan?.display_name ?? ""}</div>
        <div className="count-big" data-testid="count">
          {scan ? countText(scan.expected_count, scan.detected_target_count) : "—"}
          <span className="count-unit"> {TARGET_NOUN[mode][1]}</span>
        </div>
        <div className="circles-box" ref={circRef}>
          {scan && <CountCircles expected={scan.expected_count} detected={scan.detected_target_count}
                                 targets={targets} width={cw} height={ch} />}
        </div>
        <div className="scan-notes">
          {scan && scan.partial_target_count > 0 && <span className="pill warn">{scan.partial_target_count} partial</span>}
          {scan && scan.non_target_count > 0 && (
            <span className="pill neutral">{plural(scan.non_target_count, ["other feature", "other features"])} not counted</span>
          )}
          {finishing && mismatch && <span className="pill neutral">{mismatch} so far</span>}
          {others.length > 0 && <span className="pill warn">{others.map((s) => s.display_name).join(", ")} analyzing</span>}
        </div>
      </div>
      <div className="scan-map" ref={mapRef}>
        <FeatureMap features={features} width={mw} height={mh} compact />
      </div>
    </div>
  );
}
