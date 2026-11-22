# inspection_node

Layer 5 (measurement). Once per scan, after the detector has written its scan folder, it measures every target
feature with the measurement algorithms and publishes the results: CSK depth for countersinks (CSK Depth
inspections), flushness for rivets (Rivet Flushness inspections).

Source: `src/inspection_node.cpp`; input parsing in `include/laser_hhrp/measurement_input.hpp`.
Contract with the algorithms: [`docs/operator_ui/MEASUREMENT_INTEGRATION.md`](../../docs/operator_ui/MEASUREMENT_INTEGRATION.md).

## Algorithms (git submodules)

| Path | Repository | Function |
| --- | --- | --- |
| `third_party/laser_csk_inspection` (branch `optimized_algorithm`; uses `csk_depth/`) | laser_csk_inspection | `csk::measure_countersink_depth` (C++17 + Eigen) |
| `third_party/rivet_flushness` (branch `main`) | rivet_flushness | `rivet_flushness::measure_rivet_flushness` (C++17) |

After cloning: `git submodule update --init`. The submodules use URLs relative to this repository's remote. Both
build as static libraries inside `laser_hhrp` (optimized at `-O2` even without a build type). A missing submodule
gives a CMake warning, and every feature of that kind is then reported `failed`.

Update an algorithm: `git -C laserHHRP/third_party/<repo> pull`, commit the new submodule commit, rebuild
`laser_hhrp`, run `tools/ui_verify/measurement_check.sh`.

## Interfaces

| Kind | Name | Type | Notes |
| --- | --- | --- | --- |
| sub | `inspection/detection_finalized` | `laser_hhrp/DetectionFinalized` | from the tagged detector; `output_dir` = the scan folder. Volatile: a restarted node does not re-measure old scans |
| pub | `inspection/measurement` | `laser_hhrp/MeasurementFinalized` | one per successful detection, reliable, transient local (depth 50) |
| file | `<scan folder>/measurement.csv` | | `id, <quantity>_um, in_limits, reason, points, ms, note`; written before the message |

`MeasurementFinalized`: `scan_id`, `detector_mode`, `quantity` (`csk_depth` / `flushness`), the GO range
`lower_limit_m` .. `upper_limit_m` (inclusive), `features[]` (`feature_id`, `measured`, `value_m`, `reason`,
`elapsed_s`), `success`, `error`, `elapsed_s`. Every target feature appears exactly once.
A failed detection gets no message (the backend has already failed the scan). An unreadable scan folder gets
`success = false` with the reason.

| Parameter (`hhrp.yaml`) | Default | |
| --- | --- | --- |
| `inspection.csk_depth_min_m` / `_max_m` | 0.000947 / 0.001147 | GO range of the CSK depth (truncated cone height) |
| `inspection.flushness_min_m` / `_max_m` | -0.00015 / 0.00005 | GO range of the flushness (positive = head proud) |
| `inspection.csk_invert_z`, `inspection.rivet_invert_z` | false | z direction of the PCDs (scanner `.sc1`); a wrong value gives `unexpected_shape`, not a value |
| `inspection.rivet_check_orientation` | true (hhrp.yaml: false) | the flushness library's z check (mandrel groove, panel 2); off for rivets without that groove, which it refuses as `unexpected_shape` even with the right z |
| `inspection.csk_angle_deg` | 130.0 | included countersink angle (input to the CSK fit) |
| `inspection.csk_bore_diameter_mm` | 4.25 | measured bore diameter (input to the CSK fit) |
| `inspection.workers` | 4 | features measured in parallel threads (one scan at a time) |
| `inspection.write_csv` | true | `measurement.csv` in each scan folder |

## Processing

```
detection_finalized (success) → queue → worker thread, one scan at a time:
    summary.csv → targets: CSK Depth = countersunk rows, Rivet Flushness = every row
    for each target, on `workers` threads:
        partial            → not measured, reason "partial" (never handed to the algorithm)
        hole_NN / rivet_NN.pcd (binary float32 x y z, metres) → algorithm → value_m or reason code
    measurement.csv, then MeasurementFinalized
```

Reason codes: `too_few_points`, `surface_not_flat`, `unexpected_shape`, `scan_distorted` (rivets), `partial`,
`failed` (also a missing or unreadable PCD). The engineer detail of each not-measured feature is in the node's log
and the `note` column of `measurement.csv`.

## Checks

`HHRP_WS=<workspace> tools/ui_verify/measurement_check.sh`: the node on copies of both algorithms' test folders,
values against their expected files, plus the failure cases.
