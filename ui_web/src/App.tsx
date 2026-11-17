// Phase 1 scaffold: drives the whole backend workflow at 800 x 480 with plain controls.
// The designed operator screens (count circles, SVG results map, history) replace this in Phase 4.
import { useState } from "react";
import { api, type Result } from "./api/client";
import type { Preview, Scan, Snapshot } from "./api/types";
import { useBackend } from "./state/useBackend";

const MODE_LABEL = { csk_depth: "CSK Depth", rivet_flushness: "Rivet Flushness" } as const;
const SCENARIOS = ["normal", "short", "fast_next", "zero", "reordered", "missing_result", "error_stop", "detector_failure"];

export function App() {
  const { snap, connected } = useBackend();
  const [error, setError] = useState<string | null>(null);

  const run = async (p: Promise<Result<unknown>>) => {
    const r = await p;
    setError(r.ok ? null : r.error.message);
  };

  if (!snap) return <div className="frame center">{connected ? "Loading…" : "Connecting to backend…"}</div>;

  return (
    <div className="frame">
      <header>
        <strong>{snap.screen.replace("_", " ").toUpperCase()}</strong>
        {snap.inspection && <span>{snap.inspection.label} · {MODE_LABEL[snap.inspection.mode]}</span>}
        {snap.operator && <span>{snap.operator.operator_id}</span>}
        <span className={connected ? "ok" : "bad"}>{connected ? "live" : "offline"}</span>
      </header>
      {[...snap.alerts.map((a) => a.message), ...snap.warnings].map((w) => <div key={w} className="warn">{w}</div>)}
      {error && <div className="bad">{error}</div>}
      {snap.preview_notice && <div className="muted small">{snap.preview_notice}</div>}
      <main><Body snap={snap} run={run} /></main>
      {snap.source === "mock" && <MockBar run={run} />}
    </div>
  );
}

function Body({ snap, run }: { snap: Snapshot; run: (p: Promise<Result<unknown>>) => void }) {
  const [text, setText] = useState("");
  const [count, setCount] = useState<string>("");
  const insp = snap.inspection;
  const scan = snap.current_scan;

  switch (snap.screen) {
    case "identify":
      return (
        <div className="col">
          <label>Operator ID <input value={text} onChange={(e) => setText(e.target.value)} /></label>
          <button onClick={() => run(api.setOperator(text))}>Continue</button>
        </div>
      );
    case "setup":
      return (
        <div className="col">
          <label>Inspection ID (empty: generated) <input value={text} onChange={(e) => setText(e.target.value)} /></label>
          <div className="row">
            <button className="big" onClick={() => run(api.createInspection("csk_depth", text))}>CSK Depth</button>
            <button className="big" onClick={() => run(api.createInspection("rivet_flushness", text))}>Rivet Flushness</button>
          </div>
          <button onClick={() => run(api.logout())}>Log out</button>
        </div>
      );
    case "ready":
    case "not_ready":
      return (
        <div className="col">
          <div className="row">
            <label>Expected features
              <input inputMode="numeric" value={count} placeholder={String(insp?.next_expected_count ?? "")}
                     onChange={(e) => setCount(e.target.value)} />
            </label>
            <button onClick={() => insp && run(api.setExpected(insp.uuid, Number(count)))}>Set</button>
          </div>
          {snap.screen === "ready"
            ? <div className="ok big-text">Ready: hold the button to scan</div>
            : <ul className="bad">{snap.not_ready_reasons.map((r) => <li key={r}>{r}</li>)}</ul>}
          {snap.detector.error && !snap.detector.ready &&
            <button onClick={() => run(api.restartDetector())}>Restart detector</button>}
          {snap.preview && <PreviewView p={snap.preview} />}
          <History scans={snap.recent_scans} run={run} />
          <button onClick={() => insp && run(api.endInspection(insp.uuid))}>End inspection</button>
        </div>
      );
    case "scanning":
    case "finishing_analysis":
      if (snap.preview_notice && snap.preview) return <PreviewView p={snap.preview} />;
      return scan ? <Progress scan={scan} distance={snap.hardware?.distance_mm ?? 0} /> : null;
    case "results":
      return scan ? (
        <div className="col">
          <Counts scan={scan} />
          <div className="features">
            {(scan.features ?? []).map((f) => (
              <span key={f.result_seq} className={f.is_target ? (f.partial ? "partial" : "") : "muted"}>
                #{f.feature_id} {f.kind} ({f.x_mm.toFixed(1)}, {f.y_mm.toFixed(1)}) Ø{f.diameter_mm?.toFixed(2) ?? "?"}
                {f.partial ? " partial" : ""} · Not measured
              </span>
            ))}
          </div>
          <div className="row">
            <button className="big" onClick={() => run(api.decide(scan.scan_id, "accept"))}>Accept</button>
            <button className="big" onClick={() => run(api.decide(scan.scan_id, "rescan"))}>Rescan</button>
          </div>
        </div>
      ) : null;
    case "analysis_failed":
      return scan ? (
        <div className="col">
          <div className="bad big-text">Analysis failed: {scan.analysis_error}</div>
          <Counts scan={scan} />
          <div className="row">
            {scan.replay_eligible && <button onClick={() => run(api.failureAction(scan.scan_id, "retry"))}>Retry analysis</button>}
            <button className="big" onClick={() => run(api.failureAction(scan.scan_id, "rescan"))}>Rescan</button>
            <button onClick={() => run(api.failureAction(scan.scan_id, "keep_incomplete"))}>Keep incomplete</button>
          </div>
        </div>
      ) : null;
  }
}

function Counts({ scan }: { scan: Scan }) {
  return (
    <div className={scan.count_mismatch ? "warn big-text" : "big-text"}>
      {scan.display_name}: {scan.detected_target_count} / {scan.expected_count ?? "?"}
      {scan.partial_target_count > 0 && ` (${scan.partial_target_count} partial)`}
      {scan.count_mismatch && " — count differs from expected"}
    </div>
  );
}

function Progress({ scan, distance }: { scan: Scan; distance: number }) {
  const n = Math.max(scan.expected_count ?? 0, scan.detected_target_count);
  const shown = Math.min(n, 40);
  return (
    <div className="col">
      <div className="big-text">{scan.acquisition === "scanning" ? `Recording · ${distance.toFixed(0)} mm` : "Finishing analysis…"}</div>
      <Counts scan={scan} />
      <div className="circles">
        {Array.from({ length: shown }, (_, i) => <span key={i} className={i < scan.detected_target_count ? "dot on" : "dot"} />)}
        {n > shown && <span>+{n - shown}</span>}
      </div>
    </div>
  );
}

function PreviewView({ p }: { p: Preview }) {
  const n = Math.min(Math.max(p.expected_count ?? 0, p.detected_target_count), 40);
  return (
    <div className="col">
      <div className="big-text">
        {p.scanning ? `Recording · ${p.distance_mm.toFixed(0)} mm` : "Last scan (preview, not recorded)"}:{" "}
        {p.detected_target_count} / {p.expected_count ?? "?"}
        {p.partial_target_count > 0 && ` (${p.partial_target_count} partial)`}
      </div>
      {p.unassigned_reason && <div className="warn">Not shown for an inspection: {p.unassigned_reason}</div>}
      <div className="circles">
        {Array.from({ length: n }, (_, i) => <span key={i} className={i < p.detected_target_count ? "dot on" : "dot"} />)}
      </div>
      <div className="features">
        {p.features.map((f) => (
          <span key={f.result_seq} className={f.is_target ? (f.partial ? "partial" : "") : "muted"}>
            #{f.feature_id} {f.kind} ({f.x_mm.toFixed(1)}, {f.y_mm.toFixed(1)}){f.after_release ? " · after release" : ""} · Not measured
          </span>
        ))}
      </div>
    </div>
  );
}

function History({ scans, run }: { scans: Scan[]; run: (p: Promise<Result<unknown>>) => void }) {
  if (!scans.length) return null;
  return (
    <table>
      <tbody>
        {scans.slice(0, 6).map((s) => (
          <tr key={s.scan_id}>
            <td>{s.display_name}</td><td>{s.detected_target_count}/{s.expected_count ?? "?"}</td><td>{s.status}</td>
            <td>{s.status === "undecided" && <>
              <button onClick={() => run(api.decide(s.scan_id, "accept"))}>Accept</button>
              <button onClick={() => run(api.decide(s.scan_id, "rescan"))}>Rescan</button></>}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function MockBar({ run }: { run: (p: Promise<Result<unknown>>) => void }) {
  return (
    <footer>
      <span className="muted">mock</span>
      <button onPointerDown={() => run(api.mock.press())} onPointerUp={() => run(api.mock.release())}>Hold to scan</button>
      <select onChange={(e) => e.target.value && run(api.mock.scenario(e.target.value))} value="">
        <option value="">scenario…</option>
        {SCENARIOS.map((s) => <option key={s}>{s}</option>)}
      </select>
    </footer>
  );
}
