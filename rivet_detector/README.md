# rivet_detector

Finds **rivet heads** while a part is scanned and publishes each one **once**, as soon as the head and a 2 mm skin ring around it have been scanned. Heads may stand proud of the skin or sit below it. Both are found in one pass and the reported height is signed (negative = below the skin). numpy and scipy only (Pi 5, Jazzy). It is a separate package from `hole_detector` (same frame input, threading, outputs) and imports nothing from it.

With the laser's `test.sc1` setup the panel's heads read about 0.1 mm *below* the skin, and countersinks read as cones above it (the setup seems to flip Z). The older setup shows heads proud. Both work with the defaults.

## Run

```bash
# in the ros2_visualizer container; built together with hole_detector:
#   cd /root/hole_ws && colcon build --base-paths /bags/hole_detector /bags/rivet_detector --symlink-install
ros2 launch rivet_detector rivet_detector.launch.py rviz:=true      # rivets only
ros2 launch laser_hhrp detectors.launch.py mode:=both rviz:=true    # holes and rivets (mode default: hhrp.yaml)
ros2 bag play /bags/panel2_09112026_02 --topics /scan/frame
```

## Outputs

- **Topics:**
  - `/rivets/markers`, `/rivets/circles` and `/rivets/poses` (latched).
  - `/rivets/boundary`: raw cut-outs, one per message.
  - `/rivets/boundary_view`: all cut-outs at 0.3 mm, latched; RViz draws this one.
  - `/rivets/progress`: status line with scan, rivets, mm scanned, rejected and detect ms.
  - `/rivets/surface`: only with `viz_surface: true`.
  - `/inspection/result` with `feature_type: rivet` and `detail` JSON `id, d_head_mm, height_mm, rms_mm, edge_coverage, partial, scanned_share`.
  - Labels read `#3  Ø8.97  rivet` / `h 0.18  rms 0.03  cov 100%`.
- **Files:** `out_dir/rivet_scan_<time>/` holds `summary.csv` and `rivet_NN.pcd`: every raw point within the head edge + `boundary_margin`. A worker thread writes them, so detection never waits for the disk.

## Parameters (`config/params.yaml`, mm)

- `rivet_diam`: nominal head diameter, 9. `diam_tol`: accepted ± around it, 2.
- `min_height`: smallest |height| accepted, 0.04.
- `ring`: the skin ring beyond the head, 2.
- `period`: seconds between detection cycles, 0.2.
- `viz_surface`: false.
- The edge limits (`min_rim_coverage`, `max_roundness`, `min_scanned_share`) work as for holes.

## How it works

1. **Candidates.** A matched filter on a height image (just inside minus just outside the nominal edge) gives peaks of either sign. The head and its ring must lie on the part (MIN_ON_PART), which holes fail.
2. **Measurement.** Once a nominal head + ring is in the crop, each candidate is measured on the raw points in a box around it, against the skin ring. The skin is turned over for a sunk head. The edge is taken per 15° sector at half the head height, and a circle is fitted to those points.
3. **Gaps.**
   - A gap within 0.5 mm of the head edge (a hole, or the head clipped by the part's side) rejects the head.
   - A gap that reaches only the skin ring (a head at the edge of the laser's field) keeps it, flagged `partial`.
4. **Each cycle.**
   - Downsampled voxel rows the laser has passed are reused.
   - Reported heads are not measured again.
   - Candidates found flat on a settled skin fit (a crop of at least 30 mm) are not measured again either.

## Test

```bash
python3 /bags/rivet_detector/test/test_rivets.py      # about 3 min, ends with "all checks passed"
```

- **Synthetic panel:** batch, side clip, both signs (every head sunk instead of proud), incremental, raw boundaries, no NaN.
- **Panel 2 bags** (1280-point profiles, `/scan/frame`): 9 and 10 rivets, with partial ids pinned.
- **Panel bags** `panel_12112026` (640-point profiles, scan toward -y, rebuilt from `/scan/cloud`):
  - b_1904 gives 9 sunk heads; the one at y = -172 mm is partial (field edge).
  - a_1903 gives 0: holes only, their cones are not heads.
- **Latency** in simulated real time at the 0.2 s cycle:
  - Each rivet comes out < 0.5 s after the frame that completes it.
  - Four borderline heads are pinned at ≤ 0.8 s.
  - Cycle time: ≤ 150 ms median, ≤ 300 ms at the 99th percentile.
