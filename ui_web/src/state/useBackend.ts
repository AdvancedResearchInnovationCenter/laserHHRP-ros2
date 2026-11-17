import { useEffect, useRef, useState } from "react";
import type { Envelope, Feature, Snapshot } from "../api/types";

// Live backend state. The backend snapshot is authoritative: on connect / reconnect we take a fresh
// snapshot; afterwards feature_detected is applied incrementally and every other event triggers a
// snapshot refetch. event_seq makes repeated delivery harmless.
export function useBackend() {
  const [snap, setSnap] = useState<Snapshot | null>(null);
  const [connected, setConnected] = useState(false);
  const seq = useRef(0);
  const refetching = useRef(false);

  useEffect(() => {
    let ws: WebSocket | null = null;
    let closed = false;
    let retry = 500;
    let timer: number | undefined;

    const refetch = async () => {
      if (refetching.current) return;
      refetching.current = true;
      try {
        const r = await fetch("/api/v1/state");
        if (r.ok) {
          const s = (await r.json()) as Snapshot;
          if ((s.event_seq ?? 0) >= seq.current) {
            seq.current = s.event_seq ?? seq.current;
            setSnap(s);
          }
        }
      } finally {
        refetching.current = false;
      }
    };

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
          seq.current = env.event_seq;
          setSnap(env.payload as Snapshot);
          return;
        }
        if (env.event_seq <= seq.current) return;         // duplicate / already in the snapshot
        seq.current = env.event_seq;
        if (env.type === "feature_detected") {
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
        void refetch();
      };
    };
    connect();
    return () => { closed = true; window.clearTimeout(timer); ws?.close(); };
  }, []);

  return { snap, connected };
}
