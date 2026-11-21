# laser_hhrp_ui: operator UI backend

ROS 2 `ament_python` package. It holds the authoritative inspection state, keeps the canonical
history in SQLite, and serves the HTTP/WebSocket API used by the browser UI ([../ui_web](../ui_web)).
Contract: [plan.md](../plan.md). Status: **Phase 3**. On live ROS it records every physical scan under the
controller's scan UUID, waits for the selected detector's finalization, links the raw recording and verifies
it. Also runs on the mock source without ROS.

## Run

```bash
# live ROS: start the core WITHOUT detectors or RViz; the backend runs the selected detector itself
ros2 launch laser_hhrp hhrp.launch.py holes:=false ui:=false        # add simulate:=true without hardware
ros2 run laser_hhrp_ui server --static ui_web/dist                   # http://127.0.0.1:8080

# no ROS at all
cd ui_backend && python3 -m laser_hhrp_ui.server --mock --static ../ui_web/dist
```

For a service (Phase 5), start the installed `lib/laser_hhrp_ui/server` directly: the `ros2 run` wrapper
does not forward SIGTERM and would leave the server, and its detector, running.

| Option | Default | |
| --- | --- | --- |
| `--db` | `$XDG_DATA_HOME/laser_hhrp/ui/inspections.db` (`~/.local/share/...`) | SQLite file, never in the Git checkout |
| `--host` / `--port` | `127.0.0.1` / `8080` | local only by default |
| `--mock`, `--mock-speed` | off, 1.0 | mock ROS source and its time scale (instead of live ROS) |
| `--no-ros` | off | neither mock nor ROS: API only, never Ready |
| `--data-root` | next to `--db` | `detector-output/<inspection_uuid>/` and `logs/` (detector logs, pidfile) |
| `--analysis-warn-s` / `--analysis-fail-s` | 10 / 120 | watchdog after switch release |
| `--static` | installed `share/laser_hhrp_ui/web` if present | built frontend |

**Live ROS.** `ros_bridge.py` subscribes read-only to `scan/status`, `laser/status`, `nano/status`,
`scan/storage` (readiness) and the tagged identity topics: `scan/session_state` (scan transitions),
`scan/stream_closed`, `inspection/result_tagged`, `inspection/detection_finalized`, `scan/recording_session`.
It never publishes or calls a service; the untagged `inspection/result` is ignored. Identity subscriptions are
volatile, so old transitions replayed after a backend restart cannot create scans. The detector runs with
`tagged:=true`.

**Raw recording.** The recorder's session is matched to its scan by the exact stamp of the start transition,
never by time. When both the session and the scan's frame stream have closed, `frames.bin` is walked record by
record: the scan is replay-eligible only if the file is complete and holds exactly the frames the scan
published. Retry Analysis is still refused (replay into the detector is not implemented), with the reason. `detector_supervisor.py` runs
`hole_detector` (CSK Depth) or `rivet_detector` (Rivet Flushness), never both, with the package's own
`params.yaml` and a fixed argv. Ready requires the process **and** its node subscribing `/scan/frame_tagged` and
publishing `/inspection/result_tagged`. A detector node the UI did not start (core launched with `holes:=true`)
or a second one makes the state Not ready; a crash is reported with its exit code and restarted only
through `POST /api/v1/detector/restart`.

**Dependencies:** FastAPI, Uvicorn (rosdep keys `python3-fastapi`, `python3-uvicorn`); tests also need
pytest and httpx. Request bodies are validated in plain Python (`models.py`), so the code does not
depend on the pydantic major version: the laptop uses pydantic 2, while Ubuntu 24.04 apt ships pydantic 1
with FastAPI 0.101. The apt versions are **not yet tested on the Pi** (Phase 5).

## Tests

```bash
cd ui_backend && python3 -m pytest -q test/        # 103 tests, ~15 s
colcon test --packages-select laser_hhrp_ui           # same tests via colcon
```

- `test_state.py`: engine with direct events and a fake clock. Covers the lifecycle, the completion
  barrier, reordering, duplicates, the watchdog, failures, decisions, guards and restart recovery.
- `test_api.py`: HTTP + WebSocket through FastAPI's TestClient with the mock source.
- `test_mock_scenarios.py`: every mock scenario end to end at 40× speed.
- `test_models_repository.py`: validation, the detector-result contract, schema/WAL/constraints.
- `test_preview_bridge.py`: the bridge's message conversions (tagged topics) and the Phase 2 preview fallback.
- `test_supervisor.py`: the supervisor with real processes and signals (fake detector executables).

Live checks against the simulated core, real detectors and the real ROS graph (isolated ROS domain, scratch
HOME): see [VERIFY_PHASE3.md](../docs/operator_ui/VERIFY_PHASE3.md).

To check that the tests catch real bugs: `python3 ../tools/ui_verify/mutation_check.py` (see
[VERIFY_PHASE1.md](../docs/operator_ui/VERIFY_PHASE1.md)).

## Architecture

```
ROS bridge (Phase 2) / MockRos ──submit()──> Ingress (bounded, thread-safe) ──> Engine ──> SQLite
                                                                                  │
                         HTTP commands (async, loop thread) ─────────────────────>│──emit──> EventHub ──> /ws clients
```

- **Engine** (`state.py`) is single-threaded on the asyncio loop. ROS callbacks only enqueue
  immutable events (`events.py`) and never block on SQLite or clients. Queue overflow fails pending
  analyses visibly.
- **Completion barrier:** a scan is `complete` only when acquisition ended, `StreamClosed` and
  `DetectionFinalized` arrived with **equal frame counts**, every declared `result_seq`
  0..n‑1 is **committed to SQLite**, the backend's target count equals the detector's, and
  `inspection_node`'s `MeasurementFinalized` arrived with exactly one entry per target feature (stored in
  one transaction). Otherwise the scan is pending (watchdog: warning at 10 s, failure at 120 s) or failed with a reason.
  Reaching the expected count never completes or stops anything.
- **Stop evidence:** a scan completes only on the controller's confirmed stop. A stop inferred from the next
  scan's start is `UNCONFIRMED`: the analysis stays pending until the controller's own stop arrives (in any
  order relative to the stream close and finalization) or the watchdog fails it ("stop not confirmed"). A
  frame-stream close with an error, or whose stop state differs from the controller's, fails the analysis.
- **Terminal messages:** an identical redelivery of `ScanStreamClosed`, `DetectionFinalized` or `MeasurementFinalized` is ignored; a
  different second one fails a pending analysis as a conflict. After the analysis is closed the scan is not
  rewritten, but a conflicting message raises an alert. A redelivered result counts as a duplicate only if it is identical (sequence, feature
  id, kind, position and detector detail); otherwise the scan fails as a conflict.
- **Measurements** come only from `inspection_node` (`inspection/measurement`, see
  `docs/operator_ui/MEASUREMENT_INTEGRATION.md`): per target feature `measured` with `value_mm` and a `verdict`
  (`in_limits` / `out_of_limits` against the inclusive GO range sent with it, `lower_mm` .. `upper_mm`), or
  `not_measured` with the algorithm's `reason` code. Until then a feature's measurement is `pending`. A measurement
  that does not cover exactly the targets, a value beyond 10 mm or not finite, or `success = false` fails the scan.
  Scan views carry `measurement`: counts `in_limits`, `out_of_limits`, `not_measured`, `pending`. With live ROS,
  Ready also needs `inspection_node` in the graph ("Measurement not running").
- **Never invented:** the detectors' own measurement fields (-1) and `passed` are ignored, `partial` comes only
  from the detector's `detail.partial`, and a hardware `ERROR` stop fails analysis. Restart turns unfinished
  scans into `interrupted`.
- **Detector control** interface (`request(mode)`, `stop()`, `replay(scan)`): `NullDetectorControl`
  now, the mock in mock mode, the process supervisor in Phase 2.

## API v1 (all JSON)

| Method, path | Body | Notes |
| --- | --- | --- |
| `GET /api/v1/health` | | API, source, database, detector, ingress drops |
| `GET /api/v1/state` | | full snapshot + `event_seq` |
| `POST /api/v1/operator` | `{operator_id}` | 1..32 `[A-Za-z0-9_-]`; changing operator needs no open inspection |
| `POST /api/v1/operator/logout` | | only with no inspection or pending analysis |
| `POST /api/v1/inspections` | `{mode, label?, request_id?}` | `mode`: `csk_depth` / `rivet_flushness`; label 1..64, unique ignoring case, generated if empty; the same `request_id` returns the same inspection |
| `GET /api/v1/inspections` | | recent inspections |
| `POST /api/v1/inspections/{uuid}/end` | | 409 while acquiring/analysing; idempotent |
| `PATCH /api/v1/inspections/{uuid}/next-expected-count` | `{count}` | 1..500, only on Ready / Not ready |
| `POST /api/v1/detector/restart` | | explicit recovery after a detector crash or failed start |
| `POST /api/v1/device/calibrate` | | gyro calibration (`calibration/start`); 409 during a scan, while one runs, without current hardware status; the outcome comes in `hardware.calibrating` / `hardware.message` |
| `POST /api/v1/device/reboot` | | runs `--reboot-command` (e.g. `systemctl reboot`); the same rules as power off; `device.power_action` = `reboot` |
| `POST /api/v1/device/service/unlock` | `{pin}` | developers' service menu (kiosk: `--service-pin-file`, `--kiosk-control`): checks the PIN (403 wrong, 429 after 5 wrong for 60 s), returns `{network}` |
| `POST /api/v1/device/service/{desktop\|restart}` | `{pin}` | writes the request for the kiosk session (`deploy/kiosk`); 409 during a scan or a pending analysis; `device.switching` until the backend stops |
| `POST /api/v1/device/power-off` | | runs `--power-off-command` (e.g. `systemctl poweroff`); 409 during a scan or a pending analysis or when not configured; `device.powering_off` until the system stops the backend; a repeated call runs nothing |
| `GET /api/v1/inspections/{uuid}/scans` | | history, oldest first |
| `GET /api/v1/scans/{scan_id}` | | scan with features |
| `POST /api/v1/scans/{scan_id}/decision` | `{decision: accept\|rescan}` | completed analyses only (including earlier undecided scans); idempotent |
| `POST /api/v1/scans/{scan_id}/failure-action` | `{action: retry\|rescan\|keep_incomplete}` | failed/interrupted only; `retry` needs verified raw data |
| `GET /api/v1/inspections/{uuid}/export.csv` | | CSV download, built in memory: **no server-side write** |
| `GET /api/v1/exports/usb` | | read-only list of mounted drives below the allowed roots (`--usb-root`, default `/media`, `/run/media`), with writability and free space |
| `POST /api/v1/inspections/{uuid}/exports/usb` | `{mount}` | writes `<label>.csv` (`-2`, `-3` ... never overwrites) to a listed drive; 403 outside the roots, 409 not mounted / read-only, 507 full; audited (`export_usb` / `export_usb_failed`) |
| `WS /ws` | | `snapshot` first, then events |

Errors: `{"error": {"code", "message", "field"}}` with 400 (invalid), 404 or 409 (wrong state).
Scan views carry `retry_available` (verified raw data **and** a replay path): the screens offer Retry
Analysis only when it is true.

**CSV** (one inspection): one row per feature, plus one row for a scan without features, so every scan
(including rescans and failures) appears. Columns: inspection label/UUID, mode, scan number/UUID, status,
decision, decided by/at, operator, start/end, clock quality, expected and detected counts, mismatch,
analysis state/error, frame count, feature id, kind, target flag, x/y/z mm, diameter, partial, coverage,
scanned share, fit RMS, measurement name/status/value, `measurement_verdict` (in_limits / out_of_limits),
`limit_lower_mm`, `limit_upper_mm`, `measurement_reason`. Free-text cells that start with `= + - @` get a leading `'`.
**No endpoint starts or stops the physical scan.** `/api/v1/dev/mock/*` (press, release, scenario,
fault, hardware, layout, power-on) exist **only with `--mock`** (the mock calibrates in 2 s and turns nothing off).
`snapshot.device` = `{can_calibrate, can_power_off, powering_off, can_service, switching}`: which device actions the
screens offer. The mock's service PIN is `2580` (file next to `--db`).

**Envelope:** `{version: 1, event_seq, type, timestamp, inspection_id, scan_id, payload}`. Types:
`snapshot`, `system_state`, `scan_progress`, `feature_detected`, `scan_acquisition_ended`,
`analysis_complete`, `analysis_failed`, `inspection_updated`, `scan_decision`. A client subscribes
before it receives the snapshot; events with `event_seq <=` the snapshot's are already included.

**Screens** (`snapshot.screen`): `identify`, `setup`, `not_ready`, `ready`, `scanning`,
`finishing_analysis`, `results`, `analysis_failed`. The screen follows the inspection's latest scan.
An earlier scan left in results when a new one starts stays `undecided` in history.

**Scan status labels:** `scanning`, `analyzing`, `undecided`, `accepted`, `accepted_with_warning`
(count mismatch), `rescanned`, `failed`, `incomplete_retained`, `unassigned`.

## Deviations from plan.md (documented per section 12)

- `ScanTransition.transition_seq` increments on **every** controller transition, not once per scan
  (`scan_seq`), so a missed stop is detectable as a gap. `ScanSessionState.msg` carries it (`transition_seq`).
- `next_expected_count` starts empty for the very first inspection (no invented default); Ready
  requires it. Later inspections prefill the previous value.
- A scan whose acquisition ended in `ERROR` fails analysis after its results are stored (plan: an error
  never counts as success). Operator options: Rescan or Keep Incomplete.
- A scan started without an operator, an open inspection or a ready detector is stored as
  `unassigned` with the reason and an alert; its results are never attributed.
- Plan section 1 ("no pass/fail until a validated algorithm and limits exist"): since 2026-11-21 the screens show
  GO / NO-GO per feature, against the limits given for the repeatability trials (`hhrp.yaml` `inspection.*`).
  The algorithms' accuracy against gauge readings is not yet established; the operator decision (Accept /
  Rescan) is unchanged and not tied to the verdicts.
- Plan section 3 (the backend only observes ROS, no device control from the web): since 2026-11-21 the screens
  calibrate the gyro (`calibration/start`) and turn the device off (`--power-off-command`), so the device runs
  without a terminal (operator kiosk, `deploy/kiosk/`). Both are refused during a scan, power off also while an
  analysis is pending; `device.py`, tests in `test/test_device.py`. Scan start / stop stays on the switch.

## Not yet

Replay of a verified raw recording for Retry Analysis; Pi/apt validation and the operator launch (Phase 5). Everything is verified on an x86 laptop (Humble) only.
