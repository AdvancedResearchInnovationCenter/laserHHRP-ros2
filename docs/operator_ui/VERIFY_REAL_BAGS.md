# Verify: the whole stack on the device's own recordings (2026-11-22)

Four bags recorded on the Pi with `ros2 bag record -a` while `hhrp.launch.py` ran, after the gyro and the extrinsic
calibration (`metres_per_count` 5.33e-5): `rivets` (5 scans), `rivets2` (1 long scan), `holes1`, `holes2` (1 scan
each, the same row of holes). About 2.2 GB, not in the repo (copies: the Pi's home, and the SSD used for this work).
Checked component by component, then end to end, on the real data only.

## Results

| Component | Check | Result |
| --- | --- | --- |
| Laser | profile counter, timing | 120.2 profiles/s, no counter gap in any bag, stamp jitter < 0.1 ms |
| Nano | encoder, IMU | 242 Hz each, no gap > 8 ms, no read error; clock map jitter ~2 ms |
| Synchronizer | frames per scan | no index gap, no flags, forward rolls; spacing 0.05–0.48 mm (up to 52 mm/s) |
| Geometry | holes round, pitch | top Ø 8.7–9.1 mm, round in x and y (the new encoder scale); pitch 21.1–21.3 mm |
| Hole detector | 35 countersinks, 2 scans | all found once, at 0.2 s and 1.5 s cycles; the one running out of the laser field: partial |
| Rivet detector | 41 flush heads, 6 scans | was 6 / 41; now 41 / 41 once each, at 0.2 / 1.0 / 1.5 s cycles; none on the hole rows |
| Full pipeline | `tools/replay/replay_stack.sh`, 8 scans | every expected count met (76 features), every analysis complete |
| CSK depth | 33 / 35 measured | 1.14–1.24 mm (median 1.21): NO-GO against 0.947–1.147. Repeatability holes1 vs holes2, 13 holes: mean diff −0.006 mm, sd per scan ~0.012 mm |
| Flushness | 28 / 41 measured (was 10) | −0.003 to −0.056 mm (sunk), all GO; the rest: partial 3, scan_distorted 3, unexpected_shape 6, too_few_points 1 |

What was wrong and is fixed (tests in brackets):
- **Rivets were found by their height step**; these heads sit flush (0–0.08 mm sunk) and have none. Now by the seam
  around the head, through the laser's glare stripe, the line-to-line rocking of the roll and the field's edge
  (`rivet_detector` `test_rows_20261122`, `test_side_clip`).
- **Hole at the laser field's edge**: its straight cut was fitted as rim (Ø 11.6 mm), and a second measurement of
  the same hole with another diameter counted as a new hole. Now partial, once (`hole_detector` `test_rows_20261122`).
- **Features just before the scan's end** were never decided (the guard for points still to come). The tagged
  node runs a final pass once every frame is in (phase3_detector_check, both detectors).
- **Flushness refused 2 of 3 correct heads** (`unexpected_shape`): the library's z check reads a mandrel groove
  these rivets don't show. `inspection.rivet_check_orientation: false` (z is fixed by the `.sc1`, same for CSK).
- **Odometry ran away** (pose km off on 0.5 m rolls) and raised a false "roller slip" warning 10–20× per scan: a slip
  longer than 1 s now re-anchors the filter to the encoder and restores the encoder's distance (gtest
  `Odometry.*`); the operator warning is off (`system.slip_warning: false`) until the filter is validated.
  Frame positions never used it.
- **A hole the device rocked over read as a bad hole** (holes2 hole 5, `failed` / `unexpected_shape` in every run):
  the operator paused on it, the roller stood at one encoder count for 0.1 s while the device tipped (skin 0.7 /
  0.3 mm low either side), and the tilted lines stacked at one position. `inspection_node` now checks the lines
  crossing each feature for a height jump (> 0.1 mm, `inspection.max_line_jump_m`) and reports `scan_distorted`
  ("Scan distorted: rescan"). 100 features of the four bags: good ones ≤ 0.031 mm, this hole 0.27–0.30 mm; a stop
  out on the skin (holes1 hole 12) does not count (gtest `MeasurementInput.*`; `measurement_check.sh` 0 failures).

Seen, not changed:
- **Laser dropout stripe** at x ≈ −8…+2.5 mm in every scan (13–60% valid, specular glare on bare aluminium,
  exposure 3000 µs): it runs through the features' middle. The detectors now cope; a lower exposure or a slight
  tilt of the scanner would give more data there.
- **CSK depths all ~0.06 mm above the GO maximum**: consistent across holes and scans; needs one gauge reading to
  tell a deep row from a bias.
- **Scale**: hole pitch 21.27 mm in holes1 (12.8 mm/s) and 21.11 mm in holes2 (32 mm/s), 0.7% apart.
- **Pi detection time**: 0.5–1.5 s per cycle on the Pi during the recording (RViz running too) vs ~0.1 s on the
  laptop; results no longer depend on it (1.5 s cycles tested), only how soon a feature appears.

## With the operator screens (UX)

`UX=1 tools/replay/replay_stack.sh ...`: an operator in headless Chromium at 800 × 480 identifies, starts the
inspection, sets each count on the keypad, watches Scanning, opens a feature, steps with ‹ ›, accepts; every screen
checked against the layout rules and shot (`OUT/ux/`). All 8 scans of the 4 bags: every check passed.

Found on the screens, and fixed:
- **The live count trailed the laser** (holes2: 10 of 20 at release, the rest during "Finishing analysis"):
  OpenBLAS ran a thread per core for the detectors' small fits, 1–3.7 s per cycle under the stack's load. One
  thread: 2.5× faster even idle; with the screens 281 ms median, 18 of 20 at release, Results 1.2 s later;
  rivets2 65 ms, 18 / 18 at release.
- **"scan speed too high: profiles are skipped"** (and "triggers fired late") above 25.6 mm/s with the software
  sync, where the laser runs free and skips nothing: the limit is now the line spacing, 0.5 mm (60 mm/s), and the
  wording says so; late triggers only with the hardware trigger.
- **"imu: excess vibration" on Ready / Results**: that was the device carried between scans (up to 1.9 rad/s; while
  scanning ≤ 0.21). Checked while scanning only.
- **Long scans**: the Results map keeps its contract (uniform scale, +y down), so 20 holes on 540 mm sit 12 px
  apart, too close for a finger. A ‹ Feature k of N › stepper walks them in scan order.

**By a person on the Pi's own screen** (`UX=external` on the Pi, Chromium kiosk on its display): rivets (5 scans),
holes2, holes1, rivets2: every count right, every analysis complete, Results 0.7–1.2 s after the release, each
decision recorded; values equal to the laptop runs within 0.003 mm.

Not changed: the map's corner label ("top-left x −462 · y −40 mm (scan-local, not a part origin)") reads as a
developer note; the replay tool's pace slips when the laptop is loaded (the stack then sees a gap; a rivets2 run
ended "Nano link lost" that way, shown correctly as Analysis failed → Keep incomplete); the browser runs at nice 15.

## Re-check

```bash
# bags: <dir>/pi_20261122/{rivets,rivets2,holes1,holes2}
HHRP_BAGS=<dir> python3 tools/ui_baseline/run_tests_each.py rivet_detector/test/test_rivets.py "test_rows_20261122()"
HHRP_BAGS=<dir> python3 tools/ui_baseline/run_tests_each.py hole_detector/test/test_detector.py "test_rows_20261122()"
# full pipeline (core without drivers + backend + detector + measurement), as the operator would; ~3 min per bag
tools/replay/replay_stack.sh <dir>/pi_20261122/holes2 csk_depth 20 /tmp/e2e_holes2 <device calibration.yaml>
tools/replay/replay_stack.sh <dir>/pi_20261122/rivets rivet_flushness 5,6,6,1,5 /tmp/e2e_rivets <calibration.yaml>
# -> OUT/results.json (each scan as GET /scans/{id}), OUT/logs/
UX=1 tools/replay/replay_stack.sh <dir>/pi_20261122/rivets rivet_flushness 5,6,6,1,5 /tmp/ux_rivets <calibration.yaml>
# -> OUT/ux/*.png, OUT/ux/ux.json (live count over time, release -> Results), OUT/logs/ux.log (checks)
```
