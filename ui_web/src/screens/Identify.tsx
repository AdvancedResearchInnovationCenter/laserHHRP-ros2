import { useState } from "react";
import { api } from "../api/client";
import { Keyboard } from "../ui/Keyboard";
import type { Run } from "./common";

const VALID = /^[A-Za-z0-9_-]{1,32}$/;    // hint only; the backend validates

/** `previous`: the operator who just tapped Change operator, offered as a way back until anyone logs in. */
export function Identify({ run, previous }: { run: Run; previous?: string | null }) {
  const [id, setId] = useState("");
  return (
    <div className="screen identify">
      {previous && (
        <button className="secondary id-back" onClick={() => void run(api.setOperator(previous))}>
          ‹ Back to {previous}
        </button>
      )}
      <div className="id-top">
        <div>
          <h1>Operator identification</h1>
          <p className="muted">Enter your operator ID once for this work session.</p>
        </div>
        <div className={id ? "id-field" : "id-field placeholder"} data-testid="id-field">
          {id || "e.g. ENG-1042"}
        </div>
      </div>
      <Keyboard value={id} onChange={setId} maxLength={32} submitLabel="Continue" canSubmit={VALID.test(id)}
                onSubmit={() => void run(api.setOperator(id))} />
    </div>
  );
}
