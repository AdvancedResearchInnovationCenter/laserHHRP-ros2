# Laser Roller Handheld: Operator UI Implementation Plan (v0.1)

**Status:** Approved design, implementation pending  
**Branch:** `ui` only. Do not commit to, merge into, or modify `dev` or `main` without explicit instruction.  
**Baseline:** `ui` at `87c5641b3b44633b412cb92c15e129e30f090172` (17 Nov 2026).  
**Target:** Raspberry Pi 5, 5-inch 800 x 480 landscape touchscreen, ROS 2 Jazzy / Ubuntu 24.04; offline operation.  
**Audience:** Coding agents. This file is the execution contract. Preserve the working scan/detection pipeline.

## 1. Goal and approved decisions

Deliver a local, touch-friendly operator web application for inspection setup, live scan feedback, results and history. Preserve ROS 2 as the source of truth for hardware and metrology. A browser crash/reload must never start, stop, lose or corrupt a scan.

**Agreed:**
- User Mode is a React + TypeScript frontend, SVG visualization, Chromium kiosk; RViz2 remains developer-only.
- Python ROS 2 UI backend provides the WebSocket/HTTP API and local persistence (SQLite preferred).
- Physical momentary switch controls acquisition: **hold = scan, release = stop**. No onscreen Start/Stop button.
- One selected inspection mode per inspection: **CSK Depth** or **Rivet Flushness**. Starting a different mode requires a new inspection.
- **Exactly one detector process active at a time** for the chosen mode. Reuse the existing `hole_detector` or `rivet_detector` implementation rather than rewriting the algorithm.
- An inspection has many scans; each scan has a new local coordinate system and scan-local feature IDs. No global feature correspondence between scans.
- Expected feature count is editable per scan, with the previous scan's value remembered. Count mismatch warns but does not block acceptance. Count match never stops scanning or proves coverage.
- Detection is **already incremental and asynchronous**. Hole/rivet circles can appear after switch release. Do not turn this into a synchronous pipeline.
- "Accept scan" is an operator decision, **not** a measurement tolerance pass/fail.
- No fabricated CSK depth, inspection-grade rivet flushness or dimensional pass/fail. Show **Not measured** until an actual algorithm and tolerance configuration exist.

**Out of scope for v0.1:** changing the physical button or Arduino firmware; improving detector accuracy/latency; full PCD visualization in the operator UI; global mapping; metrology algorithm development; remote/cloud service; automatic tolerances; elaborate user authentication.

## 2. Verified repository baseline (read before coding)

| Existing path | Behavior to preserve / reuse |
| --- | --- |
| `laserHHRP/scripts/scan_switch_node.py` | Pi GPIO25 switch calls `scan/start` on press and `scan/stop` on release (debounced). LEDs follow `scan/status`. |
| `laserHHRP/src/scan_controller_node.cpp` | Services `scan/start`, `scan/stop`, `scan/pause`, `scan/zero`; publishes `scan/status` (IDLE, SCANNING, PAUSED, ERROR) and Nano commands. Starts can be refused if not ready/calibrated. |
| `laserHHRP/src/synchronizer_node.cpp` | Publishes `scan/frame` with 0-based `index`. On stop, flushes only `ready_`; pending pairs still inside the hardware/software pairer are not drained. The closure count must reflect frames actually published. |
| `laserHHRP/src/point_cloud_node.cpp` | Accumulates `scan/frame`; clears on `index == 0`; publishes cumulative cloud approximately once/second. This is not the detector's input in the normal launch. |
| `hole_detector/hole_detector/node.py` | Incremental asynchronous detection from `scan/frame`, nominal 0.2 s timer. **No explicit release/end callback**: residual processing and tracker reset only happen when the *next scan's* `index == 0` arrives. Last-scan end-edge features can remain undecided. |
| `rivet_detector/rivet_detector/node.py` | Same asynchronous model and missing end-of-scan finalization for rivets, publishing `inspection/result` and `rivets/*`. |
| `laserHHRP/launch/detectors.launch.py` | Selects `holes`, `rivets` or `both` **at launch time**. Runtime selection does not yet exist. |
| `laserHHRP/launch/hhrp.launch.py` | Starts core nodes, detector launch and optional RViz. For the new browser workflow, start core with `holes:=false ui:=false`; keep the old launch working. |
| `laserHHRP/src/recorder_node.cpp` | **Confirmed omission:** `add_result()` ignores messages after the file closes on scan stop. Late detections are missing from legacy `results.json`. **Decision:** UI SQLite is canonical for complete inspection history; legacy raw/session files remain diagnostic. |
| `laserHHRP/msg/{ScanStatus,ScanFrame,InspectionResult}.msg` | Existing types lack scan UUID/finalization. **Keep their definitions unchanged for old bag compatibility**; add tagged interfaces instead. |
| `laserHHRP/src/inspection_node.cpp` | Stub currently emits no results but also publishes on shared `/inspection/result`; do not treat all publishers on that legacy topic as the chosen detector. |
| `hole_detector/test/`, `rivet_detector/test/`, `laserHHRP/test/` | Existing regression tests and historical bag replay must keep passing. |

**Important metrology reality:** `hole_detector/results.py` outputs `countersink_depth_m=-1` and `flushness_m=-1`; `rivet_detector/results.py` also outputs both as `-1`. The latter has a `height_mm` field in `detail`, which is a detector geometry output, not a validated final flushness result. Current `InspectionResult.passed=True` is not permission to show a metrology PASS badge.

**Feature contract:** Hole detector emits `feature_type="countersink"` or `"hole"`; rivet detector emits `"rivet"`. Both place their scan-local ID at `detail.id`. UI partial status is `detail.partial`, **not** a new threshold. Detector `min_rim_coverage=0.6` and `min_scanned_share=0.6` are detection acceptance thresholds. Live acquisition works as intended; identity/finalization is an incremental UI integration requirement.

## 3. Proposed project layout and ownership

```text
ui_backend/                 # new ROS 2 ament_python package: laser_hhrp_ui
  laser_hhrp_ui/
    api.py                  # FastAPI HTTP + WebSocket (proposal)
    ros_bridge.py           # rclpy subscriptions, callbacks -> thread-safe queue
    state.py                # authoritative UI/inspection state machine
    detector_supervisor.py  # exactly one allowlisted detector process
    repository.py           # SQLite schema + transactional persistence
    models.py               # validated external data models
  test/
ui_web/                     # new Vite + React + TypeScript frontend
  src/
    api/
    screens/
    components/
    state/
  tests/
laserHHRP/msg/              # six additive ROS messages, required for Phase 3
laserHHRP/launch/           # optional additive operator-web launch
plan.md                     # this document
```

Implementation defaults, not previously approved technology mandates:
- Python: `rclpy` + FastAPI + Uvicorn; isolate ROS executor in its own thread and send immutable event data to the asyncio loop via a **thread-safe queue**. Never block ROS callbacks on UI clients, SQLite or network I/O.
- Browser: Vite + React + TypeScript; CSS sized for 800 x 480; SVG for counts and spatial map; no full PCD streamed.
- Bind API to `127.0.0.1` by default; no internet dependency. Developer Mode is hidden and **disabled until the administrator configures a hashed PIN**, with no default PIN.
- SQLite under `~/.local/share/laser_hhrp/ui/inspections.db` by default (respect `XDG_DATA_HOME`), never inside Git. Use foreign keys, migrations, transactions and WAL.
- Preserve legacy RViz launch and diagnostics. New kiosk launcher must not silently replace the meaning of the old `ui` launch flag.

## 4. Two state machines and explicit decision rules

**Hardware acquisition:** Existing controller owns IDLE/SCANNING/PAUSED/ERROR and the physical button. Status publishes at 5 Hz **and on every state change**, but depth-10 non-latched ROS status is not a durable scan-event log. Scan UUID + transition sequence (section 5) is authoritative, including for quick presses.

**UI inspection lifecycle:**

~~~text
Operator -> Inspection Setup -> READY (no scan UUID yet)
READY --(confirmed physical scan started)--> SCANNING
SCANNING --(switch released / confirmed stop)--> FINISHING_ANALYSIS
FINISHING_ANALYSIS --(verified processing complete)--> RESULTS_READY
FINISHING_ANALYSIS --(timeout / failure)--> ANALYSIS_FAILED
RESULTS_READY --(Accept / Rescan)--> ACCEPTED / RESCANNED -> READY
ANALYSIS_FAILED --(Replay if possible / Rescan / Keep Incomplete)-->
                  FINISHING_ANALYSIS / RESCANNED / INCOMPLETE_RETAINED
~~~

Rules:
1. **READY represents the next scan, not a scan record.** Store editable `next_expected_count` on the inspection. On actual confirmed physical start, create scan UUID record and snapshot that count.
2. Release stops **acquisition** immediately as today. Detection may continue afterwards. Reaching expected feature count **never** triggers a stop or analysis completion.
3. A quick second physical press may begin scan B while A analyzes. Maintain separate A/B buffers and records; new capture must not wait for the UI's old decision.
4. If B starts while A is RESULTS_READY with no operator decision, A stays **UNDECIDED**, not auto-accepted/rescanned. Permit a later decision from History.
5. **ANALYSIS_FAILED cannot be accepted as completed.** Offer Retry Analysis only with recoverable raw/replay data; otherwise Rescan or Keep Incomplete, with warning and audit entry.
6. PAUSED remains the same scan; hardware ERROR never implies successful analysis. A truly completed zero-feature scan differs from failed processing.
7. End an inspection and drain/record pending work before changing detector modes. A scan started while no valid inspection exists becomes **UNASSIGNED** with visible warning, not guessed attribution.
8. The frontend is stateless with respect to acquisition: reload/browser crash reconstructs from authoritative backend snapshot.

**Operator sessions:** Identify once. Explicit logout/operator change is available only with no active inspection or unfinished scan. No idle automatic logout in v0.1. Snapshot operator ID into each newly started scan.

## 5. ROS 2 scan identity, provenance and completion

**Compatibility choice:** Do **not** extend original `ScanStatus.msg`, `ScanFrame.msg` or `InspectionResult.msg`. Existing ROS bags serialize them, and their original replay must keep working. Use **new additive interfaces** in operator-web mode (five tagged/control messages, plus the recorder metadata event below):

| New topic / message | Owner | Required fields |
| --- | --- | --- |
| `/scan/session_state`: `ScanSessionState.msg` | Scan controller | `scan_id` UUID generated on **successful start**, `controller_instance_id`, monotonic `scan_seq`, state, **`status_stamp` equal to the exact corresponding `ScanStatus.header.stamp` on transitions**, and timestamp. Emit each transition plus a latest-state snapshot. |
| `/scan/frame_tagged`: `TaggedScanFrame.msg` | Synchronizer | `scan_id` and embedded *original* `ScanFrame` with unchanged 0-based `index`. Publish in parallel with legacy `/scan/frame`, benchmark overhead. |
| `/scan/stream_closed`: `ScanStreamClosed.msg` | Synchronizer | `scan_id`, `frame_count` of tagged frames **actually published**, stop state, optional error. |
| `/inspection/result_tagged`: `TaggedInspectionResult.msg` | Selected detector | `scan_id`, sequential `result_seq`, `detector_mode`, `feature_id` from `detail.id`, embedded original `InspectionResult`; legacy `/inspection/result` remains. |
| `/inspection/detection_finalized`: `DetectionFinalized.msg` | Selected detector | `scan_id`, `frame_count`, `emitted_result_count`, `detected_target_count`, mode, success/error. |
| `/scan/recording_session`: `ScanRecordingSession.msg` | Existing recorder | Exact `ScanStatus.header.stamp` for open/close transition, unique raw-session ID, session directory, event type, recorded frame count and write status; match scan UUID via controller's transition metadata, not wall time. |

**Producer/consumer protocol:**
1. **Single acquisition-control path:** controller generates a UUID on confirmed start and publishes a `ScanSessionState` identity/transition companion, but the **existing `scan/status` alone still drives start/stop/flush** in the synchronizer, point-cloud node and recorder in *both* operator and developer modes. The new companion is **metadata, never a second acquisition command**. For every start/stop transition, stamp the original `ScanStatus` once and copy its exact `header.stamp` into the associated `ScanSessionState.status_stamp`. The synchronizer correlates by this exact transition key (plus controller instance/sequence), not by approximate arrival time or the latest UUID. Cross-topic delivery may reorder: buffer pending **tagged** frames and close metadata until identity matches, while preserving normal capture and legacy frame publication. Bound the queue; if a tag cannot be established or event sequences have gaps, fail that scan's tagged analysis explicitly. Do not guess a UUID, reassign frames to the next scan or add a second start/stop state machine.
2. On release or error, synchronizer preserves current acquisition behavior and flushes its `ready_` queue, then emits the stream-close marker. **Pairs still inside the pairer are not necessarily published**; the marker counts only published frames. Document this as capture-policy behavior, not proof of malfunction.
   **Ordering requirement:** the close marker may be sent only after all tagged frames for its scan have been published or the scan is explicitly failed for missing identity/capture. A missing identity must not hold the GPIO release or change the existing `scan/status`-controlled stop.
3. Cross-topic messages may reorder. Detector waits for all indexes `0..frame_count-1` for that scan before final processing. Missing frame => failure/watchdog, never fake completion. Repeated indices are idempotent.
4. **Mandatory explicit final pass:** After close and receipt of declared frames, process the scan's remaining buffer/edge candidates and flush asynchronous PCD/CSV writes. This must run even if the operator never starts another scan. Respect geometric guard/partial thresholds; do not invent holes beyond scanned coverage.
5. Maintain independent scan-ID-specific tracker state while a subsequent physical scan starts. Publish remaining results with tagged source/mode/UUID; then emit exactly one terminal `DetectionFinalized`.
6. **Second barrier:** A finalization message can reach UI backend before tagged results (different topics). Require all `result_seq` values up to `emitted_result_count-1` **committed to SQLite** before announcing `analysis_complete`.
7. Errors, missing frames, missing results, detector crash, overflow, or timeout produce `analysis_failed` with reason. Do not equate expected detected count with completion.

**Source/target filter (backend only):** CSK Depth selects `hole_detector` and counts only `feature_type="countersink"`; `feature_type="hole"` can be labeled non-target and is not counted. Rivet Flushness selects `rivet_detector` and counts `feature_type="rivet"`. Validate tagged mode and scan UUID. Ignore unqualified messages from the shared legacy `/inspection/result` (including future non-stub `inspection_node` output). Trust `detail.partial` boolean; quality percentages are additional details, not new UI thresholds.

**Legacy bag adapter:** For tests, read original `/scan/frame` bags, synthesize UUIDs from scan-index boundaries and explicit close at end-of-bag, then feed the new tagged path. Do not alter original bags or infer physical switch-release time if unrecorded.

**Phased dependency:** Phase 2 bridges existing status/results for **preview only**; without UUIDs it cannot guarantee accurate late-result scan assignment. Do not activate production acceptance/history until Phase 3 identities and completion are implemented. Phase 1 mock data may exercise the full workflow.

**Controller events:** The controller emits transitions even for rapid presses. Use reliable delivery, adequate event history and monotonic sequence with a controller-instance identifier to detect event gaps; do not rely on receiving every 5 Hz periodic status or infer a missing scan from time alone.
**No split-brain start/stop:** `scan/status` is the authoritative actuator-facing state for the synchronizer, point-cloud builder and recorder. `scan/session_state` provides correlated UUID metadata only. Test start, pause/resume, stop, error, quick successive presses and both possible topic-delivery orders. If any matching transition is absent, preserve existing acquisition but flag the metadata link as invalid and never emit a falsely successful UI result.

**Data-path cost:** Duplication of frame payloads can be expensive on Pi 5. Benchmark CPU, ROS bandwidth and memory on real capture; if unacceptable, design a tested metadata-only alternative that still provides unambiguous scan attribution without breaking old bags.

**Recorder decision:** SQLite is **canonical** for complete inspection measurements/late features. The legacy recorder's already-confirmed exclusion of late results needs documentation; **do not refactor it as a prerequisite for accurate UI history**. Test raw frame integrity independently and make only narrowly justified fixes.
**Raw recording identity / Retry Analysis dependency:** `recorder_node` currently starts its raw session using the **local wall-clock second** and publishes `scan/storage.session_dir` only at 2 Hz. This status alone cannot reliably identify short/back-to-back recordings. Add a small **`ScanRecordingSession.msg` / `/scan/recording_session`** metadata event emitted by the recorder **immediately after successfully opening and on closing** a raw scan file. Include the **exact triggering `ScanStatus.header.stamp`**, `session_dir`, raw recording session identifier, open/close result, frame count and write status; the backend correlates that stamp with the controller's `ScanSessionState.status_stamp` and stores the matched raw path against the scan UUID. The recorder need not learn or guess a UUID. Make raw directory naming **collision-proof** (exclusive per-scan suffix or scan-sequence identifier, never truncate an existing `frames.bin`); the current second-granularity name can collide if multiple scans start within a second. Changes are limited to raw-record identification, path safety and event emission, **not** detector result lifetime. A matching directory is **necessary but not sufficient** for Retry Analysis: validate recorded frame identity/count and a replay-safe file before enabling Retry. If missing/incomplete, disable Retry and offer Rescan/Keep Incomplete; never claim replay capability from `scan/storage.session_dir` alone.

## 6. Detector runtime supervisor

Current `detectors.launch.py` supports only startup mode selection. Add the following **separate UI-controlled lifecycle**:
- Keep hardware/core ROS nodes running independently; launch them without auto-spawning detectors/RViz for production web mode.
- Inspection creation selects one allowlisted executable: `hole_detector` or `rivet_detector`, with existing parameters and **configurable root** `~/.local/share/laser_hhrp/ui/detector-output/` (honor `XDG_DATA_HOME`). **Never run both**. Do not hardcode the stale `~/aric_ws/...` default. **Required small detector storage change in Phase 3:** existing `ScanFolder` creates time-named folders only on the first feature, so operator tagged mode must instead create a deterministic `<inspection_uuid>/<scan_uuid>/` folder **on confirmed scan start, including scans with zero detections**, initialize `summary.csv` even if empty, and write each result into its UUID folder. Preserve current lazy time-based folder behavior for legacy/developer mode; do not change the detection algorithm.
- Start selected detector before showing Ready. Confirm process liveness **and ROS subscription/readiness**; a spawned PID alone is insufficient. Report startup failure in the UI, and prevent falsely showing Ready.
- Keep the selected detector alive across multiple scans within the same inspection.
- On inspection close/mode change, ensure pending scans have terminal analysis states and writes have drained, then gracefully stop the detector. Handle SIGINT/termination, cleanup and restart; prevent duplicates and orphan processes. Use fixed command arguments; no shell interpolation of user input.
- If a detector crashes, mark its pending scans unresolved/failed, surface the fault and permit an explicit recovery path. Do **not** automatically certify pending results.
- Maintain and test developer flows (`mode:=holes|rivets|both`, bag replay, RViz) unchanged outside the new operator mode.

## 7. WebSocket and HTTP API (versioned)

Expose a **complete snapshot** at connection and on reconnect. Broadcast incremental, JSON-serializable events with a monotonic `event_seq`, `scan_id` where relevant and UTC timestamp. Event IDs make repeated delivery safe.

**WebSocket `/ws` envelope (example, proposed):**

```json
{
  "version": 1,
  "event_seq": 42,
  "type": "feature_detected",
  "timestamp": "2026-11-17T09:00:00Z",
  "inspection_id": "INS-20261117-001",
  "scan_id": "UUID-from-ROS",
  "payload": {
    "feature_id": 7,
    "kind": "countersink",
    "x_mm": 12.1,
    "y_mm": 103.5,
    "diameter_mm": 7.46,
    "partial": false,
    "measurement": { "name": "csk_depth", "value_mm": null, "status": "not_measured" }
  }
}
```

**Event types:** `snapshot`, `system_state`, `scan_progress`, `feature_detected`, `scan_acquisition_ended`, `analysis_complete`, `analysis_failed`, `inspection_updated`, `scan_decision`.

A snapshot contains current operator, inspection ID/mode, active scan ID, lifecycle state, expected/detected counts, elapsed distance, warnings, all features of the active scan, detector readiness and recent scan records. On reconnect prefer a fresh snapshot over speculative client replay; `event_seq` is useful for deduplication.

**HTTP commands (proposed; choose consistent method/validation/status codes):**
- `GET /api/v1/state`: same snapshot as WS initial state.
- `POST /api/v1/operator`: set operator ID (alphanumeric, e.g. `ENG-1042`).
- `POST /api/v1/inspections`: create inspection with editable/generated ID and selected immutable mode.
- `POST /api/v1/inspections/{id}/end`: close inspection after its pending work is safely resolved.
- `PATCH /api/v1/inspections/{id}/next-expected-count`: only in Ready; **inspection-level setting**, integer **1..500**. No scan UUID exists yet; snapshot count into new scan on confirmed acquisition start.
- `POST /api/v1/scans/{id}/decision`: `accept` or `rescan` only from RESULTS_READY, including earlier UNDECIDED scans; record mismatch, actor and time; idempotent.
- `GET /api/v1/inspections/{id}/scans` and `GET /api/v1/scans/{id}`: history/details.
- `POST /api/v1/scans/{id}/failure-action`: `retry` (only with available replay), `rescan` or `keep_incomplete` after ANALYSIS_FAILED.
- `POST /api/v1/operator/logout`: allowed only with no active inspection/pending analysis.
- `GET /api/v1/inspections/{id}/export.csv`: return a CSV **download only** (read-only request; no server-side USB writes).
- `POST /api/v1/inspections/{id}/exports/usb`: explicit **server-side write** of a CSV file to an approved writable mounted USB destination, with allowlisted mount roots, safe filename and audit/confirmation. `GET` must never write to removable media.
- `GET /api/v1/health`: API, ROS, database and detector readiness.

**Defaults:** Operator ID 1..32 ASCII letters/digits/underscore/hyphen. Inspection label 1..64 of same characters, case-insensitive unique; immutable internal UUID separate from editable label. Generated `INS-YYYYMMDD-NNN` only with verified time; otherwise unique `INS-LOCAL-NNNNNN`. UTC timestamps include clock quality. **Forbidden:** web API directly starting/stopping the physical scan in User Mode. Validate commands server-side, make decisions idempotent and do not block ROS callbacks.

## 8. Canonical history, time and retention

**Decision:** SQLite is the **canonical and complete** store for operator sessions, inspections, scans, late detections, analysis finalization, decisions and history. The legacy recorder's `results.json` is incomplete for late detections because it closes on release. Continue to use `frames.bin` / `session.json` for raw/diagnostic data, and link these paths; do not use `results.json` as definitive history.

SQLite schema, with versioned migrations, foreign keys, WAL and transactions:
- `operator_sessions`: operator ID, work-session start/end, time quality.
- `inspections`: immutable internal UUID, unique editable ID, selected immutable mode, operator session, **next_expected_count** (1..500), state and times.
- `scans`: ROS UUID PK, inspection ID or explicit unassigned state, human scan sequence, **operator and expected-count snapshot**, timestamps, clock quality, published frame count, detected target count, emitted result count, analysis state/error, nullable operator decision (**UNDECIDED**), **raw recording session ID, verified recording directory and replay-eligibility status**.
- `features`: unique scan UUID + detector mode + local feature ID, tagged result sequence, local X/Y/Z mm, original `detail` JSON, geometry/quality, partial flag, nullable validated inspection measurement/status.
- `audit_events`: operator decisions, warnings, failure/recovery, export and state changes.

Persist each detection transactionally on arrival, including after switch release; completion only after all declared tagged results committed. Retain rescans and originals, never overwrite features. Reboot with unfinished analysis => **interrupted/failed** unless actual replayable data supports recovery. No fake PASS; both current detectors' `countersink_depth_m=-1` and `flushness_m=-1` represent **Not measured**, and `InspectionResult.passed=True` is not valid tolerance evidence.

**Offline time:** Pi 5 without battery-backed RTC may have wrong wall clock. Use monotonic clock for durations, UUID/DB sequence for order, and UTC + `clock_quality` of `synced` / `manually_set` / `unverified` for history. When unverified show **Time not verified**, avoid date-based IDs, provide manual time verification procedure. Clock changes cannot overwrite data or reorder scans.

**Storage:** Default database `~/.local/share/laser_hhrp/ui/inspections.db` (respect `XDG_DATA_HOME`) and artifacts under the same UI data root; avoid workspace-specific defaults. **No automatic deletion** in v0.1, including rescans. Show free-space warning/NOT READY under **500 MB** (UI-only; controller presently treats low space as warning, not a new hardware interlock). CSV export per inspection, with an explicit operator action and mounted USB permission check. Provide an administrator procedure for backup/manual archive/cleanup; automated quotas and retention policies deferred pending actual scan sizes.
**Export behavior:** `GET .../export.csv` provides a downloadable file without server-side disk side effects. `POST .../exports/usb` writes to a validated mounted USB location; check mount status, authorization, writability and filename collision before writing, then record success/failure and destination in the audit log.

## 9. Operator screens, readiness and map semantics

All screens: **800 x 480** landscape, >=48 px touch targets where practical, large values and clear units, no raw PCD in User Mode.

**Ready policy:** Show READY only if recent `scan/status` says **IDLE**, calibration is CALIBRATED, no FATAL status, chosen detector alive **and ROS subscriptions actually ready**, operator/inspection valid, UI DB writable and storage not failing. Require `laser/status.connected` fresh within **3 s**, `nano/status` fresh within **1.5 s** with `encoder_ok==true`, and free space >=**500 MB**. **Intentional distinction:** the controller's direct `start_scan()` check does not explicitly gate on `encoder_ok`, but the monitor marks encoder failure FATAL; the UI's explicit `encoder_ok` Ready check is a conservative display requirement, **not** an added hardware interlock. An IMU fault remains a warning under the current monitor rules. Missing/stale critical data => NOT READY with actionable reason. The existing controller retains final start authority. Show other acquisition/tilt/speed warnings as reported.

1. **Operator Identification:** alphanumeric ID, onscreen keyboard, once per session. Logout or change operator only after closing inspection with no analysis pending; no idle timeout.
2. **Inspection Setup:** generated/editable ID; CSK Depth or Rivet Flushness, fixed for inspection. Start one selected detector before READY.
3. **Ready / Scanning / Finishing Analysis:** Ready edits *inspection's next expected count* (1..500), without scan UUID. During acquisition display distance, **detected target / expected**, asynchronously filling count circles. For >40 expected, display 40 circles plus remaining count rather than tiny hundreds. Release immediately stops capture; Finishing Analysis continues filling results. Do not stop at count match.
4. **Results:** count and mismatch warning, local XY spatial map and selected feature, partial status and geometry, Not measured until validated metrology, Accept / Rescan. ANALYSIS_FAILED offers Retry only if replayable, Rescan or Keep Incomplete, not Accept.
5. **Inspection History:** optional from Ready and Results; include Accepted, Accepted with Warning, Rescanned, **UNDECIDED**, Failed/Incomplete; reopen earlier scan data and later decide undecided scans. Never overwrite rescans.

**Map contract:** `InspectionResult.position_m` uses local **(x,y,z) metres**. Plot x = position_m[0] * 1000 across the laser line **rightward**; y = position_m[1] * 1000 along roll/scan axis **downward**, z not drawn. (Changed on 2026-11-22 at the user's request to x **leftward**, y **upward**: the operator's view from behind the device, the scan's start at the bottom; `ui_web/README.md`.) The displayed top-left is the viewport bounding minimum, **not global workpiece origin**. Preserve signed y on backwards scans, show mm units and +Y indicator, fit with **uniform scale** (no distortion), and provide larger tap targets than rendered circles. There is no cross-scan spatial registration.

**Partial:** Display amber only when the selected detector sets `detail.partial==true`. `ring_coverage` / `edge_coverage` and `scanned_share` are supporting quality fields; no UI-only partial cutoff.

Normal route: Identification -> Setup -> Ready -> Scanning -> Finishing Analysis -> Results -> Accept/Rescan -> Ready. Previous RESULTS_READY scans without decisions stay UNDECIDED if another scan starts.

## 10. Execution phases (one independently reviewable PR/commit series per phase)

### Phase 0: Protect and document baseline
- [ ] Confirm `ui` HEAD, existing launches and tests; do not change `dev` / `main`.
- [ ] Record the real topic/message schemas, known detector outputs and current empty/not-measured semantics in a concise developer README.
- [ ] Establish deterministic simulation/bag replay instructions. Run baseline tests and record actual outcomes/environment limits.
- **Exit:** Existing behavior can be reproduced and test regressions measured.

### Phase 1: API/state/persistence foundation (no live ROS mutations)
- [ ] Create `ui_backend/` package and `ui_web/` scaffold; local health endpoint and WS connect/snapshot.
- [ ] Implement pure Python state-machine models and transitions; write SQLite migrations/repository.
- [ ] Implement HTTP operator/inspection-level next expected count/decision/failure action/history commands, validation (1..500 count) and idempotency; mock UNDECIDED and failed states.
- [ ] Add a mock ROS event source with delayed/out-of-order detections, quick consecutive scans and injected failures.
- [ ] Add tests for transitions, restart/reconnect, failed start, modes, count mismatch acceptance and rescan retention.
- **Exit:** Five-screen workflow can be driven by mocked streams and survives browser/backend restarts without incorrect acceptance.

### Phase 2: Live ROS bridge and one-detector supervisor
- [ ] Subscribe to existing `/scan/status`, `/inspection/result` and readiness topics via `rclpy` **for preview only** before Phase 3. Do not claim reliable scan IDs or persist production acceptance/history from this preview.
- [ ] Implement bounded thread-safe ROS -> API event queue and full snapshot/reconnect semantics.
- [ ] Implement allowlisted detector start/stop/readiness/crash supervision; maintain one detector for an inspection, never both.
- [ ] Ensure physical scan starts are detected from confirmed ROS state, not inferred from UI commands.
- [ ] Run real/simulated `holes` and `rivets` separately and verify existing topics and latched RViz displays remain unaffected.
- **Exit:** Selected-detector live status and incremental detections reach the **UI preview only**; **no authoritative scan-ID assignment, no persisted production history and no scan acceptance** until Phase 3.

### Phase 3: Stable scan identity and reliable analysis finalization
- [ ] Add **six new ROS metadata/tagging types** (session state, tagged frame, stream closed, tagged result, detector finalized, raw recorder session) while preserving original message schemas and old bag replay.
- [ ] Keep `scan/status` as the **sole** synchronizer/recorder/point-cloud acquisition control. Correlate controller transition metadata with the exact original status stamp; test either topic arriving first, pause/resume, rapid starts and missing identity. Add stream-close marker with **actual published tagged frame count**; document pairs still held inside the pairer at stop.
- [ ] Add explicit detector final pass **on scan close, without a subsequent scan**, per-scan UUID buffering and terminal acknowledgment, including markers arriving before final frames, slow detection and back-to-back scans. Implement a UUID-named output folder **on scan start**, even for zero-feature scans; keep legacy folder rules in developer mode.
- [ ] Persist tagged late results to **canonical SQLite**, wait until all result sequences are committed before analysis_complete. The legacy recorder is **known** to omit late detector results. Add only the small recorder metadata/unique-path changes needed to map a raw recording reliably to its scan UUID, preventing same-second raw-file overwrites; verify recorded frames before allowing Retry Analysis.
- [ ] Watchdog defaults: warning after **10 s** post-release, explicit failure at **120 s** without successful completion (configurable and Pi-tested); no fixed-delay success. Add old-bag tagged replay adapter.
- **Exit:** For every scan the backend can say **pending, finalized, or failed**, keyed by scan ID; no results cross scan boundaries, including a rapid second button press.

### Phase 4: Build the approved operator screens
- [ ] Implement shared 800 x 480 shell, system readiness/warning header and touchscreen inputs.
- [ ] Implement Identification, Setup, Ready/Scanning/Finishing Analysis, Results and History screens.
- [ ] Show asynchronously filling detection-count circles; support overflow, zero counts and partial features.
- [ ] Implement spatial SVG results map from real per-scan feature coordinates and selectable details.
- [ ] Implement Accept/Rescan, UNDECIDED/ANALYSIS_FAILED handling, inspection-level count defaults, **GET CSV download and separate POST USB export**, and no-fake-pass measurements.
- [ ] Add UI unit/component tests and screenshot/visual QA at **800 x 480**.
- **Exit:** All five screens operate end-to-end on replay/mock data and live ROS input.

### Phase 5: Pi 5 deployment and verification
- [ ] Add separate operator-web launch without breaking existing launch files. **Build frontend on dev PC/CI** (pinned Node + npm ci), ship static `dist/`; Pi runtime must not need Node/Vite. Pin Python FastAPI/Uvicorn dependencies and document apt/venv/pip wheelhouse/rosdep for offline installation, with no silent install.
- [ ] Configure Chromium kiosk on the 800 x 480 Pi display; keep startup initially manual, document optional auto-start separately.
- [ ] Verify offline startup, GPIO press/release, detector selection, repeated scans and saved history on hardware.
- [ ] Test CPU/memory under worst observed detector load and both inspection modes **separately**; web UI must not delay ROS capture.
- [ ] Test WS disconnect, browser/backend restart, failure/recovery, disk-full/read-only, detector exit, clock unverified, GET download/POST USB export, retention, same-second successive raw scan directories and unexpected device disconnect. Measure tagged-frame CPU/bandwidth on Pi.
- [ ] Run existing C++/Python regression suites and scan/replay acceptance tests.
- **Exit:** Operator can create an inspection, scan, release/lift device, see late detections, review/accept/rescan, reopen history and safely switch to a new inspection mode on the Pi.

## 11. Mandatory acceptance scenarios

1. **Normal CSK:** Identify operator -> setup CSK -> expect 20 -> hold/release switch -> receive detections both before/after release -> final count -> Results -> Accept -> Ready. No false dimensional PASS.
2. **Normal rivets:** New Rivet Flushness inspection runs **only `rivet_detector`**, renders head positions and explicitly labels unvalidated flushness as Not measured.
3. **Count mismatch:** 18/20 yields warning; operator can still Accept; record both counts and decision. 21/20 never stops hardware scanning.
4. **Fast next scan:** A ends while detections are pending; B starts immediately. A's late feature IDs/results/analysis status stay with A; B remains clean; both records survive.
5. **No detection / partial:** Zero detections or boundary-limited partial features finalize honestly with suitable warnings. Do not hallucinate unseen holes after lifting device.
6. **Lost/late message:** Reorder `ScanStreamClosed` ahead of final frames; completion waits for expected indices. A missing index results in an explicit failed/incomplete state, never a fake success.
7. **Browser reload:** Reopen to the exact current authoritative state while physical scanning or analysis is running. No scan restart, duplicate feature or duplicate acceptance.
8. **Detector change:** Same-mode scans reuse one detector. Ending inspection A and starting opposite mode B leaves exactly one correct detector active, after pending work is resolved.
9. **Restart/fault:** SQLite retains finalized history, including late features, and marks unresolved scans interrupted. ANALYSIS_FAILED supports retry only from raw data, or Rescan/Keep Incomplete, never Accept.
10. **Performance/regression:** Old bags and scan-switch, calibration, synchronizer, detector, recorder and RViz workflows continue to work. Added tagged-frame traffic does not block acquisition.
11. **Quick press/undecided:** Short scan uses matching status-transition stamp/UUID/sequence metadata; missing or reordered metadata must fail explicitly rather than guess. Existing `scan/status` continues to drive acquisition. A prior RESULTS_READY scan stays UNDECIDED on new physical start.
12. **Last scan:** Terminal detector pass runs on release without requiring another scan; missing/reordered frames or results never create false completion.
13. **Data/visual:** Selected-mode source filtering, `detail.partial` status, signed XY map, clock-quality warning, intentional encoder Ready check, operator logout guard and GET CSV/POST USB export all work.
14. **Zero-feature/rapid raw recording:** Operator-tagged detector creates a scan folder even with no features; the legacy recorder produces distinct raw directories for two starts in one second, maps each through the exact status-transition key, and enables Retry only for validated replayable data.

## 12. Coding agent rules and delivery checklist

- Work **only** on `ui`, in small focused commits/PRs mapped to phases. Do not force-push, merge or delete other branches.
- Before coding a phase, inspect its existing source files and adjacent tests. This plan is a design contract, not evidence of current runtime behavior beyond the baseline described above.
- Preserve existing ROS acquisition-control topics; add the six specified identity, tagging and completion/recording metadata topics. Make narrow legacy detector storage, explicit-finalization and recorder unique-path/event changes required by those contracts, with regression tests for each change. Do **not** add a second scan-control source.
- Preserve capture behavior, existing `scan/frame` index semantics, calibration requirements and GPIO handling.
- Do not claim feature metrology, tolerance compliance, analysis completion or durable writes without corresponding evidence.
- For every phase deliver: source changes, automated tests, how to run locally/Pi, changes to public contracts, screenshots when a screen changes, and known limitations.
- Keep API/ROS message names aligned with the chosen implementation; document any justified deviation from the examples here before consuming them in another component.
- Do not add external cloud services or download/install dependencies silently in production scripts.

**Definition of done for v0.1:** The five-screen operator UI works at 800 x 480 on the Pi, one detector operates per inspection, physical button semantics are unchanged, delayed detections remain associated with the correct scan, completion is explicit, records and rescan history persist, and regression/acceptance tests pass.

**Remaining implementation choices:** Exact field encodings for the six additive ROS types, a bounded status/UUID correlation buffer, supervisor mechanics, migration/UI libraries and offline installation commands. The single `scan/status` acquisition-control path, required scan/recorder identity link, UUID detector output folders, canonical SQLite record, old-bag compatibility, expected-count API, failure/undecided policies, 10s/120s watchdog, offline clock and retention defaults are specified. Changes need documented tradeoffs/tests.
