import { useState } from "react";
import { api } from "../api/client";
import { Keyboard } from "../ui/Keyboard";
import type { Run } from "./common";

const VALID = /^[A-Za-z0-9_-]{1,32}$/;    // hint only; the backend validates

export function Identify({ run }: { run: Run }) {
  const [id, setId] = useState("");
  return (
    <div className="screen identify">
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
