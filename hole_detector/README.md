# hole_detector

Finds holes (plain or countersunk, through or blind; the top rim is measured) while the part is scanned and publishes each hole **once**, as soon as the hole and its 5 mm surface ring have been scanned. It uses numpy and scipy only, so it runs on a Pi 5 with Jazzy.

- Design and the review notes: [../OneDrive_2_10-28-2026/PLAN_realtime_holes.md](../OneDrive_2_10-28-2026/PLAN_realtime_holes.md).
- Ported from `hole_segmentation.py` (v2). Only the top circle is fitted; the bottom (through-hole) circle was removed on request.

## Build and run

```bash
# in the ros2_visualizer container (/bags is read-only, so build outside it)
source /opt/ros/jazzy/setup.bash && source /laser_ws/install/setup.bash
mkdir -p /root/hole_ws && cd /root/hole_ws
colcon build --base-paths /bags/hole_detector /bags/rivet_detector --symlink-install
source install/setup.bash

ros2 launch hole_detector hole_detector.launch.py rviz:=true     # holes only
ros2 bag play /bags/flap1_05112026 --topics /scan/frame
```

**Holes, rivets or both.** Rivet heads are found by the separate package `rivet_detector` (next to this one). `ros2 launch laser_hhrp detectors.launch.py [mode:=holes|rivets|both] [rviz:=true]` starts the chosen detectors without the laser hardware (for bags); `mode` defaults to `detection.mode` in `laser_hhrp`'s `config/hhrp.yaml`. `hhrp.launch.py` includes it and passes `mode:=` through.

**Input.** By default (`input_type: frame`) the node builds the cloud itself from the scanner's `/scan/frame` profiles. Each valid point becomes (x, position_m, z), as in `laser_hhrp`'s `point_cloud_node`. Detection runs every `period` (0.2 s) on the newest strip, and points it no longer needs are dropped. A hole is published within about 0.4 s of the laser passing its far edge + ring + guard (laptop; per cycle 25-90 ms median).
- Per cycle: downsampled voxel rows the laser has passed are reused, full-resolution points are used only in a box around each gap, and a gap that matches a hole already reported is not measured again.
- Rows and PCDs are written by a worker thread, so detection never waits for the disk; the node drains it on shutdown.
- This is complete: the Pi's `/scan/cloud_view` lost most of the flap scan, while `/scan/frame` gave all 20 holes.
- It is also light: 1,278-point messages at 80 Hz instead of a cloud of up to 115 MB every second.
- Frames are received on their own thread, so a slow detection doesn't drop profiles.

For an older bag that only has the stitched cloud, set `input_type: cloud` and `input_topic: /scan/cloud_view`:
```bash
ros2 launch hole_detector hole_detector.launch.py  # after editing config/params.yaml, or:
ros2 run hole_detector hole_detector --ros-args --params-file <params.yaml> -p input_type:=cloud -p input_topic:=/scan/cloud_view
```

**In the laser roller.** In the laserHHRP-ros2 repo, `ros2 launch laser_hhrp hhrp.launch.py` starts this node (`holes:=false` to leave it out), and the operator UI shows its displays. Which displays start shown: `laserHHRP/config/rviz_displays.yaml`. On the Pi, run the UI on a PC (`ui:=false` on the Pi): RViz on the Pi competes with detection for CPU.

**Where results go.** Started from `hhrp.launch.py`, each scan is saved inside this package, in `~/aric_ws/src/laserHHRP-ros2/hole_detector/hole_detector_out/scan_<time>/` (launch argument `holes_out_dir:=` to change it). Started on its own, they go to `out_dir` in `config/params.yaml` (`~/hole_detector_out`), or `out_dir:=` on the launch line.

## Topics (frame `laser`)

| Topic | Content |
|---|---|
| `/holes/markers` | Top ring, centre sphere, label `#id  <top diameter in mm>` (+ `(partial)`). Latched |
| `/holes/circles` | Fitted top circles of all holes, as points with a `hole_id` field. Latched |
| `/holes/boundary_view` | Every cut-out of the scan, downsampled to 0.3 mm, in one latched cloud: what RViz draws (the raw ones are ~100k points each) |
| `/holes/boundary` | Per new hole: every raw (not downsampled or filtered) point within the top radius + `boundary_margin` (default 5 mm) of the hole axis, with `hole_id`. Only the floor seen through the hole (deeper than `max_depth`) is cut |
| `/holes/poses` | Hole centres; the z axis is the local surface normal. Latched |
| `/holes/progress` | Decided / laser lines and the status: scan, holes, mm scanned, rejected, detect ms |
| `/holes/surface` | The decided strip coloured by depth; only with `viz_surface: true` (default false: costs CPU on the Pi) |
| `/inspection/result` | `laser_hhrp/InspectionResult` per hole. `feature_type` is `countersink` or `hole`; `detail` is JSON: `id, d_top_mm, rms_mm, ring_coverage, countersunk, partial, scanned_share`. `-1` means not measured (countersink depth, flushness) |

Parameters are in `config/params.yaml` (mm). Each cloud logs its timing: `convert / detect / publish ms`.

| Parameter | Use |
|---|---|
| `hole_type` | `any`, `countersunk` (a cone wall inside the rim) or `plain` |
| `min_hole_depth` | A gap whose middle is neither empty nor this deep is a dent (mm) |
| `min_rim_coverage`, `max_roundness` | Rim acceptance: covered share of the scanned rim, fit RMS / radius |
| `min_scanned_share` | Share of the rim outside unscanned bands; holes below 1 are flagged `partial` |
| `max_depth` | Deepest point counted, for the hole tests and the boundary cut (mm) |
| `boundary_margin` | Width of the saved boundary cut-out around the top rim (mm, default 5). Output only: the accepted holes and their measurements do not change; only `/holes/boundary` and the saved `hole_NN.pcd` do. Complete up to `ring` + `guard` = 8 mm, except for a hole at the very start of the scan (nothing was scanned before it); wider cut-outs can be clipped at the crop ends |

**Unscanned bands.** The stitched cloud can drop whole scan lines (on the Pi, `flap1_05112026`). Lines with under 20% of the median point count are "unscanned". They are filled from their neighbours, so a band is never taken for a hole, and rim points next to them are ignored. A hole a band cuts through is still measured from the rest of its rim and flagged `partial`. A hole mostly inside a band can't be measured.

## Test

```bash
python3 /bags/hole_detector/test/test_detector.py
```

The test checks:
- a synthetic plate: plain, countersunk and blind holes (3–26 mm), a band through one hole and beside another, and a dent. Every hole is found once with the right type and partial flag, the dent is not, and the result is the same when fed incrementally;
- holes per scan on every real bag (csk_1–3, holes_1, flap), from `/scan/cloud_view`;
- the flap from `/scan/frame` through the node's frame buffer: 20 holes, each once;
- batch detection vs `out/csk_3/summary.csv`;
- replaying every real bag cloud (also with every 2nd and every 3rd one dropped) gives each hole exactly once;
- a hole next to the scan start is found;
- an empty cloud is ignored;
- `boundary_margin` widens only the cut-out: the same holes and measurements, raw points, and an 8 mm cut-out reaches 8 mm;
- the panel bags (640-point profiles, scan toward -y): 13 and 12 holes;
- latency: every bag replayed in simulated real time at the 0.2 s cycle (`test/frames.py`; cloud-only bags are rebuilt into profiles): each hole < 0.5 s after the frame that completes it (one pinned re-checked rejection in holes_1), cycle <= 150 ms median, <= 300 ms at the 99th percentile;
- the PCD writer drains on close; the launch file starts one node; the RViz layout draws no full clouds.
