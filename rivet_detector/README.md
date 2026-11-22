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

**Tagged mode (operator UI, `tagged: true`; off by default).** Started by the UI's detector supervisor. Input
`/scan/frame_tagged` (transient local: frames published before the subscription was matched arrive from the
synchronizer's history; frames exposed before the node started are ignored) + `/scan/stream_closed` instead of
`/scan/frame`; each scan UUID gets its own tracker and
`out_dir/<scan UUID>/` (made at once, also for a scan with nothing found). When the stream close has arrived
and frames 0..n-1 are all in, the last frames get their detection pass, the files are flushed and exactly one
`/inspection/detection_finalized` is published, without waiting for a next scan. Results also go to
`/inspection/result_tagged` (numbered per scan). Missing frames fail the scan after `final_wait_s` (10 s);
a frame stream closed with an error fails it too. A result file (CSV row, PCD) or scan folder that cannot be
written also fails the scan, with the reason in `DetectionFinalized.error`. The legacy `/inspection/result` is still published.

## Parameters (`config/params.yaml`, mm)

- `rivet_diam`: nominal head diameter, 9. `diam_tol`: accepted ± around it, 2.
- `min_height`: smallest |height| of a head without a seam, 0.04. `min_groove` (DEFAULTS, 0.05): smallest seam depth.
- `ring`: the skin ring beyond the head, 2.
- `period`: seconds between detection cycles, 0.2.
- `viz_surface`: false.
- The edge limits (`min_rim_coverage`, `max_roundness`, `min_scanned_share`) work as for holes.

## How it works

1. **Height image.** The crop's skin, as a height image (0.3 mm pixels). Each image row (along the scan) is
   corrected by its line-to-line jitter: the row median minus its running median over 3 mm. A hand-held roll
   rocks a whole scan line by up to 0.2 mm, as much as a head or its seam.
2. **Gaps.** Empty areas over ~1 mm are gaps (holes, the part's sides), except:
   - laser dropout: shiny metal loses a fixed stretch of the laser line (glare), a sparse stripe along the scan;
     its empty runs along the scan are under 3 mm (`DROPOUT_RUN`), a bore is longer;
   - beyond the laser's field across the scan: unscanned, not a gap.
3. **Candidates**, from two matched filters, each of either sign:
   - **seam** (groove): a narrow ring at the nominal edge that lies off both the head inside it and the skin
     outside it. A countersunk head's seam shows whether the head is proud, sunk or perfectly flush; this is the
     one that finds flush heads (2026-11-22: heads 0-0.08 mm below the skin, with no height step to speak of).
   - **step**: just inside minus just outside the nominal edge (proud or sunk heads without a seam).
   The head and its ring must hold (nearly) no gap (MIN_ON_PART), which holes fail.
4. **Measurement**, on the raw points around the candidate, against its own skin ring:
   - seam: per 15° sector the radius where the radial profile peaks between 0.6 and 1.35 × the nominal radius,
     at least `min_groove` / 2 off both the head and the skin. Searched around the nominal edge every pass, so
     the fit never moves in to the mandrel's groove. Reported diameter = the seam's (~8.1 mm on these heads).
   - step: the edge per sector at half the head height. A stepped head that also shows a seam is measured on
     the seam (one edge definition).
   A circle is fitted to the edge points; centre and radius settle in up to three passes.
5. **Acceptance.** Diameter within `rivet_diam` ± `diam_tol`; edge rms ≤ 0.03 × radius and ≥ 70% of the scanned
   edge sectors covered (or the stricter configured limits): real heads score ≤ 0.02 and ≥ 0.75, the rocking at
   the field's edge lines up into ≥ 0.03 and ≤ 0.7. Both 60° ends along the scan must hold edge points (or be
   unscanned).
   - A gap within 0.5 mm of the head edge (a hole, or the part's side inside the field) rejects the head.
   - A head running out of the laser's field is measured on the part it shows, flagged `partial` (sectors
     beyond the field count as unscanned).
6. **Each cycle.**
   - Downsampled voxel rows the laser has passed are reused.
   - Reported heads are not measured again; a second measurement of a head (heads never overlap) is not a new one.
   - A candidate found to be no head on a settled skin fit (a crop of at least 30 mm) in 3 crops is not
     measured again.
   - Final pass (the scan's stream closed, every frame in): heads up to the scan's last line are decided, without
     the guard for points still to come.

## Test

```bash
python3 /bags/rivet_detector/test/test_rivets.py      # about 3 min, ends with "all checks passed"
```

- **Synthetic panel:** batch, side clip (a part side inside the field rejects; the end of the field gives partial
  heads at most), both signs (every head sunk instead of proud), incremental, raw boundaries, no NaN.
- **Rows of 2026-11-22** (`test_rows_20261122`, bags `pi_20261122/` under `HHRP_BAGS`, Pi, 640-point profiles):
  all 41 flush heads of 6 scans found exactly once, seam Ø 7.7–8.5 mm, at 0.2 s and 1.5 s detection cycles; none
  on the two hole rows.
- **Panel 2 bags** (1280-point profiles, `/scan/frame`): 9 and 10 rivets, with partial ids pinned.
- **Panel bags** `panel_12112026` (640-point profiles, scan toward -y, rebuilt from `/scan/cloud`):
  - b_1904 gives 9 sunk heads; the one at y = -172 mm is partial (field edge).
  - a_1903 gives 0: holes only, their cones are not heads.
- **Latency** in simulated real time at the 0.2 s cycle:
  - Each rivet comes out < 0.5 s after the frame that completes it.
  - Four borderline heads are pinned at ≤ 0.8 s.
  - Cycle time: ≤ 150 ms median, ≤ 300 ms at the 99th percentile.
