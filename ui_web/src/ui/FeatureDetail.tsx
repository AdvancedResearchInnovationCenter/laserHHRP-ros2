import type { Feature } from "../api/types";
import { KIND_LABEL, MEASUREMENT_LABEL, mm, pct } from "../lib/format";

// Geometry and detector quality are observations; the inspection measurement stays "Not measured"
// until a validated algorithm exists (plan sections 1, 8). No pass/fail is ever shown.
export function FeatureDetail({ f }: { f: Feature }) {
  const m = f.measurement;
  return (
    <div className="detail">
      <div className="detail-head">
        <strong>#{f.feature_id} {KIND_LABEL[f.kind] ?? f.kind}</strong>
        {f.partial && <span className="pill warn" title="Detector flagged this feature as boundary-limited">Partial</span>}
        {!f.is_target && <span className="pill neutral">Not counted</span>}
      </div>
      <dl>
        <dt>Position</dt><dd>x {f.x_mm.toFixed(1)} · y {f.y_mm.toFixed(1)} mm</dd>
        <dt>Diameter</dt><dd>{mm(f.diameter_mm, 2)}</dd>
        <dt>Edge coverage</dt><dd>{pct(f.quality.coverage)}</dd>
        <dt>Scanned share</dt><dd>{pct(f.quality.scanned_share)}</dd>
        <dt>Fit RMS</dt><dd>{mm(f.quality.rms_mm, 3)}</dd>
        <dt>{MEASUREMENT_LABEL[m.name] ?? m.name}</dt>
        <dd className={m.status === "measured" ? "" : "muted"}>
          {m.status === "measured" && m.value_mm !== null ? mm(m.value_mm, 3) : "Not measured"}
        </dd>
      </dl>
    </div>
  );
}
