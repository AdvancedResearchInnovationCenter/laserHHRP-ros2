# ui_web: operator UI frontend

Vite + React + TypeScript for the Pi's **800 × 480** touchscreen in a Chromium kiosk (plan §9). Five
screens, chosen by the backend's `snapshot.screen`:

| Screen | Shows |
| --- | --- |
| Identification | on-screen keyboard (a physical keyboard also works); once per work session |
| Setup | inspection ID (generated, or typed on the keyboard), CSK Depth or Rivet Flushness (fixed per inspection), change operator, past inspections |
| Ready / Not ready | READY + "hold the switch", or every backend reason; expected count (keypad, − / +, remembered); restart detector after a detector error; history; end inspection |
| Scanning / Finishing analysis | distance, detected / expected, count circles filling as results arrive (also after release; > 40 → 40 + remainder; amber = partial, dashed = beyond expected), live map |
| Results / Analysis failed | SVG map of the scan's local XY (tap a feature for details), count and mismatch warning, GO / NO-GO / not-measured counts (targets coloured on the map; a feature shows its CSK depth or signed flushness, verdict and GO range, or why it was not measured); Accept / Rescan, or after a failure Retry (only if it can run) / Rescan / Keep incomplete (confirmed) |
| History | the inspection's scans newest first with status (incl. Undecided, failed, incomplete); reopen any scan; decide undecided / failed ones; CSV download and USB export |

Header: state pill, inspection, *Time not verified*, operator, a ⚠ button listing alerts and warnings, the
connection dot. If the backend connection drops, the screen is covered by a "not current" notice.

**Map contract** ([src/lib/map.ts](src/lib/map.ts)): x = `position_m[0]·1000` rightward, y =
`position_m[1]·1000` downward, one uniform scale, +X / +Y marker and scale bar, the label states the view's real
top-left in scan-local mm (not a part origin). Taps select the nearest feature within 24 px, larger than the
drawn circles.

The frontend is a view. It never derives lifecycle, counts or target filtering: those come from the backend
(`is_target`, `detected_target_count`, `count_mismatch`, `status`, `retry_available`). On connect / reconnect it
takes a snapshot; it applies `feature_detected` incrementally and refetches the state for every other event
([SnapshotSync](src/state/snapshotSync.ts)).

```bash
npm ci                       # pinned versions from package-lock.json (Node >= 18.17)
npm run dev                  # http://127.0.0.1:5173, proxies /api and /ws to 127.0.0.1:8080
npm run build                # typecheck + dist/ (served by the backend with --static)
npm test                     # 35 tests: SnapshotSync, map / circles / wording, screens rendered from snapshots
```

Start the backend first: `cd ../ui_backend && python3 -m laser_hhrp_ui.server --mock`. With the mock
source a small **DEV** tab (bottom left) offers hold-to-scan and scenarios; `?kiosk` in the URL hides it.

Tests use no extra dependencies: `tsc` compiles them to CommonJS and Node runs them; screens are rendered with
`react-dom/server`. Real-browser checks (taps, layout at 800 × 480, screenshots) are in
`tools/ui_verify/phase4_screens.sh` (mock) and `phase4_live.sh` (live ROS); planted-bug check:
`tools/ui_verify/web_mutation_check.py`. See [docs/operator_ui/VERIFY_PHASE4.md](../docs/operator_ui/VERIFY_PHASE4.md).

Offline: system fonts only, no CDN. The Pi needs only the built `dist/` (no Node on the Pi).
