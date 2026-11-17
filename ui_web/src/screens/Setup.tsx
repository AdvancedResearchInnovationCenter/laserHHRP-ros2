import { useState } from "react";
import { api } from "../api/client";
import type { Mode, Snapshot } from "../api/types";
import { MODE_DETAIL, MODE_LABEL } from "../lib/format";
import { Keyboard } from "../ui/Keyboard";
import { Modal } from "../ui/Modal";
import type { Run } from "./common";

const VALID = /^[A-Za-z0-9_-]{1,64}$/;
const MODES: Mode[] = ["csk_depth", "rivet_flushness"];
const MEASURE_NOTE: Record<Mode, string> = {
  csk_depth: "Countersink depth: not measured in this version",
  rivet_flushness: "Flushness: not measured in this version",
};

export function Setup({ snap, run, openPast }: { snap: Snapshot; run: Run; openPast: () => void }) {
  const [mode, setMode] = useState<Mode | null>(null);
  const [label, setLabel] = useState("");
  const [draft, setDraft] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const start = async () => {
    if (!mode) return;
    setBusy(true);
    await run(api.createInspection(mode, label));
    setBusy(false);
  };

  return (
    <div className="screen setup">
      <div className="setup-id">
        <span className="muted">Inspection ID</span>
        <button className="field" onClick={() => setDraft(label)} data-testid="label-field">
          {label || <span className="muted">Generated automatically</span>}
        </button>
        {label && <button className="ghost" onClick={() => setLabel("")}>Use generated</button>}
      </div>
      <div className="mode-cards">
        {MODES.map((m) => (
          <button key={m} className={mode === m ? "mode-card on" : "mode-card"} aria-pressed={mode === m}
                  onClick={() => setMode(m)}>
            <span className="mode-title">{MODE_LABEL[m]}</span>
            <span className="mode-detail">{MODE_DETAIL[m]}</span>
            <span className="mode-note">{MEASURE_NOTE[m]}</span>
          </button>
        ))}
      </div>
      <p className="muted small">The mode is fixed for the whole inspection. A different mode needs a new inspection.</p>
      <div className="bar">
        <button className="secondary" onClick={() => void run(api.logout())}>
          Change operator ({snap.operator?.operator_id})
        </button>
        <button className="secondary" onClick={openPast}>Past inspections</button>
        <span className="spacer" />
        <button className="primary big" disabled={!mode || busy} onClick={start}>Start inspection</button>
      </div>
      {draft !== null && (
        <Modal title="Inspection ID" wide onClose={() => setDraft(null)}>
          <div className={draft ? "id-field" : "id-field placeholder"}>{draft || "e.g. WING-L-07"}</div>
          <Keyboard value={draft} onChange={setDraft} maxLength={64} submitLabel="Use this ID"
                    canSubmit={VALID.test(draft)} onSubmit={() => { setLabel(draft); setDraft(null); }} />
        </Modal>
      )}
    </div>
  );
}
