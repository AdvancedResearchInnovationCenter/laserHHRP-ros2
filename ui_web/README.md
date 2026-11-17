# ui_web: operator UI frontend

Vite + React + TypeScript, for the Pi's 800 × 480 touchscreen in Chromium kiosk. **Phase 1
scaffold:** it connects to the backend, rebuilds from the authoritative snapshot and drives the full
workflow with plain controls. The designed screens (count circles, SVG results map, history) arrive in
Phase 4.

```bash
npm ci                       # pinned versions from package-lock.json (Node >= 18.17)
npm run dev                  # http://127.0.0.1:5173, proxies /api and /ws to 127.0.0.1:8080
npm run build                # typecheck + dist/ (served by the backend with --static)
npm test                     # SnapshotSync regression tests (no extra dependencies)
```

Start the backend first: `cd ../ui_backend && python3 -m laser_hhrp_ui.server --mock`. In mock mode a
footer bar offers a hold-to-scan button and scripted scenarios.

The frontend is a view. On connect or reconnect it takes the backend snapshot. It applies
`feature_detected` incrementally and refetches `/api/v1/state` on every other event, ignoring envelopes
whose `event_seq` it has already seen. It never derives lifecycle, counts or target filtering itself.
The Pi only needs the built `dist/` (plan: build on a dev PC/CI; no Node on the Pi).
