# Recorded data: what each scan leaves on the device

For whoever uses the device's data for experiments or ML. Every scan made through the operator screens leaves one
folder on the Pi's SD card, `~/laser_hhrp/scans/<date-time>/`:

| File | Written | What |
| --- | --- | --- |
| `frames.bin` | during the scan, line by line | the raw recording: every laser line exactly as measured (ROS format) |
| `session.json`, `results.json` | at the scan's end | recorder metadata |
| `Hole-<id>/` or `Rivet-<id>/` | ~1 s after the analysis | the **legacy per-feature layout** (below), one folder per target feature |
| `scan.json` | with those folders; updated at the decision | the scan's labels: operator, decision, each feature's measurement |

The per-feature folders follow the robot datasets in `data.legacy/data` (one folder per hole, one ASCII PCD per
laser line, a `_profiles.json`, a `_3d.pcd`), so tools written for those read them unchanged. They are generated
from `frames.bin` after the scan, not during it: the feature and its label only exist after detection and
measurement, and writing ~120 small files a second while scanning would risk the recorder's write failure, which
stops the scan. Re-generate or export elsewhere at any time:
`ros2 run laser_hhrp export_legacy.py OUT [--inspection INS-...]` (`laserHHRP/scripts/export_legacy.py`).

## Coordinates and units

- Points are in **metres**, in the laser's frame: `x` along the laser line (laser +x is the operator's left when
  rolling away from themselves), `z` the distance from the laser (larger = farther, i.e. deeper into the part).
- Positions along the scan come from the **encoder** (`metres_per_count` from the device calibration), not from
  the IMU or odometry.
- Measurements in `scan.json` are in **mm**.

## Per-feature folder: `Hole-5/` (holes) or `Rivet-5/` (rivets), 5 = the feature's id in the scan

Every laser line whose position is within **±3 mm** of the feature's centre, the full line across (valid points
only).

| File | Content |
| --- | --- |
| `Hole-5_<dy>mm.pcd` | one laser line, ASCII PCD, fields `x y z intensity`; `y` is always 0; `intensity` is 1.0 (the laser's intensity is not recorded) |
| `Hole-5_<dy>mm_2.pcd`, `_3`, … | further lines with the same `dy` (see stalls below) |
| `Hole-5_profiles.json` | every line of the folder (below) |
| `Hole-5_3d.pcd` | all lines merged, ASCII PCD, fields `x y z`, with `y = dy / 1000` |

**`dy`** = the line's encoder position minus the feature's centre, in mm (negative = before the centre along the
roll). In the file name it is rounded to 0.01 mm as in the legacy files (`-2.88`, `0.0`, `0.12`); in the JSON it
has 4 decimals.

### `Hole-5_profiles.json`

Profiles are numbered in order of `dy`. Each holds the legacy keys plus the device's own record of that line:

```json
"profile_001": {
    "dy": -2.876,                         offset from the feature centre, mm (legacy key)
    "file": "Hole-5_-2.88mm.pcd",         this line's PCD
    "frame_index": 658,                   line number in the scan (0 = first line)
    "time_s": 1795336767.3838,            when the line was taken: Pi clock, Unix seconds
    "encoder_count": 10252,               raw encoder reading
    "position_mm": 118.500,               distance rolled since the scan's start (encoder)
    "direction": 1,                       +1 rolling forward, -1 backward, 0 not moving
    "accel_mps2": [-7.70, 5.14, -2.48],   IMU acceleration at that moment, m/s², IMU axes (calibrated)
    "gyro_radps": [-0.030, 0.002, 0.013], IMU rotation rate, rad/s (rocking / tipping shows here)
    "imu_valid": true,                    false if no IMU samples bracket the line (values then unreliable)
    "flags": 0,                           bit 0: overspeed, bit 1: laser / trigger timing mismatch
    "points": [ {"intensity": 1.0, "x": 0.01186, "y": 0.0, "z": 0.07363}, ... ]   legacy key
}
```

A program written for the legacy data reads `dy` and `points` and ignores the rest.

### Why the extra fields: a hand is not a robot

The robot stepped exactly 0.12 mm per line, so `dy` alone said where each line was. On this device:

- **Spacing is uneven.** It follows how fast the operator rolls: about 0.05–0.5 mm between lines (120 lines per
  second). `time_s` and `position_mm` give the real speed at every line.
- **Stalls.** When the operator pauses on a feature, the encoder stands still, so consecutive lines get the same
  `dy`: their files are `Hole-5_0.0mm.pcd`, `Hole-5_0.0mm_2.pcd`, `_3`, … From `dy` alone they look like one
  line repeated; in fact the device may be tipping while they are taken. That shows in `gyro_radps`, and in
  `time_s` advancing while `encoder_count` stands still. (2026-11-22, holes2 hole 5: 12 lines at one encoder count
  over 0.1 s while the skin either side read 0.7 / 0.3 mm low; the device reports such a feature as
  `scan_distorted`.)

## `scan.json`

| Key | |
| --- | --- |
| `scan_id`, `scan_number`, `inspection`, `inspection_uuid` | identity (`inspection` = `INS-<date>-<n>` as on the screens) |
| `operator_id` | who scanned (Identify screen) |
| `mode` | `csk_depth` or `rivet_flushness` |
| `expected_count`, `detected_target_count` | the count the operator set, and what was found |
| `analysis` | `complete` or `failed` |
| `decision`, `decided_by`, `decided_at` | `accepted` / `rescanned` / `incomplete_retained` (failed scan kept), or null until decided |
| `started_at`, `ended_at`, `clock_quality` | UTC times; `clock_quality` says whether the Pi's clock was verified |
| `raw_dir`, `raw_frame_count`, `frames` | the folder and its line count |
| `half_width_mm` | the ±range of lines per feature (3.0) |
| `features[]` | every feature found (targets and others) |

Each `features[]` entry: `feature_id`, `kind` (`countersink`, `hole`, `rivet`), `is_target`, `x_mm` / `y_mm` /
`z_mm` (centre; `y_mm` = position along the scan), `diameter_mm`, `partial` (ran out of the scanned area),
`quality` (detector fit), and `measurement`:

```json
"measurement": {"name": "csk_depth", "status": "measured", "value_mm": 1.1745, "verdict": "out_of_limits",
                "lower_mm": 0.947, "upper_mm": 1.147, "unit": "mm", "reason": null}
```

`status` `measured` with `value_mm` and `verdict` (`in_limits` = GO, `out_of_limits` = NO-GO), or `not_measured`
with `reason`: `partial`, `scan_distorted` (device rocked or the patch bent), `unexpected_shape`,
`too_few_points`, `surface_not_flat`, `failed`. Flushness `value_mm` is signed: positive = head proud. Target
features also carry `folder` (their `Hole-<id>`) and `lines` (how many lines it holds).

## Bags: everything, for replay ("Rec bags" on the Ready screen)

`frames.bin` keeps each laser line as the device placed it, not every sensor stream: the raw IMU and encoder
samples at 240 Hz between lines, the laser's own counter and clock, and the calibration values themselves are not
in it (laser intensity is not read by the driver at all). For those, switch **Rec bags** on (Ready screen,
bottom bar; off after every restart of the software):

- One `ros2 bag record -a` per scan in `~/laser_hhrp/bags/<operator>-<inspection>-Scan-<NNN>/` (e.g.
  `ENG-1042-INS-20261123-001-Scan-003`; an existing name gets `_2`). It opens as soon as Ready shows and closes
  when that scan's analysis ends, so it also holds the seconds before the press. **Wait for "Recording bag" on
  Ready (● REC in the header) before pressing the switch**: ros2 bag takes 1-3 s to subscribe to every topic.
- Started with the ROS and workspace setup sourced (the device's own message types are recorded too).
- Size: 4-11 MB/s with `-a` (0.5-1.2 GB per scan). Below **5 GB free** the recording stops by itself and the
  switch turns off (alert on screen), so the scans' own recording never runs out of space. Not switchable during a
  scan or its analysis. Log: `~/.local/share/laser_hhrp/ui/logs/bag.log`.
- Play back: `ros2 bag play --read-ahead-queue-size 12000 <bag>`. Re-run the whole stack on it:
  `tools/replay/replay_stack.sh <bag> <mode> <counts> <out>` (it opens the `.mcap` itself; Jazzy bags on Humble).

## Size

About 75 MB per scan of 20 holes (ASCII), `frames.bin` ~13 MB. The Pi's 34 GB free holds ~450 such scans: copy
the scan folders to a drive regularly.
