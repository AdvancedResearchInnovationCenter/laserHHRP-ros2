// Operator UI shell (plan section 9): 800 x 480, header with readiness, one screen chosen by the backend.
// The backend's `screen` decides the lifecycle view; the only local navigation is History.
import { useCallback, useEffect, useRef, useState } from "react";
import { api, type Result } from "./api/client";
import type { Snapshot } from "./api/types";
import { History, PastInspections } from "./screens/History";
import { Identify } from "./screens/Identify";
import { Ready } from "./screens/Ready";
import { Results } from "./screens/Results";
import { Scanning } from "./screens/Scanning";
import { Setup } from "./screens/Setup";
import type { Run } from "./screens/common";
import { AlertStrip, Header } from "./ui/Header";
import { useBackend } from "./state/useBackend";

type Overlay = { kind: "history"; uuid: string; back: Overlay | null } | { kind: "past" } | null;

export function App() {
  const { snap, connected } = useBackend();
  const [toast, setToast] = useState<string | null>(null);
  const timer = useRef<number>();

  const run: Run = useCallback(async <T,>(p: Promise<Result<T>>) => {
    const r = await p;
    if (!r.ok) {
      setToast(r.error.message);
      window.clearTimeout(timer.current);
      timer.current = window.setTimeout(() => setToast(null), 5000);
    }
    return r;
  }, []);

  if (!snap) {
    return (
      <div className="app">
        <div className="boot">{connected ? "Loading…" : "Connecting to the scanner backend…"}</div>
      </div>
    );
  }
  return <Shell snap={snap} connected={connected} run={run} toast={toast} />;
}

export function Shell({ snap, connected, run, toast }: {
  snap: Snapshot; connected: boolean; run: Run; toast?: string | null;
}) {
  const [overlay, setOverlay] = useState<Overlay>(null);
  // A physical scan start (or a sign-out) always takes over the screen.
  useEffect(() => {
    if (snap.screen === "scanning" || snap.screen === "identify") setOverlay(null);
    if (overlay?.kind === "past" && snap.screen !== "setup") setOverlay(null);
  }, [snap.screen]); // eslint-disable-line react-hooks/exhaustive-deps

  const openHistory = () => snap.inspection && setOverlay({ kind: "history", uuid: snap.inspection.uuid, back: null });
  let body;
  if (overlay?.kind === "history") {
    body = <History snap={snap} uuid={overlay.uuid} run={run} onBack={() => setOverlay(overlay.back)} />;
  } else if (overlay?.kind === "past") {
    body = <PastInspections onBack={() => setOverlay(null)}
                            onOpen={(uuid) => setOverlay({ kind: "history", uuid, back: { kind: "past" } })} />;
  } else {
    body = <Body snap={snap} run={run} openHistory={openHistory} openPast={() => setOverlay({ kind: "past" })} />;
  }
  const kiosk = typeof location !== "undefined" && new URLSearchParams(location.search).has("kiosk");
  const dev = snap.source === "mock" && !kiosk;
  return (
    <div className="app">
      <Header snap={snap} connected={connected} />
      <AlertStrip snap={snap} />
      <main className={connected ? "main" : "main stale"}>{body}</main>
      {!connected && (
        <div className="offline" role="alert">
          <strong>Connection to the backend lost</strong>
          <span>Reconnecting… This screen is not current.</span>
          <span className="muted">Scanning and recording on the device are not affected.</span>
        </div>
      )}
      {toast && <div className="toast" role="status">{toast}</div>}
      {dev && <DevBar run={run} />}
    </div>
  );
}

function Body({ snap, run, openHistory, openPast }: {
  snap: Snapshot; run: Run; openHistory: () => void; openPast: () => void;
}) {
  const scanKey = snap.current_scan?.scan_id ?? "none";
  switch (snap.screen) {
    case "identify":
      return <Identify run={run} />;
    case "setup":
      return <Setup snap={snap} run={run} openPast={openPast} />;
    case "ready":
    case "not_ready":
      return <Ready key={snap.inspection?.uuid} snap={snap} run={run} openHistory={openHistory} />;
    case "scanning":
    case "finishing_analysis":
      return <Scanning snap={snap} />;
    case "results":
    case "analysis_failed":
      return snap.current_scan
        ? <Results key={scanKey} snap={snap} run={run} openHistory={openHistory} />
        : null;
  }
}

const SCENARIOS = ["normal", "short", "fast_next", "zero", "reordered", "missing_result", "error_stop", "detector_failure"];

/** Mock source only (developer run): stands in for the physical switch. */
function DevBar({ run }: { run: Run }) {
  const [open, setOpen] = useState(false);
  return (
    <div className={open ? "devbar open" : "devbar"}>
      <button className="dev-toggle" onClick={() => setOpen(!open)}>DEV</button>
      {open && (
        <>
          <button className="dev-hold" onPointerDown={() => void run(api.mock.press())}
                  onPointerUp={() => void run(api.mock.release())}
                  onPointerLeave={(e) => e.buttons && void run(api.mock.release())}>Hold to scan</button>
          <select value="" onChange={(e) => e.target.value && void run(api.mock.scenario(e.target.value))}>
            <option value="">Scenario…</option>
            {SCENARIOS.map((s) => <option key={s}>{s}</option>)}
          </select>
        </>
      )}
    </div>
  );
}
