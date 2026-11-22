import { useState } from "react";
import type { Screen, Snapshot } from "../api/types";
import { MODE_LABEL, type Tone } from "../lib/format";
import { applyTheme, currentTheme, saveTheme, type Theme } from "../lib/theme";
import type { Run } from "../screens/common";
import { MenuButton, offerMenu } from "./Device";
import { Modal } from "./Modal";

export const SCREEN_PILL: Record<Screen, { label: string; tone: Tone }> = {
  identify: { label: "SIGN IN", tone: "neutral" },
  setup: { label: "SETUP", tone: "neutral" },
  ready: { label: "READY", tone: "ok" },
  not_ready: { label: "NOT READY", tone: "bad" },
  scanning: { label: "SCANNING", tone: "info" },
  finishing_analysis: { label: "ANALYZING", tone: "warn" },
  results: { label: "RESULTS", tone: "info" },
  analysis_failed: { label: "ANALYSIS FAILED", tone: "bad" },
};

type AlertItem = { text: string; tone: "bad" | "warn" };

/** Sticky backend alerts, then current warnings (time quality has its own header badge). */
export function alertItems(snap: Snapshot): AlertItem[] {
  return [
    ...snap.alerts.map((a) => ({ text: a.message, tone: "bad" as const })),
    ...snap.warnings.filter((w) => w !== "Time not verified").map((w) => ({ text: w, tone: "warn" as const })),
  ];
}

export function Header({ snap, connected, run }: { snap: Snapshot; connected: boolean; run?: Run }) {
  const [open, setOpen] = useState(false);
  const items = alertItems(snap);
  const pill = snap.device.powering_off
    ? { label: snap.device.power_action === "reboot" ? "RESTARTING" : "TURNING OFF", tone: "warn" as Tone }
    : snap.device.switching ? { label: "SERVICE", tone: "warn" as Tone }
    : connected ? SCREEN_PILL[snap.screen] : { label: "OFFLINE", tone: "bad" as Tone };
  return (
    <header className="hdr">
      <span className={`state-pill ${pill.tone}`} data-testid="state-pill">
        {snap.screen === "scanning" && connected && <span className="rec" />}{pill.label}
      </span>
      {snap.inspection && (
        <span className="hdr-insp">
          <strong>{snap.inspection.label}</strong>
          <span className="muted"> · {MODE_LABEL[snap.inspection.mode]}</span>
        </span>
      )}
      <span className="spacer" />
      {snap.bag?.state === "recording" && <span className="pill bad hdr-bag" data-testid="bag-rec" title={snap.bag.name ?? ""}>● REC</span>}
      {snap.clock_quality === "unverified" && <span className="pill warn">Time not verified</span>}
      {snap.operator && <span className="hdr-op">{snap.operator.operator_id}</span>}
      {items.length > 0 && (
        <button className={`hdr-alerts ${items[0].tone}`} aria-label="Alerts and warnings" onClick={() => setOpen(true)}>
          ⚠ {items.length}
        </button>
      )}
      {run && offerMenu(snap, connected) && <MenuButton snap={snap} run={run} />}
      <span className={`conn ${connected ? "on" : "off"}`} title={connected ? "Connected" : "Disconnected"} />
      <ThemeToggle />
      {open && (
        <Modal title="Alerts and warnings" onClose={() => setOpen(false)}>
          <ul className="alert-list">
            {items.map((i, k) => <li key={k} className={i.tone}>{i.text}</li>)}
          </ul>
        </Modal>
      )}
    </header>
  );
}

/** Small, faint light / dark switch; the choice is remembered in this browser. */
function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>(currentTheme);
  const next: Theme = theme === "dark" ? "light" : "dark";
  return (
    <button className="hdr-theme" aria-label={next === "light" ? "Light mode" : "Dark mode"} data-testid="theme-toggle"
            onClick={() => { applyTheme(next); saveTheme(next); setTheme(next); }}>
      ◐
    </button>
  );
}

/** The first alert as a line under the header; the header's ⚠ button lists them all. */
export function AlertStrip({ snap }: { snap: Snapshot }) {
  const items = alertItems(snap);
  if (!items.length) return null;
  return (
    <div className={`strip ${items[0].tone}`} data-testid="alert-strip">
      <span className="strip-text">{items[0].text}</span>
      {items.length > 1 && <span className="strip-more">+{items.length - 1} more</span>}
    </div>
  );
}
