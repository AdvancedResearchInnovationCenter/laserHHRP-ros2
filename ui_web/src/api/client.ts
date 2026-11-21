import type { ApiError, Inspection, Scan, UsbTarget } from "./types";

const P = "/api/v1";

export type Result<T = unknown> = { ok: true; data: T } | { ok: false; error: ApiError };

async function call<T = unknown>(method: string, path: string, body?: unknown): Promise<Result<T>> {
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
    call<Inspection>("POST", "/inspections", { mode, label: label || undefined, request_id: requestId() }),
  endInspection: (uuid: string) => call("POST", `/inspections/${uuid}/end`),
  setExpected: (uuid: string, count: number) => call("PATCH", `/inspections/${uuid}/next-expected-count`, { count }),
  decide: (scanId: string, decision: "accept" | "rescan") => call<Scan>("POST", `/scans/${scanId}/decision`, { decision }),
  failureAction: (scanId: string, action: "retry" | "rescan" | "keep_incomplete") =>
    call<Scan>("POST", `/scans/${scanId}/failure-action`, { action }),
  inspections: () => call<Inspection[]>("GET", "/inspections"),
  scans: (uuid: string) => call<Scan[]>("GET", `/inspections/${uuid}/scans`),
  scan: (scanId: string) => call<Scan>("GET", `/scans/${scanId}`),
  restartDetector: () => call("POST", "/detector/restart"),
  // the backend refuses both during a scan (and power off while an analysis is pending)
  calibrate: () => call<{ started: boolean; message: string }>("POST", "/device/calibrate"),
  powerOff: () => call<{ powering_off: boolean }>("POST", "/device/power-off"),
  // GET download: the browser saves the file; nothing is written on the server
  csvUrl: (uuid: string) => `${P}/inspections/${uuid}/export.csv`,
  usbTargets: () => call<{ targets: UsbTarget[]; roots: string[] }>("GET", "/exports/usb"),
  exportUsb: (uuid: string, mount: string) =>
    call<{ path: string; rows: number }>("POST", `/inspections/${uuid}/exports/usb`, { mount }),
  // mock-only developer controls (404 unless the backend runs with --mock)
  mock: {
    press: () => call("POST", "/dev/mock/press"),
    release: () => call("POST", "/dev/mock/release"),
    scenario: (name: string) => call("POST", "/dev/mock/scenario", { name }),
    fault: (fault: string) => call("POST", "/dev/mock/fault", { fault }),
  },
};
