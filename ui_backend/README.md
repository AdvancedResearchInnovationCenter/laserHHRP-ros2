# laser_hhrp_ui: operator UI backend

ROS 2 `ament_python` package. It holds the authoritative inspection state, keeps the canonical
history in SQLite, and serves the HTTP/WebSocket API used by the browser UI ([../ui_web](../ui_web)).
Contract: [plan.md](../plan.md). Status: **Phase 2**. It runs on live ROS (read-only bridge, one supervised
detector) or on the mock source. Live detections are a **preview**: shown, never recorded or acceptable,
until Phase 3 adds tagged scan identity and finalization.

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

**Live ROS (Phase 2).** `ros_bridge.py` subscribes read-only to `scan/status`, `laser/status`, `nano/status`,
`scan/storage` and `inspection/result`; it never publishes or calls a service. `detector_supervisor.py` runs
`hole_detector` (CSK Depth) or `rivet_detector` (Rivet Flushness), never both, with the package's own
`params.yaml` and a fixed argv. Ready requires the process **and** its node subscribing `/scan/frame` and
publishing `/inspection/result`. A detector node the UI did not start (core launched with `holes:=true`)
or a second one makes the state Not ready; a crash is reported with its exit code and restarted only
through `POST /api/v1/detector/restart`.

**Dependencies:** FastAPI, Uvicorn (rosdep keys `python3-fastapi`, `python3-uvicorn`); tests also need
pytest and httpx. Request bodies are validated in plain Python (`models.py`), so the code does not
depend on the pydantic major version: the laptop uses pydantic 2, while Ubuntu 24.04 apt ships pydantic 1
with FastAPI 0.101. The apt versions are **not yet tested on the Pi** (Phase 5).

## Tests

```bash
cd ui_backend && python3 -m pytest -q test/        # 88 tests, ~15 s
colcon test --packages-select laser_hhrp_ui           # same tests via colcon
```

- `test_state.py`: engine with direct events and a fake clock. Covers the lifecycle, the completion
  barrier, reordering, duplicates, the watchdog, failures, decisions, guards and restart recovery.
- `test_api.py`: HTTP + WebSocket through FastAPI's TestClient with the mock source.
- `test_mock_scenarios.py`: every mock scenario end to end at 40× speed.
- `test_models_repository.py`: validation, the detector-result contract, schema/WAL/constraints.
- `test_preview_bridge.py`: Phase 2 preview (nothing persisted) and the bridge's message conversions.
- `test_supervisor.py`: the supervisor with real processes and signals (fake detector executables).

Live check against the simulated core, real detectors and the real ROS graph (30 checks, ~45 s; isolated
ROS domain, scratch HOME): `tools/ui_verify/phase2_ros_check.sh` (see
[VERIFY_PHASE2.md](../docs/operator_ui/VERIFY_PHASE2.md)).

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
  0..n‑1 is **committed to SQLite**, and the backend's target count equals the detector's.
  Otherwise the scan is pending (watchdog: warning at 10 s, failure at 120 s) or failed with a reason.
  Reaching the expected count never completes or stops anything.
- **Never invented:** measurements are always `not_measured` (both detectors send -1), `passed` is
  ignored, `partial` comes only from the detector's `detail.partial`, and a hardware `ERROR` stop fails
  analysis. Restart turns unfinished scans into `interrupted`.
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
| `GET /api/v1/inspections/{uuid}/scans` | | history, oldest first |
| `GET /api/v1/scans/{scan_id}` | | scan with features |
| `POST /api/v1/scans/{scan_id}/decision` | `{decision: accept\|rescan}` | completed analyses only (including earlier undecided scans); idempotent |
| `POST /api/v1/scans/{scan_id}/failure-action` | `{action: retry\|rescan\|keep_incomplete}` | failed/interrupted only; `retry` needs verified raw data |
| `WS /ws` | | `snapshot` first, then events |

Errors: `{"error": {"code", "message", "field"}}` with 400 (invalid), 404 or 409 (wrong state).
**No endpoint starts or stops the physical scan.** `/api/v1/dev/mock/*` (press, release, scenario,
fault, hardware, layout) exist **only with `--mock`**.

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
  (`scan_seq`), so a missed stop is detectable as a gap. Phase 3's `ScanSessionState.msg` must carry it.
- `next_expected_count` starts empty for the very first inspection (no invented default); Ready
  requires it. Later inspections prefill the previous value.
- A scan whose acquisition ended in `ERROR` fails analysis after its results are stored (plan: an error
  never counts as success). Operator options: Rescan or Keep Incomplete.
- A scan started without an operator, an open inspection or a ready detector is stored as
  `unassigned` with the reason and an alert; its results are never attributed.

## Not yet

Tagged ROS messages, real finalization, recorded live scans and replay for Retry (Phase 3); CSV/USB export
and the designed screens (Phase 4); Pi/apt validation (Phase 5). Phase 2 cannot tell whether a detector
still has pending results when an inspection ends (no finalization yet); the detector is stopped with
SIGINT, which makes it flush its result files. Everything is verified on an x86 laptop (Humble) only.
