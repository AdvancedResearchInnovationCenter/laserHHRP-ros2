// Operator device actions (backend device.py): turning the device off and the gyro calibration, so the device
// needs no terminal. The backend refuses both during a scan (power off also while an analysis is pending);
// the screens only offer them where that cannot happen.
import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type { Screen, Snapshot } from "../api/types";
import { calibrationOutcome } from "../lib/format";
import type { Run } from "../screens/common";
import { Confirm, Modal } from "./Modal";

/** Screens with nothing being scanned or analysed. */
export const POWER_OFF_SCREENS: readonly Screen[] = ["identify", "setup", "ready", "not_ready"];

export function offerPowerOff(snap: Snapshot, connected: boolean): boolean {
  return connected && snap.device.can_power_off && !snap.device.powering_off && POWER_OFF_SCREENS.includes(snap.screen);
}

export function PowerButton({ run }: { run: Run }) {
  const [confirm, setConfirm] = useState(false);
  return (
    <>
      <button className="hdr-power" aria-label="Turn off" data-testid="power-off" onClick={() => setConfirm(true)}>⏻</button>
      {confirm && (
        <Confirm title="Turn off the device?" tone="danger" confirmLabel="Turn off"
                 text={<>The scanner shuts down. Inspections and results are kept.<br />
                   <strong>Wait until the screen is dark</strong> before you remove the power.</>}
                 onCancel={() => setConfirm(false)}
                 onConfirm={() => { setConfirm(false); void run(api.powerOff()); }} />
      )}
    </>
  );
}

export function PoweringOff() {
  return (
    <div className="screen powering-off" role="alert" data-testid="powering-off">
      <div className="status-big">Turning off…</div>
      <p>Wait until the screen is dark, then remove the power.</p>
    </div>
  );
}

type Phase = "confirm" | "starting" | "running" | "done";
const NOT_SEEN_MS = 4000;     // the controller reports CALIBRATING within 0.2 s; without it, show the outcome

/** Instruction, then "keep still" while the backend reports the calibration running, then its outcome. */
export function CalibrateDialog({ snap, run, onClose }: { snap: Snapshot; run: Run; onClose: () => void }) {
  const [phase, setPhase] = useState<Phase>("confirm");
  const seen = useRef(false);
  const calibrating = !!snap.hardware?.calibrating;

  useEffect(() => {
    if (phase !== "running") return;
    if (calibrating) seen.current = true;
    else if (seen.current) setPhase("done");
  }, [phase, calibrating]);
  useEffect(() => {
    if (phase !== "running") return;
    const t = window.setTimeout(() => !seen.current && setPhase("done"), NOT_SEEN_MS);
    return () => window.clearTimeout(t);
  }, [phase]);

  const start = async () => {
    seen.current = false;
    setPhase("starting");
    const r = await run(api.calibrate());
    if (r.ok) setPhase("running");
    else onClose();                       // the reason is shown as a toast
  };

  if (phase === "confirm") {
    return (
      <Confirm title="Calibrate the gyro" confirmLabel="Start calibration"
               text={<>Put the device down on a flat, steady surface and do not touch it.<br />
                 It takes a few seconds.</>}
               onCancel={onClose} onConfirm={() => void start()} />
    );
  }
  if (phase !== "done") {
    return (
      <Modal title="Calibrating">
        <div className="modal-text calib-running" data-testid="calibrating">Keep the device still…</div>
      </Modal>
    );
  }
  const out = calibrationOutcome(snap.hardware?.message ?? "", !!snap.hardware?.calibrated);
  return (
    <Modal title="Calibration" onClose={onClose}>
      <div className={`modal-text ${out.tone}-text`} data-testid="calibration-outcome">{out.text}</div>
      {out.detail && <div className="muted small">{out.detail}</div>}
      <div className="modal-actions">
        {out.tone === "bad" && <button className="secondary" onClick={() => setPhase("confirm")}>Try again</button>}
        <button className="primary" onClick={onClose}>OK</button>
      </div>
    </Modal>
  );
}
