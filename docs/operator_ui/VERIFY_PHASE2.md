# Verifying Phase 2 yourself

> **Superseded by Phase 3** ([VERIFY_PHASE3.md](VERIFY_PHASE3.md)): live scans are now recorded, not previewed,
> and `phase2_ros_check` was replaced by `phase3_ros_check`, which covers the same supervisor checks.

Phase 2 = live ROS bridge (read-only) + one supervised detector per inspection, with live detections as a
**preview** (shown, never recorded or acceptable) until Phase 3 adds scan identity.

## A. Automated live check (~45 s, isolated)

```bash
tools/ui_verify/phase2_ros_check.sh                       # builds into a temp dir (C++ build: 2 jobs)
HHRP_WS=<dir with build/ install/> tools/ui_verify/phase2_ros_check.sh   # reuse a build, no C++ compile
```

It runs the **simulated core** (`simulate:=true holes:=false ui:=false scan_switch:=false`), the **real
backend** and **real detector processes** on ROS domain 88 with a loopback-only DDS config and a scratch
`HOME`, so it never touches your ROS graph, `~/laser_hhrp` or the scanner network. The switch is played by
calling `scan/start` / `scan/stop`, the same services `scan_switch_node` calls.

| Checks (30, all PASS on the laptop, 2026-11-17, twice in a row) | Plan |
| --- | --- |
| Backend up on live ROS; operator; rivet inspection created | §6 |
| `rivet_detector` ready only once its node subscribes `/scan/frame` and publishes `/inspection/result` | §6 |
| Exactly one `rivet_detector` node, no `hole_detector`; `/rivets/markers` still there | §1, §6, Phase 2 |
| Not ready until calibrated; Ready after the existing calibration service | §9 |
| SCANNING appears from the confirmed `scan/status` (not from a UI command) | §4, Phase 2 |
| Frame rate during the scan unaffected (119 of 120 frames/s) | §11.10 |
| Preview shows 3 rivets, the last one after release, all *Not measured* (synthetic results, since the simulator detects nothing) | §1, Phase 2 |
| **Nothing written to history; nothing can be accepted** | Phase 2 exit |
| End inspection stops `rivet_detector` (node and process gone); CSK starts exactly one `hole_detector`; `/holes/markers` there | §6, §11.8 |
| Detector output under the UI data root, per inspection | §6 |
| SIGKILLed detector → NOT READY with exit reason; **no automatic restart**; explicit restart works with a new process | §6 |
| A second `hole_detector` started outside the UI → NOT READY; Ready again once it leaves | §6 |
| Backend stopped with SIGTERM leaves **no detector process** | §6 |

Logs land in `OUT/` (`check.log`, `backend.log`, `core.log`, `ui/logs/<detector>.log`).

### What the first runs found (fixed, so this is not a first-try claim)

- **Supervisor bug:** after a detector was SIGKILLed, its dead node stays in the ROS graph until DDS times
  it out (~10 s). The supervisor took it for a foreign detector and refused the explicit restart. It now
  waits up to 20 s for its own crashed node to disappear (unit test
  `test_restart_after_crash_waits_for_the_dead_node_to_leave_the_graph`, and the live check).
- **Shutdown:** the `ros2 run` wrapper does not forward SIGTERM, so stopping it orphaned the server and
  its detector. The server itself shuts down cleanly on SIGTERM (detector stopped with exit 0). Phase 5's
  service must start the installed `lib/laser_hhrp_ui/server` directly.
- **Test machine safety:** a full-parallel C++ build exhausted RAM on a laptop without swap. Builds in the
  scripts are now limited to one package and 2 compile jobs, and can reuse an existing build.

## B. Unit tests

`cd ui_backend && python3 -m pytest -q test/` (88 tests). New in Phase 2:
`test_preview_bridge.py` (preview never persists; bridge freshness limits match the controller's 3 s laser
/ 1.5 s Nano gate; fault parsing; malformed results) and `test_supervisor.py` (real processes and signals
with fake detector executables: fixed argv, one process, mode switch stops first, crash not auto-restarted,
SIGINT → SIGTERM → SIGKILL escalation, foreign/duplicate nodes, start timeout, orphan cleanup that never
kills an unrelated PID).

## C. Hands-on (simulator, ~10 min)

```bash
source install/setup.bash                       # workspace with laser_hhrp, detectors, laser_hhrp_ui
ros2 launch laser_hhrp hhrp.launch.py simulate:=true holes:=false ui:=false scan_switch:=false   # terminal 1
ros2 run laser_hhrp_ui server --static src/laserHHRP-ros2/ui_web/dist --db /tmp/p2.db            # terminal 2
```

Open <http://127.0.0.1:8080>, identify, choose **Rivet Flushness**, and set the expected count.

| Do | Expect |
| --- | --- |
| `ros2 node list` (terminal 3) | `/rivet_detector` once, no `/hole_detector` |
| Screen | NOT READY: *Calibration required* |
| `ros2 service call /calibration/start std_srvs/srv/Trigger` | READY |
| `ros2 service call /scan/start std_srvs/srv/Trigger`, then `/scan/stop` | SCANNING with distance, then back to Ready with "Last scan (preview, not recorded)" and the notice |
| `ros2 topic pub --once /inspection/result laser_hhrp/msg/InspectionResult "{feature_type: rivet, position_m: [0, 0.02, 0], detail: '{\"id\": 1}'}"` | One rivet in the preview, *after release*, *Not measured* |
| End inspection, choose **CSK Depth** | `ros2 node list`: `/hole_detector` once, no `/rivet_detector` |
| `pkill -9 -f lib/hole_detector/hole_detector` | NOT READY: *hole_detector exited with code -9*; **Restart detector** button; it does not come back by itself |
| Tap **Restart detector** | Ready again after a few seconds |
| Ctrl+C terminal 2 | `pgrep -f lib/hole_detector` prints nothing |

## Not covered yet

Real detections from real hardware or bags; tagged scan identity, finalization and recorded history
(Phase 3); designed screens (Phase 4); the Pi with Jazzy (Phase 5).
