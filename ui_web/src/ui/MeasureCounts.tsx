import type { Scan } from "../api/types";

/** GO / NO-GO / not measured counts of a scan's target features (the backend's verdicts). */
export function MeasureCounts({ scan }: { scan: Scan }) {
  const c = scan.measurement;
  if (!c || scan.analysis !== "complete") return null;
  return (
    <>
      {c.in_limits > 0 && <span className="pill ok" data-testid="count-go">{c.in_limits} GO</span>}
      {c.out_of_limits > 0 && <span className="pill bad" data-testid="count-nogo">{c.out_of_limits} NO-GO</span>}
      {c.not_measured > 0 && <span className="pill neutral" data-testid="count-unmeasured">{c.not_measured} not measured</span>}
    </>
  );
}
