import { useEffect, useState } from "react";
import type { Envelope, Feature, Snapshot } from "../api/types";
import { SnapshotSync } from "./snapshotSync";

// Live backend state. The backend snapshot is authoritative: on connect / reconnect we take a fresh
// snapshot; afterwards feature_detected is applied incrementally and every other event triggers a
// snapshot refresh (SnapshotSync: never stale after the last event, duplicates ignored by event_seq).
export function useBackend() {
  const [snap, setSnap] = useState<Snapshot | null>(null);
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    let ws: WebSocket | null = null;
    let closed = false;
    let retry = 500;
    let timer: number | undefined;
    const sync = new SnapshotSync(async () => {
      const r = await fetch("/api/v1/state");
      return r.ok ? ((await r.json()) as Snapshot) : null;
    }, setSnap);

    const connect = () => {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${proto}://${location.host}/ws`);
      ws.onopen = () => { setConnected(true); retry = 500; };
      ws.onclose = () => {
        setConnected(false);
        if (!closed) timer = window.setTimeout(connect, (retry = Math.min(retry * 2, 5000)));
      };
      ws.onmessage = (m) => {
        const env = JSON.parse(m.data) as Envelope;
        if (env.type === "snapshot") {
          sync.snapshot(env.payload as Snapshot, env.event_seq);
          return;
        }
        if (!sync.accept(env.event_seq)) return;          // duplicate / already in the snapshot
        if (env.type === "feature_detected" && !(env.payload as { preview?: boolean }).preview) {
          const f = env.payload as Feature & { detected_target_count: number; non_target_count: number };
          setSnap((s) => {
            const cur = s?.current_scan;
            if (!s || !cur || cur.scan_id !== env.scan_id) return s;
            const features = cur.features ?? [];
            if (features.some((x) => x.result_seq === f.result_seq)) return s;
            return { ...s, current_scan: { ...cur, features: [...features, f],
              detected_target_count: f.detected_target_count, non_target_count: f.non_target_count } };
          });
          return;
        }
        void sync.refresh();                              // everything else, including preview detections
      };
    };
    connect();
    return () => { closed = true; sync.stop(); window.clearTimeout(timer); ws?.close(); };
  }, []);

  return { snap, connected };
}
