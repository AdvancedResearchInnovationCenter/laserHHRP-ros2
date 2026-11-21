import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { Feature, Inspection, Scan, Snapshot, UsbTarget } from "../api/types";
import { clockTime, countText, KIND_LABEL, MEASUREMENT_LABEL, measurementReason, measurementValue, MODE_LABEL, statusOf,
  VERDICT } from "../lib/format";
import { FeatureMap } from "../ui/FeatureMap";
import { MeasureCounts } from "../ui/MeasureCounts";
import { Confirm, Modal } from "../ui/Modal";
import { useBoxSize } from "../ui/useBoxSize";
import { ScanActions } from "./Results";
import type { Run } from "./common";

/** Scans of one inspection, newest first: reopen any scan, decide UNDECIDED / failed ones, export. */
export function History({ snap, uuid, run, onBack }: { snap: Snapshot; uuid: string; run: Run; onBack: () => void }) {
  const [insp, setInsp] = useState<Inspection | null>(snap.inspection?.uuid === uuid ? snap.inspection : null);
  const [scans, setScans] = useState<Scan[] | null>(null);
  const [selId, setSelId] = useState<string | null>(null);
  const [detail, setDetail] = useState<Scan | null>(null);
  const [usb, setUsb] = useState(false);
  const [version, setVersion] = useState(0);
  // refetch when the backend's view of this inspection's scans changes, or after our own decisions
  const live = snap.inspection?.uuid === uuid
    ? snap.recent_scans.map((s) => `${s.scan_id}:${s.status}:${s.detected_target_count}`).join(",") : "";

  useEffect(() => {
    let alive = true;
    void api.scans(uuid).then((r) => { if (alive && r.ok) setScans([...r.data].reverse()); });
    return () => { alive = false; };
  }, [uuid, live, version]);
  useEffect(() => {
    if (insp) return;
    void api.inspections().then((r) => r.ok && setInsp(r.data.find((i) => i.uuid === uuid) ?? null));
  }, [uuid, insp]);

  const current = selId ?? scans?.[0]?.scan_id ?? null;
  const summary = scans?.find((s) => s.scan_id === current);
  useEffect(() => {
    if (!current) { setDetail(null); return; }
    let alive = true;
    void api.scan(current).then((r) => { if (alive && r.ok) setDetail(r.data); });
    return () => { alive = false; };
  }, [current, summary?.status, summary?.detected_target_count]);

  const decided: Run = async (p) => {
    const r = await run(p);
    setVersion((v) => v + 1);
    return r;
  };

  return (
    <div className="screen history">
      <div className="bar top">
        <button className="secondary" onClick={onBack}>‹ Back</button>
        <div className="hist-title">
          <strong>{insp?.label ?? "…"}</strong>
          {insp && <span className="muted"> · {MODE_LABEL[insp.mode]}{insp.status === "closed" ? " · closed" : ""}</span>}
        </div>
        <span className="spacer" />
        <a className="button secondary" href={api.csvUrl(uuid)} download>Download CSV</a>
        <button className="secondary" onClick={() => setUsb(true)}>USB export</button>
      </div>
      <div className="hist-body">
        <ul className="hist-list" data-testid="hist-list">
          {scans === null && <li className="muted">Loading…</li>}
          {scans?.length === 0 && <li className="muted">No scans yet</li>}
          {scans?.map((s) => (
            <li key={s.scan_id}>
              <button className={s.scan_id === current ? "hist-row two on" : "hist-row two"} onClick={() => setSelId(s.scan_id)}>
                <span className="hist-name">{s.display_name}</span>
                <span className="hist-time muted">{clockTime(s.started_at, s.clock_quality)}</span>
                <span className={`pill ${statusOf(s).tone}`}>{statusOf(s).label}</span>
                <span className="hist-count">{countText(s.expected_count, s.detected_target_count)}</span>
              </button>
            </li>
          ))}
        </ul>
        <div className="hist-detail">
          {detail ? <ScanSummary scan={detail} run={decided} key={detail.scan_id} />
            : <div className="muted center-text">Select a scan</div>}
        </div>
      </div>
      {usb && insp && <UsbExport insp={insp} run={run} onClose={() => setUsb(false)} />}
    </div>
  );
}

function ScanSummary({ scan, run }: { scan: Scan; run: Run }) {
  const [sel, setSel] = useState<number | null>(null);
  const [ref, w, h] = useBoxSize<HTMLDivElement>(470, 230);
  const features = scan.features ?? [];
  const f = features.find((x) => x.result_seq === sel) ?? null;
  const st = statusOf(scan);
  const decidable = scan.status === "undecided" || scan.status === "failed";
  return (
    <div className="summary">
      <div className="summary-head">
        <strong>{scan.display_name}</strong>
        <span className={`pill ${st.tone}`}>{st.label}</span>
        <span>{countText(scan.expected_count, scan.detected_target_count)}</span>
        <MeasureCounts scan={scan} />
        {scan.partial_target_count > 0 && <span className="pill warn">{scan.partial_target_count} partial</span>}
        <span className="spacer" />
        <span className="muted small">
          {scan.decided_by ? `${scan.decided_by} ${clockTime(scan.decided_at, scan.clock_quality)}` : scan.operator_id}
        </span>
      </div>
      {scan.analysis_error && <div className="small bad-text">{scan.analysis_error}</div>}
      <div className="summary-map" ref={ref}>
        <FeatureMap features={features} width={w} height={h} selected={sel} onSelect={setSel} compact />
      </div>
      <div className="summary-foot">
        {f ? <FeatureLine f={f} /> : decidable
          ? <div className="bar inline"><span className="spacer" /><ScanActions scan={scan} failed={scan.status === "failed"} run={run} /></div>
          : <span className="muted small">{features.length ? "Tap a feature for details." : ""}</span>}
      </div>
    </div>
  );
}

function FeatureLine({ f }: { f: Feature }) {
  const m = f.measurement;
  const verdict = m.status === "measured" && m.verdict ? VERDICT[m.verdict] : null;
  const reason = measurementReason(m);
  return (
    <div className="feature-line">
      <strong>#{f.feature_id} {KIND_LABEL[f.kind] ?? f.kind}</strong>
      <span>x {f.x_mm.toFixed(1)} · y {f.y_mm.toFixed(1)} mm</span>
      {f.partial && <span className="pill warn">Partial</span>}
      {f.is_target && (
        <span className={m.status === "measured" ? "" : "muted"} data-testid="line-measurement">
          {MEASUREMENT_LABEL[m.name] ?? m.name}: {measurementValue(m)}{reason ? ` (${reason})` : ""}
        </span>
      )}
      {verdict && <span className={`pill ${verdict.tone}`}>{verdict.label}</span>}
    </div>
  );
}

function UsbExport({ insp, run, onClose }: { insp: Inspection; run: Run; onClose: () => void }) {
  const [targets, setTargets] = useState<UsbTarget[] | null>(null);
  const [chosen, setChosen] = useState<UsbTarget | null>(null);
  const [done, setDone] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const load = () => {
    setTargets(null);
    void api.usbTargets().then((r) => setTargets(r.ok ? r.data.targets : []));
  };
  useEffect(load, []);

  if (chosen && !done) {
    return (
      <Confirm title="Write to USB drive?" confirmLabel={busy ? "Writing…" : "Write file"}
               text={<>Writes <strong>{insp.label}.csv</strong> to <strong>{chosen.name}</strong>. An existing file is never replaced.</>}
               onCancel={() => setChosen(null)}
               onConfirm={async () => {
                 if (busy) return;
                 setBusy(true);
                 const r = await run(api.exportUsb(insp.uuid, chosen.path));
                 setBusy(false);
                 if (r.ok) setDone(`${r.data.path} (${r.data.rows} rows)`);
                 else setChosen(null);
               }} />
    );
  }
  return (
    <Modal title="USB export" onClose={onClose}>
      {done ? (
        <div className="modal-text">Written: <strong>{done}</strong></div>
      ) : targets === null ? (
        <div className="modal-text muted">Looking for USB drives…</div>
      ) : targets.length === 0 ? (
        <div className="modal-text">No USB drive found. Insert a drive and try again.</div>
      ) : (
        <ul className="usb-list">
          {targets.map((t) => (
            <li key={t.path}>
              <button className="hist-row" disabled={!t.writable} onClick={() => setChosen(t)}>
                <span className="hist-name">{t.name}</span>
                <span className="muted">{t.writable ? `${Math.round(t.free_mb ?? 0)} MB free` : t.reason}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
      <div className="modal-actions">
        {!done && <button className="secondary" onClick={load}>Search again</button>}
        <button className="primary" onClick={onClose}>{done ? "Done" : "Close"}</button>
      </div>
    </Modal>
  );
}

/** Earlier inspections (from Setup): pick one to open its history. */
export function PastInspections({ onOpen, onBack }: { onOpen: (uuid: string) => void; onBack: () => void }) {
  const [list, setList] = useState<Inspection[] | null>(null);
  useEffect(() => { void api.inspections().then((r) => setList(r.ok ? r.data : [])); }, []);
  return (
    <div className="screen history">
      <div className="bar top">
        <button className="secondary" onClick={onBack}>‹ Back</button>
        <div className="hist-title"><strong>Past inspections</strong></div>
      </div>
      <ul className="hist-list full">
        {list === null && <li className="muted">Loading…</li>}
        {list?.length === 0 && <li className="muted">No inspections yet</li>}
        {list?.map((i) => (
          <li key={i.uuid}>
            <button className="hist-row" onClick={() => onOpen(i.uuid)}>
              <span className="hist-name">{i.label}</span>
              <span>{MODE_LABEL[i.mode]}</span>
              <span className="muted">{i.scan_count} scans</span>
              <span className={`pill ${i.status === "open" ? "info" : "neutral"}`}>{i.status}</span>
              <span className="hist-time muted">{clockTime(i.created_at, i.clock_quality)}</span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
