import type { Feature } from "../api/types";
import { KIND_LABEL, MEASUREMENT_LABEL, measurementReason, measurementValue, rangeText, VERDICT } from "../lib/format";

// One feature for the operator: what it is, where, and its measurement with the GO / NO-GO verdict the backend
// recorded (against the GO range shown). Detector quality figures stay in the export, not on screen.
export function FeatureDetail({ f }: { f: Feature }) {
  const m = f.measurement;
  const verdict = m.status === "measured" && m.verdict ? VERDICT[m.verdict] : null;
  const reason = measurementReason(m);
  const range = rangeText(m);
  return (
    <div className="detail">
      <div className="detail-head">
        <strong>#{f.feature_id} {KIND_LABEL[f.kind] ?? f.kind}</strong>
        {verdict && <span className={`pill ${verdict.tone}`} data-testid="verdict">{verdict.label}</span>}
        {f.partial && <span className="pill warn" title="Detector flagged this feature as boundary-limited">Partial</span>}
        {!f.is_target && <span className="pill neutral">Not counted</span>}
      </div>
      <dl>
        <dt>Position</dt><dd>x {f.x_mm.toFixed(1)} · y {f.y_mm.toFixed(1)} mm</dd>
        {f.is_target && (
          <>
            <dt>{MEASUREMENT_LABEL[m.name] ?? m.name}</dt>
            <dd className={m.status === "measured" ? "measure-value" : "muted"} data-testid="measurement">
              {measurementValue(m)}
            </dd>
            {reason && <><dt>Why</dt><dd className="muted">{reason}</dd></>}
            {range && <><dt>GO range</dt><dd className="muted">{range}</dd></>}
          </>
        )}
      </dl>
    </div>
  );
}
