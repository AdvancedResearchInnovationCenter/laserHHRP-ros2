# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A hand-held laser roller for aircraft-skin inspection: a scanCONTROL LLT2900 laser line profiler, an
AMT222C encoder and an MPU6050 IMU (read by an Arduino Nano) on a roller, run by a Raspberry Pi 5. Profiles
are placed along the scan axis into a point cloud; detectors find countersinks / holes and rivets during the
scan; an operator UI (touchscreen, 800 × 480) runs inspections.

| Folder | Package | What |
| --- | --- | --- |
| `arduino_nano/` | firmware | encoder, IMU, laser trigger; serial protocol shared with `laserHHRP/include/laser_hhrp/serial_protocol.hpp` |
| `laserHHRP/` | `laser_hhrp` (C++, ament_cmake) | drivers, sync, odometry, calibration, recorder, scan controller, RViz panel, messages |
| `hole_detector/`, `rivet_detector/` | Python | feature detection during the scan (numpy + scipy) |
| `ui_backend/` | `laser_hhrp_ui` (Python, FastAPI) | operator UI backend: lifecycle engine, SQLite, ROS bridge, detector supervisor |
| `ui_web/` | (npm, not colcon) | React + TypeScript + Vite screens, served as built `dist/` by the backend |
| `tools/ui_baseline/`, `tools/ui_verify/` | | regression baseline, live checks, browser checks, mutation checks |

Key docs: `README.md` (hardware setup and everyday use), `laserHHRP/README.md` and `laserHHRP/docs/<node>.md`
(per node), `plan.md` (operator UI execution contract), `handover.md` (UI state as of 2026-11-18),
`docs/operator_ui/VERIFY_PHASE*.md` (evidence and how to re-check), `ui_backend/README.md` (API v1,
deviations from plan), `ui_web/README.md` (map contract, layout rules),
`docs/operator_ui/MEASUREMENT_INTEGRATION.md` (plug-in contract for the measurement algorithms),
`docs/DATA_FORMAT.md` (what each scan leaves on the device: raw recording + per-feature legacy layout).

## History in brief

- **2026-10-27** `laser_hhrp` package and Nano firmware. **11-09** Pi copy taken as baseline. **11-11–13**
  hole detector, split into `hole_detector` / `rivet_detector`, GPIO scan switch, Pi defaults (`dev` branch work).
- **11-17–18** Operator UI v0.1 on branch `ui`, phase by phase per `plan.md` §10: Phase 0 baseline, 1 backend
  + SQLite + mock, 2 ROS bridge + detector supervisor, 3 scan identity and explicit finalization (six additive
  messages, detectors' opt-in `tagged:=true`), 4 screens + CSV/USB export. Two external code reviews, both
  fixed with regression tests (`e6a2c81`, `aab91a0`).
- **11-18–19** Phase 5 started on the Pi: all checks passed there; UI built and run from the Pi's `~/aric_ws`;
  tilt warning turned off (`003bc4b`); kiosk cache fix (`0b28f31`); Setup mode figures, "Back to <operator>",
  light theme (`be64988`).
- **11-20** Measurement algorithms (CSK depth, rivet flushness) to arrive as a git submodule; contract in
  `MEASUREMENT_INTEGRATION.md`; work on branch `inspection-algorithms` (from `be64988`).
- **11-21** Both algorithms integrated: submodules in `laserHHRP/third_party/`, run by `inspection_node`; GO /
  NO-GO on the screens (`VERIFY_MEASUREMENT.md`). Operator kiosk on the Pi (`deploy/kiosk/`).
- **11-22** Whole stack tested on four bags recorded on the Pi (`VERIFY_REAL_BAGS.md`, `handover.md` §0): rivets
  found by their seam (were 6/41, now 41/41), field-edge / final-pass fixes, flushness z check off, odometry
  re-anchoring, one BLAS thread (live count), no false speed / vibration / slip warnings, Results stepper and
  Accept / Rescan hint; replay tool `tools/replay/` (laptop, Pi, with a browser or a person as operator). The user
  operated all four bags on the Pi; then `scan_distorted` for a feature the device rocked over (`a4c37c3`) and the
  map in the operator's view (+x left, +y up) with zoom / pan on Results (`a60276f`). For field data collection:
  per-feature legacy files after every scan and the Ready screen's "Rec bags" switch (one `ros2 bag record -a`
  per scan) (`9637540`, `393591c`, `6b0521d`; `docs/DATA_FORMAT.md`).

## Branches and process

- Never touch `dev` or `main`, never force-push (`plan.md` §12). UI work is on `ui`; measurement
  integration on `inspection-algorithms`.
- Commit and push only when the user asks. One focused commit per change or phase; each phase gets a
  `docs/operator_ui/VERIFY_*.md` with how to re-check it.
- Do not install dependencies or change system state on the Pi without asking.

## Build

Laptop: Ubuntu 22.04 + ROS 2 Humble, x86. Target: Pi 5, Ubuntu 24.04 + ROS 2 Jazzy (Python 3.12, FastAPI from
apt with **pydantic 1**, so request bodies are validated in plain Python, not pydantic).

```bash
git submodule update --init                               # the measurement algorithms (laserHHRP/third_party)
# from the colcon workspace root (src/ contains this repo)
MAKEFLAGS=-j2 colcon build --parallel-workers 1          # laptop has no swap: a full-parallel C++ build OOMs
colcon build --packages-select laser_hhrp_ui              # backend only
cd ui_web && npm ci && npm run build                      # frontend -> ui_web/dist (gitignored); no Node on the Pi
```

`hhrp.yaml` and other configs are **copied** at build time: rebuild `laser_hhrp` after editing
`laserHHRP/config/hhrp.yaml`. The scanCONTROL SDK is optional (without it the laser driver is stubbed; the
simulator still works).

## Run

```bash
ros2 launch laser_hhrp hhrp.launch.py simulate:=true scan_switch:=false holes:=false ui:=false  # core, simulated
ros2 run laser_hhrp_ui server --static ui_web/dist                 # backend + screens on http://127.0.0.1:8080
cd ui_backend && python3 -m laser_hhrp_ui.server --mock --static ../ui_web/dist --db /tmp/ui.db   # no ROS at all
```

- With the operator UI, start the core with `holes:=false ui:=false`: the backend starts exactly one detector
  itself (in tagged mode) for the inspection's mode.
- Without the GPIO switch: `ros2 service call /scan/start std_srvs/srv/Trigger` (and `/scan/stop`,
  `/calibration/start`). The simulator detects nothing (its rivet is 20 µm proud).
- For a service or script, start `install/laser_hhrp_ui/lib/laser_hhrp_ui/server` directly: the `ros2 run`
  wrapper does not forward SIGTERM and orphans the server and its detector.
- On the Pi: `~/aric_ws/start_simulation.sh` and `~/aric_ws/start_hardware.sh` (core + backend + Chromium kiosk;
  these scripts live only on the Pi). For operators: `deploy/kiosk/` (`sudo ./install.sh`): GDM autologin into a
  cage session with only the operator screen; core, backend and Chromium restarted if they exit. The simulator script keeps its HOME in `~/aric_ws/operator_ui/simhome`
  so it never touches the device calibration in `~/laser_hhrp/calibration.yaml`.

## Tests

| What | Command |
| --- | --- |
| Backend (~150 tests) | `cd ui_backend && python3 -m pytest -q test/` · one test: `-k <name>` |
| Frontend (all suites, one runner) | `cd ui_web && npm test` |
| C++ logic (gtest) | `colcon test --packages-select laser_hhrp && colcon test-result --verbose`, or `build/laser_hhrp/test_logic --gtest_filter=<Suite.Name>` |
| Detector tests (scripts, not pytest) | `python3 hole_detector/test/test_detector.py` / `rivet_detector/test/test_rivets.py`; one test: `python3 tools/ui_baseline/run_tests_each.py <script> "<test_a>;<test_b>"` |
| Do tests catch planted bugs? | `python3 tools/ui_verify/mutation_check.py`, `python3 tools/ui_verify/web_mutation_check.py` |
| Screens in a real browser (mock) | `tools/ui_verify/phase4_screens.sh [OUT]` (`SKIP_BUILD=1`, `BROWSER=chromium` on the Pi) |
| Live ROS checks | `HHRP_WS=<ws with build/ install/> tools/ui_verify/{phase4_live,phase3_ros_check,phase3_identity_check,phase3_detector_check}.sh` |
| Measurement node on the algorithms' test folders | `HHRP_WS=<ws> tools/ui_verify/measurement_check.sh` |
| Legacy regression baseline | `tools/ui_baseline/run_baseline.sh` (exit 0 pass / 1 fail / 3 some tests did not run) |
| Whole stack on a device bag (no drivers, real data) | `tools/replay/replay_stack.sh BAG MODE COUNTS OUT [calibration.yaml]` (`VERIFY_REAL_BAGS.md`) |
| Detectors on the 2026-11-22 rows | `HHRP_BAGS=<dir with pi_20261122/> ... run_tests_each.py <script> "test_rows_20261122()"` |

- Many detector tests need bags (`OneDrive_2_10-28-2026/`, `panel*`, `flap1_05112026`) that are not in the
  repo; they report SKIP / NODATA, which is **not** a pass. Baseline is "INCOMPLETE, 0 failures" without them.
- Live scripts isolate themselves (own ROS domain, scratch HOME, loopback DDS config
  `tools/ui_baseline/cyclonedds_loopback.xml`). If the laptop's `CYCLONEDDS_URI` points at the scanner
  interface and that link is down, every node fails with `failed to create domain`.
- Browser checks drive Chromium-family browsers over the DevTools protocol (Brave on the laptop, snap
  Chromium on the Pi); they tap like a finger and enforce layout rules on every screen (exactly 800 × 480,
  no scrolling, touch targets ≥ 48 px, nothing clipped). Playwright is not used.

## Architecture (the parts that span files)

**Acquisition.** `scan_switch_node` (GPIO) → `scan/start|stop` services → `scan_controller` → `scan/status`
drives the synchronizer, point cloud and recorder. `scan/status` is the **only** acquisition control path;
everything added for the UI is metadata.

**Scan identity (Phase 3).** The controller publishes `scan/session_state` (scan UUID + the exact
`scan/status` stamp). The synchronizer publishes `scan/frame_tagged` and `scan/stream_closed` next to the
unchanged `scan/frame`. Detectors in tagged mode publish `inspection/result_tagged` and one
`inspection/detection_finalized` per scan after all their files are on disk. The recorder publishes
`scan/recording_session`. Identity is matched by the exact status stamp, never by time or "latest UUID".

**Backend.** ROS thread (`ros_bridge.py`, read-only) → bounded `ingress` queue → `state.Engine` (single
thread: the authoritative lifecycle and completion barrier) → SQLite (`repository.py`, migrations via
`PRAGMA user_version`) + WebSocket/HTTP (`api.py`, `hub.py`) → browser. `detector_supervisor.py` runs one
allowlisted detector process per inspection, output in
`~/.local/share/laser_hhrp/ui/detector-output/<inspection>/<scan>/` (summary.csv + one PCD per feature).
Side jobs that never touch a scan's lifecycle: `recording_export.py` (after each analysis, `export_legacy.py`
writes the per-feature legacy layout into the scan's raw folder `~/laser_hhrp/scans/<date-time>/`) and
`bag_recorder.py` (the "Rec bags" switch: one `ros2 bag record -a` per scan in `~/laser_hhrp/bags/`, opened on
Ready, closed after the analysis; `Engine._bag_sync` on every watchdog tick).

**Device actions.** `device.py`: gyro calibration (`calibration/start`) and power off (`--power-off-command`) from
the screens, so the device needs no terminal (approved deviation from plan §3). The Engine refuses both during a
scan, power off also while an analysis is pending. Scan start / stop stays on the physical switch. Turn off is in
the header's ⋮ menu; developers get ⋮ → Service… (PIN file, desktop mode, restart, network) in the kiosk.

**Completion rules (each guarded by a test and a mutation).** A scan is complete only with a confirmed stop,
stream close and detector finalization with equal frame counts, every declared `result_seq` committed,
target counts agreeing, and `inspection/measurement` covering exactly the target features. Timeouts (warn 10 s, fail 120 s), crashes, missing frames, conflicting terminal
messages or an unconfirmed stop end as failed/interrupted, never success. Rescans never overwrite; an
undecided scan stays UNDECIDED. No web endpoint starts or stops a scan.

**Frontend.** A view of the backend snapshot: `snapshot.screen` picks the screen; History is the only local
navigation. It never derives lifecycle, counts or target filtering. Pure logic (map geometry, circles,
wording) is in `ui_web/src/lib/`. Theme via `data-theme` on `<html>`, colours only through the CSS tokens in
`styles.css` (dark and light). The backend serves `index.html` with `no-cache` and hashed `assets/` as
immutable.

**Measurement.** `inspection_node` (C++) takes each successful `detection_finalized` (its `output_dir` is the
scan folder), measures every target on 4 threads with the submodule algorithms (`third_party/laser_csk_inspection`
`csk_depth/`, `third_party/rivet_flushness`; contract `MEASUREMENT_INTEGRATION.md`) and publishes one
`MeasurementFinalized` with the GO range from `hhrp.yaml` `inspection.*`, plus `measurement.csv` in the folder.
The backend judges GO / NO-GO (inclusive range); the detectors' own -1 values are never used. Partly scanned
features get reason `partial`. A wrong `inspection.*_invert_z` shows as `unexpected_shape`, not a wrong value.

## Traps

- Device tilt is relative to the gyro-calibration pose, not the part; its warning is disabled via
  `system.tilt_limit_rad: 3.2` in `hhrp.yaml`.
- The direction of z in the scan cloud depends on the scanner's `.sc1` configuration: do not hard-code it.
- Background jobs of a non-interactive bash script ignore SIGINT (use SIGTERM); `pkill -f <pattern>` can
  match the shell running it; a SIGKILLed node stays in the ROS graph ~10 s.
- Scripts that move `HOME` hide pip `--user` packages: export `PYTHONUSERBASE=$HOME/.local` first.
- The 3 mm hole on the synthetic test plate sits at `max_roundness` 0.08 and is found or not depending on
  cycle timing; detector accuracy is out of scope for the UI work.
- Old bags in `~/bags/hhrp_202610*` use the previous `aric_laserhhrp_msgs` types and cannot be replayed.
- Humble's rosbag2 cannot read a Jazzy bag's `metadata.yaml` (and `ros2 bag play` fails on its QoS): open the
  `.mcap` file itself (`tools/replay/raw_replay.py`, the detectors' `bag_scans`).
- The laser loses a fixed stretch of its line on shiny metal (x ≈ −8…+2.5 mm with test.sc1): features sit in a
  sparse stripe. Empty runs along the scan under 3 mm are dropout, not gaps (rivet detector).
- `pkill -f <pattern>` / `pgrep -f` from a tool shell match that shell's own command line: bracket one character
  (`hhrp-ui-browse[r]`). Browser checks can leave headless browsers and ~130 MB profiles in the temp folder.
- The Results map is the operator's view from behind the device: +x **left**, +y **up** (laser +x is the operator's
  left, checked on the device 2026-11-22); `plan.md` §9 still says x right / y down (deviation noted there).
- `ros2 bag record` on Jazzy prints "Recording..." ~1 s before it subscribes: readiness (REC on screen) is the
  subscription to `/laser/raw`, `/nano/encoder`, `/nano/imu`. The bag runs in its own process group with
  PR_SET_PDEATHSIG (SIGINT) so it closes cleanly even if the backend is killed. Play large bags with
  `ros2 bag play --read-ahead-queue-size 12000`.
- `frames.bin` is every laser line as placed (points, encoder, IMU at the line, time), not every sensor stream
  (no 240 Hz IMU / encoder between lines, no calibration values, no laser intensity anywhere): bags for that.
- `tools/replay/replay_stack.sh` starts the backend without Turn off / Restart / Service PIN, so its screens show
  no ⋮ menu (on purpose: a test run cannot power off the Pi). The kiosk has it.
- The odometry pose (`base/pose`, `scan/pose`) is not trustworthy yet; nothing measured uses it
  (`docs/odometry_node.md`, known limitation). The slip warning is off in `hhrp.yaml`.
