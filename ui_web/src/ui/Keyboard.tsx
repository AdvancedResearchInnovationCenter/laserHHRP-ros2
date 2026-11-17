// On-screen keyboards for the kiosk (no physical keyboard). A connected keyboard also works.
import { useEffect, useRef } from "react";

const ROWS = ["1234567890", "QWERTYUIOP", "ASDFGHJKL-", "ZXCVBNM_"];
const ALLOWED = /^[A-Za-z0-9_-]$/;

export function Keyboard({ value, onChange, onSubmit, maxLength, submitLabel, canSubmit }: {
  value: string; onChange: (v: string) => void; onSubmit: () => void;
  maxLength: number; submitLabel: string; canSubmit: boolean;
}) {
  usePhysicalKeys((key) => {
    if (key === "Backspace") onChange(value.slice(0, -1));
    else if (key === "Enter") { if (canSubmit) onSubmit(); }
    else if (ALLOWED.test(key) && value.length < maxLength) onChange(value + key.toUpperCase());
  });
  const add = (c: string) => value.length < maxLength && onChange(value + c);
  return (
    <div className="kbd">
      {ROWS.map((row, r) => (
        <div className="kbd-row" key={row}>
          {[...row].map((c) => <button key={c} className="key" onClick={() => add(c)}>{c}</button>)}
          {r === 3 && <button className="key wide" aria-label="Delete" onClick={() => onChange(value.slice(0, -1))}>⌫</button>}
        </div>
      ))}
      <div className="kbd-row">
        <button className="key wide" onClick={() => onChange("")}>Clear</button>
        <button className="key submit" disabled={!canSubmit} onClick={onSubmit}>{submitLabel}</button>
      </div>
    </div>
  );
}

export function Keypad({ value, onChange, onSubmit, canSubmit, submitLabel = "OK" }: {
  value: string; onChange: (v: string) => void; onSubmit: () => void; canSubmit: boolean; submitLabel?: string;
}) {
  usePhysicalKeys((key) => {
    if (key === "Backspace") onChange(value.slice(0, -1));
    else if (key === "Enter") { if (canSubmit) onSubmit(); }
    else if (/^[0-9]$/.test(key) && value.length < 3) onChange(value + key);
  });
  const add = (c: string) => value.length < 3 && onChange(value === "0" ? c : value + c);
  return (
    <div className="keypad">
      {"123456789".split("").map((c) => <button key={c} className="key" onClick={() => add(c)}>{c}</button>)}
      <button className="key" aria-label="Delete" onClick={() => onChange(value.slice(0, -1))}>⌫</button>
      <button className="key" onClick={() => add("0")}>0</button>
      <button className="key submit" disabled={!canSubmit} onClick={onSubmit}>{submitLabel}</button>
    </div>
  );
}

function usePhysicalKeys(handler: (key: string) => void) {
  const ref = useRef(handler);
  ref.current = handler;
  useEffect(() => {
    const on = (e: KeyboardEvent) => {
      if (e.ctrlKey || e.altKey || e.metaKey) return;
      ref.current(e.key);
    };
    window.addEventListener("keydown", on);
    return () => window.removeEventListener("keydown", on);
  }, []);
}
