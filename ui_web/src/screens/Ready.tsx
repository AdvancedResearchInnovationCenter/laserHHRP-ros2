import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { Snapshot } from "../api/types";
import { countText, statusOf, TARGET_NOUN } from "../lib/format";
import { Keypad } from "../ui/Keyboard";
import { Confirm, Modal } from "../ui/Modal";
import type { Run } from "./common";

const MIN = 1, MAX = 500;
const clamp = (n: number) => Math.min(MAX, Math.max(MIN, n));

export function Ready({ snap, run, openHistory }: { snap: Snapshot; run: Run; openHistory: () => void }) {
  const insp = snap.inspection!;
  const ready = snap.screen === "ready";
  const count = insp.next_expected_count;
  const [pending, setPending] = useState<number | null>(null);    // optimistic stepper value
  const [pad, setPad] = useState<string | null>(null);
  const [confirmEnd, setConfirmEnd] = useState(false);
  useEffect(() => setPending(null), [count]);
  const shown = pending ?? count;

  const setCount = async (n: number) => {
    setPending(n);
    const r = await run(api.setExpected(insp.uuid, n));
    if (!r.ok) setPending(null);
  };
  const padValue = pad === null ? NaN : Number(pad);
  const padOk = Number.isInteger(padValue) && padValue >= MIN && padValue <= MAX;
  const last = snap.recent_scans[0];
  const others = snap.pending_scans.filter((s) => s.inspection_uuid === insp.uuid);
  const detectorProblem = !snap.detector.ready && !!snap.detector.error;

  return (
    <div className="screen ready-screen">
      <section className={ready ? "status-panel ok" : "status-panel bad"} data-testid="status-panel">
        <div className="status-main">
        {ready ? (
          <>
            <div className="status-big">READY</div>
            <div className="status-hint">Hold the switch to scan</div>
            <div className="muted small">Release the switch to stop. Detections can still arrive afterwards.</div>
          </>
        ) : (
          <>
            <div className="status-big">NOT READY</div>
            <ul className="reasons">
              {snap.not_ready_reasons.map((r) => <li key={r}>{r}</li>)}
            </ul>
            {detectorProblem && (
              <button className="secondary" onClick={() => void run(api.restartDetector())}>Restart detector</button>
            )}
          </>
        )}
        </div>
        {others.length > 0 && (
          <div className="pending-note">{others.map((s) => s.display_name).join(", ")} still analyzing</div>
        )}
        {last && (
          <div className="last-scan">
            <span className="muted">Last:</span> {last.display_name}
            <span className={`pill ${statusOf(last).tone}`}>{statusOf(last).label}</span>
            <span>{countText(last.expected_count, last.detected_target_count)}</span>
          </div>
        )}
      </section>
      <section className={shown === null ? "count-panel needs" : "count-panel"}>
        <div className="muted">Expected {TARGET_NOUN[insp.mode][1]}, next scan</div>
        <div className="stepper">
          <button className="step" aria-label="One less" disabled={shown === null || shown <= MIN}
                  onClick={() => shown !== null && void setCount(clamp(shown - 1))}>−</button>
          <button className="count-value" onClick={() => setPad("")} data-testid="expected">
            {shown ?? "Set"}
          </button>
          <button className="step" aria-label="One more" disabled={shown !== null && shown >= MAX}
                  onClick={() => void setCount(clamp((shown ?? 0) + 1))}>+</button>
        </div>
        <div className="muted small">Remembered for the following scans. A count match never stops the scan.</div>
      </section>
      <div className="bar">
        <button className="secondary" onClick={openHistory}>History ({insp.scan_count})</button>
        <span className="spacer" />
        <button className="secondary" onClick={() => setConfirmEnd(true)}>End inspection</button>
      </div>
      {pad !== null && (
        <Modal title={`Expected ${TARGET_NOUN[insp.mode][1]} (1–500)`} onClose={() => setPad(null)}>
          <div className={padOk || pad === "" ? "pad-value" : "pad-value invalid"}>{pad || <span className="muted">{shown ?? "—"}</span>}</div>
          <Keypad value={pad} onChange={setPad} canSubmit={padOk}
                  onSubmit={() => { setPad(null); void setCount(padValue); }} />
        </Modal>
      )}
      {confirmEnd && (
        <Confirm title="End inspection?" confirmLabel="End inspection"
                 text={<>Closes <strong>{insp.label}</strong>. Its scans stay in history. The next inspection can use a different mode.</>}
                 onCancel={() => setConfirmEnd(false)}
                 onConfirm={() => { setConfirmEnd(false); void run(api.endInspection(insp.uuid)); }} />
      )}
    </div>
  );
}
