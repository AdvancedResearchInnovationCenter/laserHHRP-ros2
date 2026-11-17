# Operator UI v0.1: handover

For whoever continues this work (developer or coding agent). Read with [plan.md](plan.md), the execution
contract. This file says **where things stand, how to run and verify them, and what is left**.

State as of 2026-11-17, branch `ui` @ `140ca9a` (pushed, clean). `dev` and `main` were never touched.

---

## 1. Status at a glance

| Phase (plan §10) | State | Commit | Verification |
| --- | --- | --- | --- |
| 0 Baseline | Done on the laptop; **bag tests and the Pi run still open** | `e2caef1` | [docs/operator_ui/BASELINE.md](docs/operator_ui/BASELINE.md) |
| 1 Backend, state, SQLite, mock, web scaffold | Done | `3d7f52e` | [VERIFY_PHASE1.md](docs/operator_ui/VERIFY_PHASE1.md) |
| External code review of `3d7f52e` (5 findings) | All fixed with regression tests | `e6a2c81` | VERIFY_PHASE1.md, last section |
| 2 Live ROS bridge, one-detector supervisor | Done (its preview mode was superseded by Phase 3) | `018bb06` | [VERIFY_PHASE2.md](docs/operator_ui/VERIFY_PHASE2.md) |
| 3 Scan identity, finalization, raw recording link | Done | `140ca9a` | [VERIFY_PHASE3.md](docs/operator_ui/VERIFY_PHASE3.md) |
| 4 Operator screens (800 × 480) | **Not started**: the frontend is a plain scaffold | | |
| 5 Pi 5 deployment and verification | **Not started** | | |

Everything was built and verified on an **x86 laptop, Ubuntu 22.04, ROS 2 Humble**. The target is the
**Raspberry Pi 5, Ubuntu 24.04, ROS 2 Jazzy**. Nothing has run on the Pi, on real hardware, or on real bags yet.

What works today, end to end in simulation: identify operator → create a CSK or Rivet inspection (the backend
starts exactly one detector) → Ready → physical scan (switch) → every scan recorded under the controller's UUID
→ the detector's final pass and terminal acknowledgement → Results (count, mismatch warning, *Not measured*) →
Accept / Rescan → history; failures, restarts, crashes and rapid presses handled explicitly.

---

## 2. Repository map (what is new)

```
plan.md                         execution contract (author force-pushed it twice early on; now normal pushes)
handover.md                     this file
docs/operator_ui/               BASELINE.md, VERIFY_PHASE1/2/3.md: evidence and how to re-check
laserHHRP/msg/                  6 NEW messages (additive; originals unchanged)
laserHHRP/src/                  scan_controller_node, synchronizer_node, recorder_node: identity additions (Phase 3)
hole_detector/, rivet_detector/ node.py + results.py: opt-in tagged mode (Phase 3)
ui_backend/                     ROS 2 ament_python package laser_hhrp_ui (the backend)
  laser_hhrp_ui/
    state.py                    Engine: authoritative lifecycle, completion barrier, commands (the core)
    repository.py               SQLite: migrations 1-2, WAL, FK, transactions
    models.py                   validation, detector-result -> feature contract, "not measured"
    events.py                   immutable input events (ROS-shaped, no rclpy)
    ros_bridge.py               read-only rclpy bridge (tagged topics) -> events
    detector_supervisor.py      one allowlisted detector process, readiness from the ROS graph
    raw_recording.py            frames.bin verification
    ingress.py, hub.py, api.py  thread hand-off, WebSocket fan-out, FastAPI HTTP/WS
    mock_ros.py                 mock source (no ROS): late detections, faults, scenarios
    server.py                   entry point (`ros2 run laser_hhrp_ui server`)
  test/                         103 tests
ui_web/                         Vite + React + TS scaffold (Phase 4 replaces the screens); npm test (5)
tools/ui_baseline/              Phase 0 runner and probes
tools/ui_verify/                mutation check, Phase 3 live checks, legacy bag adapter
```

---

## 3. How to run

### Build (memory!)

The dev laptop has **no swap**; a full-parallel C++ build of `laser_hhrp` ran it out of memory once (the kernel
killed the browser). Always limit it:

```bash
MAKEFLAGS=-j2 colcon build --parallel-workers 1        # laser_hhrp: ~4-5 min at 2 jobs after a message change
```

### Live (simulation or hardware)

```bash
# 1. core WITHOUT detectors and RViz (the backend starts the selected detector itself)
ros2 launch laser_hhrp hhrp.launch.py holes:=false ui:=false        # + simulate:=true scan_switch:=false without hardware
# 2. backend + built frontend
cd ui_web && npm ci && npm run build && cd ..
ros2 run laser_hhrp_ui server --static ui_web/dist                   # http://127.0.0.1:8080
```

Without the GPIO switch, `ros2 service call /scan/start std_srvs/srv/Trigger` and `/scan/stop` do what the switch
does; `/calibration/start` calibrates. The simulator detects nothing (its rivet is 20 µm proud): scans complete
with zero features.

### Without ROS

```bash
cd ui_backend && python3 -m laser_hhrp_ui.server --mock --static ../ui_web/dist --db /tmp/ui.db
```

The mock adds a hold-to-scan button and scenarios (normal, short, fast_next, zero, reordered, missing_result,
error_stop, detector_failure). Its `/api/v1/dev/mock/*` routes exist only with `--mock`.

### Data

| What | Where (default) |
| --- | --- |
| Inspection database (canonical history) | `$XDG_DATA_HOME/laser_hhrp/ui/inspections.db` (`~/.local/share/...`) |
| Detector output | `<data root>/detector-output/<inspection UUID>/<scan UUID>/summary.csv + PCDs` |
| Detector logs, pidfile | `<data root>/logs/` |
| Raw recordings (legacy recorder) | `~/laser_hhrp/scans/<time>[-n]/frames.bin, session.json, results.json` |

`results.json` lacks late detections **by decision** (plan §8): the database is the complete record.

---

## 4. How to verify (all scripts isolate themselves)

Every live script uses its own ROS domain, a loopback-only CycloneDDS config
(`tools/ui_baseline/cyclonedds_loopback.xml`) and a scratch `HOME`, so it never touches the real ROS graph,
`~/laser_hhrp` or the scanner network. `HHRP_WS=<dir with build/ install/>` reuses a built workspace.

| What | Command | Last result (laptop) |
| --- | --- | --- |
| Backend unit tests | `cd ui_backend && python3 -m pytest -q test/` | 103 passed, ~15 s |
| Frontend tests | `cd ui_web && npm test` | 5/5 |
| Do the tests catch real bugs? | `python3 tools/ui_verify/mutation_check.py` | 24/24 planted bugs caught, ~5 min |
| Phase 0 baseline / legacy regression | `tools/ui_baseline/run_baseline.sh` | INCOMPLETE (bags missing), 0 failures. Exit 0 pass / 1 fail / 3 not all ran |
| Identity layer (controller, synchronizer, recorder) | `HHRP_WS=… tools/ui_verify/phase3_identity_check.sh` | 19/19 |
| Tagged detector = legacy output | `HHRP_WS=… [CHECK_DETECTOR=rivet_detector] tools/ui_verify/phase3_detector_check.sh` | 16/16 each |
| End to end (core + backend + detector) | `HHRP_WS=… tools/ui_verify/phase3_ros_check.sh` | 28/28, ~45 s |
| Old bags through the tagged path | `tools/ui_verify/legacy_bag_adapter.py <bag>`; self-test `phase3_bag_adapter_check.sh` | PASS |

When a check fails, read its `check.log` / `backend.log` / `detector.log` in the output folder it prints.

---

## 5. Architecture and contracts

### Runtime

```
GPIO switch -> scan_switch_node -> scan/start|stop (services) -> scan_controller
scan_controller --scan/status (drives acquisition, unchanged)--> synchronizer, point_cloud, recorder
scan_controller --scan/session_state (UUID + exact status stamp)--> synchronizer, backend
synchronizer --scan/frame (unchanged)--> recorder, point_cloud
synchronizer --scan/frame_tagged + scan/stream_closed--> detector (tagged mode)
detector --inspection/result_tagged + inspection/detection_finalized--> backend
recorder --scan/recording_session--> backend
backend: ROS thread -> bounded ingress queue -> Engine (single thread, asyncio) -> SQLite + WebSocket/HTTP -> browser
```

**Rules that must hold** (each is tested; see the mutation list in `tools/ui_verify/mutation_check.py`):
- `scan/status` alone starts, stops and flushes acquisition. Identity is metadata, never a second state machine.
- Identity is matched by the **exact** `scan/status` stamp, never by time or "latest UUID".
- A scan is **complete** only if: acquisition ended with a **confirmed** stop; stream close and detector
  finalization arrived with equal frame counts; every declared `result_seq` is committed to SQLite; the
  backend's target count equals the detector's; no stream error; stream stop state = controller stop state.
- Expected-count equality never completes or stops anything. Measurements are always *Not measured*;
  `passed=True` from the detectors is ignored; `partial` comes only from `detail.partial`.
- Timeouts (warn 10 s / fail 120 s after release), detector crash, queue overflow, missing frames or results,
  hardware ERROR stop, an inferred (unconfirmed) stop, a restart mid-analysis → **failed/interrupted**, never
  success.
- Rescans never overwrite; an earlier scan left in Results when a new one starts stays UNDECIDED.
- No web endpoint starts or stops a scan (mock routes only with `--mock`).

### Messages (`laserHHRP/msg`, additive)

`ScanSessionState` (`scan/session_state`, reliable + transient-local 50), `TaggedScanFrame`
(`scan/frame_tagged`), `ScanStreamClosed` (`scan/stream_closed`), `TaggedInspectionResult`
(`inspection/result_tagged`), `DetectionFinalized` (`inspection/detection_finalized`), `ScanRecordingSession`
(`scan/recording_session`). Field docs are in the `.msg` files. The backend subscribes to identity topics
**volatile**, so old transitions replayed after a backend restart cannot create scans.

### API v1

Documented in [ui_backend/README.md](ui_backend/README.md): `/api/v1/state`, `/health`, operator,
inspections (create, end, next-expected-count), scans (detail, decision, failure-action),
`/detector/restart`, `WS /ws` (snapshot, then versioned envelopes with `event_seq`). Errors:
`{"error": {code, message, field}}`, 400 / 404 / 409.

### Database

SQLite, `PRAGMA user_version` migrations (1: schema, 2: raw recording link). One open inspection enforced by a
partial unique index. Features keyed by (scan, mode, feature id) and (scan, result_seq). `audit_events` records
decisions, alerts, failures, recoveries.

---

## 6. Decisions and deviations from plan.md

Documented in `ui_backend/README.md` (Deviations) and the VERIFY files; the important ones:

1. `transition_seq` increments on **every** controller transition (plan: per-scan `scan_seq`), so a missed
   stop is a detectable gap.
2. The first inspection has **no** expected count until the operator enters one (no invented default); later
   ones prefill the last value.
3. A stop inferred from the next scan's start is `UNCONFIRMED` and fails ("stop not confirmed") unless the
   controller's own stop arrives late (accepted even out of transition order). From the external review.
4. A hardware ERROR stop fails the analysis but keeps the features (Rescan / Keep Incomplete).
5. Scans started without operator, open inspection or ready detector are stored `unassigned` with the reason.
6. Detector tagged mode is **opt-in** (`tagged:=true`); legacy behaviour and developer flows are unchanged.
7. Retry Analysis: the raw recording is verified (`replay_eligible`), but replay into the detector is **not
   implemented**; retry is refused with the reason.
8. The recorder change goes slightly beyond "metadata only": it now keeps the session open for the scan's last
   flushed frames, because the checks proved it lost them (181 of 182). Justified under plan §5 "narrowly
   justified fixes"; documented in BASELINE.md and laserHHRP/README.md.
9. Request bodies are validated in plain Python, not pydantic, so the backend runs on FastAPI with pydantic 1
   (Ubuntu 24.04 apt) or 2 (laptop pip). **Untested on pydantic 1.**

---

## 7. Known issues, risks and traps

**Open technical items**
- **Frame loss under load.** A detector that falls behind loses frames (reliable but bounded queues). At 3× the
  laser rate the rivet detector lost 211 of 1769 frames: legacy mode then reported different rivets silently;
  tagged mode fails the scan "missing frames". Nothing was lost at 120 frames/s on the laptop. **Measure on
  the Pi** (Phase 5) under both detectors; options if it happens: larger subscription depth, lighter frame
  ingestion in the detector (FrameCloud.add converts Python lists), or `viz_surface` off (already default).
- **Tagged frames double the frame bandwidth** (plan §5 asks to benchmark). The laptop showed no rate loss
  (119/120 frames/s); not yet measured on the Pi.
- **Retry replay** not implemented (see §6.7).
- A tracker reset inside one scan (scan direction turning back) restarts hole ids; in tagged mode that makes the
  scan fail as a conflict (honest, but possible on odd scans).
- `ui_web/src/api/types.ts` does not model the new `raw` scan fields yet (Phase 4).
- The Phase 2 preview mode still exists as source `ros-preview` in the engine (tests use it), but no CLI flag
  selects it.
- Not implemented from the plan: Developer Mode / PIN, CSV and USB export, manual clock verification
  procedure, operator launch file, kiosk.

**Environment traps (cost real time)**
- The dev laptop's `CYCLONEDDS_URI` binds the scanner-network interface; with that link down every node fails
  with `failed to create domain`. Use `tools/ui_baseline/cyclonedds_loopback.xml` for local tests.
- Scripts that move `HOME` hide pip `--user` packages (FastAPI on the laptop): export
  `PYTHONUSERBASE=$HOME/.local` **before** changing `HOME` (the scripts do).
- Background jobs of a non-interactive bash script **ignore SIGINT**: stop them with SIGTERM.
- The `ros2 run` wrapper **does not forward SIGTERM**: stopping it orphans the server and its detector. A
  service (Phase 5) must start `install/laser_hhrp_ui/lib/laser_hhrp_ui/server` directly. The server itself
  shuts down cleanly on SIGTERM (stops the detector, exit 0).
- A SIGKILLed node stays visible in the ROS graph for ~10 s (DDS lease); the supervisor waits for its own
  crashed node before restarting.
- `pkill -f <pattern>` can match the shell running it; prefer PIDs or patterns that cannot match the command.
- Old bags in `~/bags/hhrp_202610*` use the previous `aric_laserhhrp_msgs` types and cannot be replayed. The
  detector regression bags (OneDrive_2_10-28-2026, panel*, flap1_05112026) are not on the laptop.
- Mock- and timing-based tests were tuned on a 12-core laptop; the Pi may need slower mock speeds.

**Process notes**
- Commits went straight to `ui` with normal pushes, one per phase (+ the review fix). Plan §12: never
  force-push, never touch `dev` / `main`.
- External review covered Phase 1 (`3d7f52e`); Phases 2 and 3 have not been reviewed by anyone else.

---

## 8. Next steps

### Finish Phase 0 (small)
On a machine with the detector bags (ROS container or Pi): `tools/ui_baseline/run_baseline.sh`; record the bag
results in BASELINE.md. Then `tools/ui_verify/legacy_bag_adapter.py <bag>` with a tagged detector running.

### Phase 4: operator screens (plan §9, §10)
Replace `ui_web/src/App.tsx` (scaffold) with the five designed screens at **800 × 480**:
Identification (on-screen keyboard), Setup, Ready/Scanning/Finishing (count circles, >40 → 40 + remainder),
Results (SVG map of local XY: x = position_m[0]·1000 rightward, y = position_m[1]·1000 downward, uniform scale,
+Y indicator, tap targets larger than the circles; selected feature details; Accept / Rescan), History
(statuses incl. UNDECIDED and Failed/Incomplete, later decisions). Plus: GET CSV download and POST USB export
(plan §7, §8), `types.ts` for `raw`, UI unit/component tests, and screenshot QA at 800 × 480 (a browser driver
such as Playwright: Firefox's headless screenshot captures before the WebSocket delivers state). Keep the
frontend a view: never derive lifecycle, counts or target filtering; keep `SnapshotSync`.

### Phase 5: Pi deployment (plan §10)
- Operator launch (additive; do not change the meaning of `hhrp.launch.py ui`): core with `holes:=false
  ui:=false` plus the server started **directly** (SIGTERM).
- Offline install: apt `python3-fastapi`, `python3-uvicorn` (check pydantic 1 compatibility), rosdep; the
  frontend built on a dev PC and shipped as `dist/` (no Node on the Pi).
- Chromium kiosk at 800 × 480; manual start first.
- Measure on the Pi: frame loss and CPU with tagged frames + each detector at real scan speed, mock test
  timing, startup readiness (detector start timeout 45 s).
- Run every check in §4 on the Pi; record results in a VERIFY_PHASE5.md.
- Hardware: GPIO press/release, rapid presses, calibration, ERROR stops (laser unplugged), disk full, backend
  and detector crashes, browser reload, clock unverified (no RTC).

### Optional
- Retry Analysis by replaying a verified `frames.bin` as tagged frames into the detector (the bag adapter shows
  the mechanism); the engine's `can_replay` switch gates it.
- An independent review of Phases 2 and 3.
