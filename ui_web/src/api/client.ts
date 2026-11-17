import type { ApiError } from "./types";

const P = "/api/v1";

export type Result<T> = { ok: true; data: T } | { ok: false; error: ApiError };

async function call<T>(method: string, path: string, body?: unknown): Promise<Result<T>> {
  try {
    const r = await fetch(P + path, {
      method,
      headers: body === undefined ? undefined : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const json = await r.json().catch(() => ({}));
    if (r.ok) return { ok: true, data: json as T };
    return { ok: false, error: (json as { error?: ApiError }).error ?? { code: String(r.status), message: r.statusText, field: null } };
  } catch {
    return { ok: false, error: { code: "network", message: "Backend not reachable", field: null } };
  }
}

let requestCounter = 0;
const requestId = () => `${Date.now().toString(36)}-${(requestCounter++).toString(36)}`;

export const api = {
  state: () => call("GET", "/state"),
  setOperator: (operator_id: string) => call("POST", "/operator", { operator_id }),
  logout: () => call("POST", "/operator/logout"),
  // request_id makes a double tap create one inspection
  createInspection: (mode: string, label?: string) =>
    call("POST", "/inspections", { mode, label: label || undefined, request_id: requestId() }),
  endInspection: (uuid: string) => call("POST", `/inspections/${uuid}/end`),
  setExpected: (uuid: string, count: number) => call("PATCH", `/inspections/${uuid}/next-expected-count`, { count }),
  decide: (scanId: string, decision: "accept" | "rescan") => call("POST", `/scans/${scanId}/decision`, { decision }),
  failureAction: (scanId: string, action: "retry" | "rescan" | "keep_incomplete") =>
    call("POST", `/scans/${scanId}/failure-action`, { action }),
  scans: (uuid: string) => call("GET", `/inspections/${uuid}/scans`),
  // mock-only developer controls (404 unless the backend runs with --mock)
  mock: {
    press: () => call("POST", "/dev/mock/press"),
    release: () => call("POST", "/dev/mock/release"),
    scenario: (name: string) => call("POST", "/dev/mock/scenario", { name }),
    fault: (fault: string) => call("POST", "/dev/mock/fault", { fault }),
  },
};
