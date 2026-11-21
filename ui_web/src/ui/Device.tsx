// Operator device actions (backend device.py): turning the device off and the gyro calibration, so the device
// needs no terminal; and the developers' service menu (PIN): network, desktop mode, restart of the software.
// The backend refuses all of them during a scan (and while an analysis is pending, except calibration); the
// screens offer the ⋮ menu only where that cannot happen. Turn off is two taps deep on purpose.
import { useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type { NetworkDevice, Screen, Snapshot } from "../api/types";
import { calibrationOutcome } from "../lib/format";
import type { Run } from "../screens/common";
import { Keypad } from "./Keyboard";
import { Confirm, Modal } from "./Modal";

/** Screens with nothing being scanned or analysed. */
export const POWER_OFF_SCREENS: readonly Screen[] = ["identify", "setup", "ready", "not_ready"];

export function offerMenu(snap: Snapshot, connected: boolean): boolean {
  const d = snap.device;
  return connected && !d.powering_off && !d.switching && POWER_OFF_SCREENS.includes(snap.screen)
    && (d.can_power_off || d.can_reboot || d.can_service);
}

type Step = "menu" | "off" | "reboot" | "pin" | "service" | "desktop" | "restart";
const PIN_MIN = 4, PIN_MAX = 8;

export function MenuButton({ snap, run }: { snap: Snapshot; run: Run }) {
  const [step, setStep] = useState<Step | null>(null);
  const [pin, setPin] = useState("");
  const [network, setNetwork] = useState<NetworkDevice[]>([]);
  const close = () => { setStep(null); setPin(""); };
  const unlock = async () => {
    const r = await run(api.serviceUnlock(pin));
    if (r.ok) { setNetwork(r.data.network); setStep("service"); } else setPin("");
  };
  const act = async (kind: "desktop" | "restart") => {
    const p = pin;
    close();
    await run(api.service(kind, p));
  };
  return (
    <>
      <button className="hdr-menu" aria-label="Menu" data-testid="menu" onClick={() => setStep("menu")}>⋮</button>
      {step === "menu" && (
        <Modal title="Menu" onClose={close}>
          <div className="menu-list">
            {snap.device.can_reboot && <button className="secondary" onClick={() => setStep("reboot")}>Restart device…</button>}
            {snap.device.can_power_off && <button className="secondary" onClick={() => setStep("off")}>Turn off…</button>}
            {snap.device.can_service && <button className="secondary" onClick={() => setStep("pin")}>Service…</button>}
          </div>
        </Modal>
      )}
      {step === "off" && (
        <Confirm title="Turn off the device?" tone="danger" confirmLabel="Turn off"
                 text={<>The scanner shuts down. Inspections and results are kept.<br />
                   <strong>Wait until the screen is dark</strong> before you remove the power.</>}
                 onCancel={close} onConfirm={() => { close(); void run(api.powerOff()); }} />
      )}
      {step === "reboot" && (
        <Confirm title="Restart the device?" confirmLabel="Restart device"
                 text={<>The scanner restarts and this screen comes back by itself in about a minute.
                   Inspections and results are kept.</>}
                 onCancel={close} onConfirm={() => { close(); void run(api.reboot()); }} />
      )}
      {step === "pin" && (
        <Modal title="Service PIN" onClose={close}>
          <div className="pad-value" data-testid="pin">{"•".repeat(pin.length) || <span className="muted">—</span>}</div>
          <Keypad value={pin} onChange={setPin} maxLength={PIN_MAX} keepZero submitLabel="Open"
                  canSubmit={pin.length >= PIN_MIN} onSubmit={() => void unlock()} />
        </Modal>
      )}
      {step === "service" && (
        <Modal title="Service" wide onClose={close}>
          <ul className="net-list" data-testid="network">
            {network.length === 0 && <li className="muted">No network</li>}
            {network.map((n) => (
              <li key={n.device}>
                <strong>{n.type === "wifi" ? "Wi-Fi" : n.type === "ethernet" ? "Ethernet" : n.device}</strong>
                {" "}{n.connection || n.state || "—"}{n.ipv4.length > 0 && <> · {n.ipv4.join(", ")}</>}
                <span className="muted"> ({n.device})</span>
              </li>
            ))}
          </ul>
          <div className="modal-actions">
            <button className="secondary" onClick={() => setStep("restart")}>Restart scanner software</button>
            <button className="primary" onClick={() => setStep("desktop")}>Desktop mode</button>
          </div>
        </Modal>
      )}
      {step === "desktop" && (
        <Confirm title="Desktop mode?" confirmLabel="Open the desktop"
                 text={<>Stops the scanner software and opens the Ubuntu desktop (terminal, Wi-Fi settings).<br />
                   Log out of the desktop to come back to this screen.</>}
                 onCancel={close} onConfirm={() => void act("desktop")} />
      )}
      {step === "restart" && (
        <Confirm title="Restart the scanner software?" confirmLabel="Restart"
                 text={<>The core, the backend and the detector stop and start again (about 15 s).
                   Inspections and results are kept.</>}
                 onCancel={close} onConfirm={() => void act("restart")} />
      )}
    </>
  );
}

/** Turning off, opening the desktop or restarting: the backend stops shortly; this stays on screen. */
export function Leaving({ snap }: { snap: Snapshot }) {
  const [title, text] = snap.device.powering_off && snap.device.power_action === "reboot"
    ? ["Restarting the device…", "This screen comes back by itself in about a minute."]
    : snap.device.powering_off
    ? ["Turning off…", "Wait until the screen is dark, then remove the power."]
    : snap.device.switching === "desktop"
      ? ["Opening the desktop…", "The scanner software stops. Log out of the desktop to come back here."]
      : ["Restarting the scanner software…", "This screen comes back by itself in about 15 s."];
  return (
    <div className="screen powering-off" role="alert" data-testid="leaving">
      <div className="status-big">{title}</div>
      <p>{text}</p>
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
