# Operator UI: Phase 0 baseline

What the existing scan/detection stack does **before** any operator-UI change, measured so later
phases can detect regressions. Contract: [plan.md](../../plan.md). Reproduce everything below with:

```bash
tools/ui_baseline/run_baseline.sh [OUT_DIR]     # ~4 min; writes only below OUT_DIR
```

Exit code and final verdict: **0 PASSED** (every test ran and passed), **1 FAILED**, **3 INCOMPLETE** (nothing
failed but tests did not run because bags are missing; SKIP/NODATA are never counted as passes).
`HHRP_WS=<dir with build/ install/>` reuses an existing build; the C++ build is limited to 2 jobs.

The script builds into `OUT_DIR` (not the workspace), runs all tests, then runs the simulator with a
fake `HOME`, `ROS_DOMAIN_ID=87` and a loopback-only CycloneDDS config
([cyclonedds_loopback.xml](../../tools/ui_baseline/cyclonedds_loopback.xml)), so it touches neither
`~/laser_hhrp`, the real ROS graph nor the scanner network.

## 1. Measured on

| | |
| --- | --- |
| Code | `ui` @ `b65a78a` (plan commit on code baseline `87c5641`); `dev` / `main` untouched |
| Machine | dev PC, x86_64, **Ubuntu 22.04 + ROS 2 Humble**, CycloneDDS, Python 3.10 |
| Not available | Pi 5 / Jazzy target, scanCONTROL SDK (laser driver built without it), Nano, detector regression bags |
| Date | 2026-11-17 |

Nothing here was measured on the Pi; Phase 5 repeats it there.

## 2. Test results

| Suite | Result |
| --- | --- |
| Build (`laser_hhrp`, `hole_detector`, `rivet_detector`) | OK, 54 s. Warning: scanCONTROL SDK not found |
| `laser_hhrp` gtest `test_logic` | 8/8 passed |
| `laserHHRP/test/test_detectors_launch.py` (pytest) | 3/3 passed |
| `hole_detector` synthetic tests (13) | 13 passed |
| `hole_detector` bag tests (8) | not run: 3 NODATA, 4 SKIP, 1 rosbag open error (bags absent); `test_incremental_matches_batch` ×3 and `test_flap_from_frames` depend on them and were not reached |
| `rivet_detector` synthetic tests (9) | 9 passed |
| `rivet_detector` bag tests (3) | not run: 2 SKIP, 1 rosbag open error (bags absent) |

**The detector test scripts need care.** They are plain scripts (`python3 test/test_*.py`, `conftest.py`
hides them from pytest). Their `__main__` stops at the first missing bag, and several bag tests
**print `skip … not copied here` and return as if passed**. Use
[run_tests_each.py](../../tools/ui_baseline/run_tests_each.py) (called by the script): it runs each test
separately and reports `PASS` / `SKIP` / `NODATA` / `FAIL`. A detector change is only regression-checked
when the bag tests report `PASS`, which requires the bags below.

## 3. Bag replay

**Detector regression bags** are expected next to the packages, at the repository root (the ROS container
mounts them as `/bags`): `OneDrive_2_10-28-2026/rosbag2_2026_10_27-{14_45_24,16_27_45,16_33_35,16_35_10}`
(with `csk_3.pcd`), `flap1_05112026`, `panel_12112026/{a_1903,b_1904}`,
`panel2_06112026_1`, `panel2_09112026_02`, and `hole_detector_out/offline_runs`. They are **not on this PC**.

Replay into the live detectors (developer flow, unchanged):

```bash
ros2 launch laser_hhrp detectors.launch.py mode:=holes rviz:=true   # or rivets | both
ros2 bag play <bag> --topics /scan/frame
```

The bags carry `/scan/frame` but **no `/scan/status`**: a scan boundary is only visible as `index == 0`.
The Phase 3 legacy-bag adapter must derive scans from that, as the plan says.

**Not replayable:** `~/bags/hhrp_20261023_*` on this PC use the old `aric_laserhhrp_msgs/msg/ScanFrame`
(profile as `PointCloud2`, `sensor_msgs/Imu`), not `laser_hhrp/msg/ScanFrame`.

**Simulation** (no hardware, no bags), from the README:
`ros2 launch laser_hhrp hhrp.launch.py simulate:=true ui:=false scan_switch:=false`, then
`ros2 run laser_hhrp timed_scan.py 3`. The probe [probe_sim.py](../../tools/ui_baseline/probe_sim.py) adds
a release/listen window and a rapid re-press.

## 4. ROS interfaces the UI will read

| Topic / service | Type | Producer | Rate, QoS | Notes |
| --- | --- | --- | --- | --- |
| `scan/status` | `ScanStatus` | scan_controller | 5 Hz **and on each transition**; depth 10, reliable, volatile | `state` IDLE/SCANNING/PAUSED/ERROR, `calibration`, `faults[]`, `message`, `position_m`. `frames` comes from `scan/sync` and lags |
| `scan/frame` | `ScanFrame` | synchronizer | per frame (sim: ~120 Hz); depth 200 | `index` 0-based per scan, restarts at 0 on a new scan (not on resume) |
| `scan/sync` | `SyncStats` | synchronizer | 10 Hz | frame/missing counts of the current scan |
| `scan/storage` | `StorageStatus` | recorder | 2 Hz | `recording`, `session_dir`, `free_mb`, `write_failed` |
| `inspection/result` | `InspectionResult` | hole_detector, rivet_detector, **and inspection_node (stub, publishes nothing)** | once per feature; detectors depth 10 | see section 5 |
| `laser/status` / `nano/status` | `LaserStatus` / `NanoStatus` | drivers or simulator | 1 Hz / 2 Hz | controller start gate: laser < 3 s old and connected, Nano < 1.5 s old |
| `calibration/status` | `CalibrationStatus` | calibration_node | latched + 2 Hz | state CALIBRATION_REQUIRED/CALIBRATING/CALIBRATED |
| `holes/*`, `rivets/*` | RViz markers/clouds | detectors | latched | developer only; not a UI dependency |
| `scan/start`, `scan/stop`, `scan/pause`, `scan/zero`, `calibration/{start,reset,extrinsics}` | `std_srvs/Trigger` | controller / calibration | | `scan/start` refused unless CALIBRATED and both devices fresh |

Message definitions: [laserHHRP/msg/](../../laserHHRP/msg/). None carries a scan ID or an end-of-analysis event.

## 5. Detector outputs and "not measured"

| | hole_detector | rivet_detector |
| --- | --- | --- |
| `feature_type` | `"countersink"` (countersunk) or `"hole"` | `"rivet"` |
| `position_m` | local (x across laser line, y along scan, z) in metres, scan-local origin | same |
| `countersink_depth_m`, `flushness_m` | **always `-1.0`** = not measured | **always `-1.0`** |
| `surface_deviation_m` | fit RMS | fit RMS |
| `passed` | **always `true`** (placeholder, not a tolerance result) | **always `true`** |
| `detail` JSON | `id, d_top_mm, rms_mm, ring_coverage, countersunk, partial, scanned_share` | `id, d_head_mm, height_mm, rms_mm, edge_coverage, partial, scanned_share` |

`detail.id` is the scan-local feature ID, and `detail.partial` is the detector's own flag
(`min_scanned_share` 0.6 and `min_rim_coverage` 0.6 are acceptance thresholds in each `config/params.yaml`).
`height_mm` is geometry, not validated flushness. The UI must show **Not measured** and no PASS.

Files: each detector writes `<out_dir>/scan_<time>/summary.csv` + per-feature PCDs, creating the folder
**at the first feature** (a zero-feature scan gets no folder). Launch default `holes_out_dir` points at
`~/aric_ws/...`, a different workspace.

## 6. Observed behavior (simulation)

Probe run: calibrate, scan 3 s, release, listen 10 s; scan 3 s, release, re-press within 1 ms for 0.15 s,
release, listen 10 s.

| Observation | Evidence |
| --- | --- |
| `scan/start` before calibration is refused | `calibration required: hold the device still…` |
| Start/stop transitions publish immediately, each with its own stamp, including a re-press 0.7 ms after a stop | IDLE→SCANNING `…534.828857776` after SCANNING→IDLE `…534.828201093` |
| The synchronizer publishes frames **after** the stop reply; the stop reply's frame count lags | stop replies 352 / 355 / 7 frames; frames actually published 361 / 360 / 19 |
| The stop reply's `session_dir` can be stale (from 2 Hz `scan/storage`) | rapid scan 3 reported in `…103851`, actually recorded in `…103854` |
| A scan accepted right after start-up may record 0 frames | earlier run: `stopped: 0 frames` |
| **Two starts in the same second share one raw session dir, and the second truncates the first** | two scans of 30 frames each, both `session closed … 20261117-103920`; `frames.bin` = 175 560 B = 30 frames |
| Recorder `results.json` is written at stop | all sessions `[]` (simulator produced no detections) |
| Detectors keep processing buffered frames for ~1 cycle after stop; the previous scan's residue is handled on the next `index == 0` | detector logs; code `node.py` `on_frame` |
| **The simulator produces no detections** | its one rivet head is 20 µm proud; 0 results in all runs |

Detector cycle in simulation (x86 PC): 20–45 ms per 0.2 s cycle for both detectors running together.

## 7. Confirmed gaps carried into later phases

From code reading plus the runs above (no code changed in Phase 0):

1. Recorder drops results that arrive after stop: `add_result()` returns while the file is closed (not
   reproducible here, since the simulator has no detections; needs bags or hardware).
2. Detectors have no end-of-scan pass; the last scan's edge features wait for the next scan's `index == 0`.
3. Synchronizer flushes only `ready_` at stop; pairs inside the pairer are not published.
4. Same-second raw session collision with `frames.bin` truncation (reproduced, section 6).
5. `scan/status.frames`, the stop reply count and `scan/storage.session_dir` are not reliable per-scan
   identifiers or final counts (reproduced, section 6).

**On the Pi 5 (target):** the same script works on Jazzy/aarch64 (it sources `$ROS_DISTRO` or the
only `/opt/ros/*`). Stop the production service first (`sudo systemctl stop laser-hhrp`) so its CPU load
and GPIO use don't skew the numbers; the simulation uses domain 87 and `scan_switch:=false`, so it never
drives the real button or hardware. Expect a longer build. Copy the detector bags next to the packages
to get the bag results.

**Status after Phase 3:** gap 4 (same-second folder collision) is fixed, and so is a related gap the Phase 3
checks found: the recorder closed `frames.bin` before the synchronizer's flushed last frame(s) arrived. Gap 1
remains by decision (the UI database is the complete record). Gap 2 is solved in tagged mode (explicit final
pass); gaps 3 and 5 are made explicit by the stream-close count. See [VERIFY_PHASE3.md](VERIFY_PHASE3.md).

## 8. Limits of this baseline

- No detector bag regression ran: rerun `run_baseline.sh` where the bags are present (ROS container
  or Pi) before Phase 3 touches detector code, and record the bag results here.
- No late-result behavior observed: needs bags or hardware with real features.
- Humble on x86, not Jazzy on the Pi 5; timing and CPU numbers are not Pi numbers.
- A developer `CYCLONEDDS_URI` that binds the scanner-network interface makes every node fail with
  `failed to create domain` when that link is down. The script uses the loopback config instead.
