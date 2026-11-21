# Verifying the measurement integration yourself

The measurement algorithms (CSK depth, rivet flushness) run after every scan and their values reach the operator
screens with a GO / NO-GO verdict. Contract: [MEASUREMENT_INTEGRATION.md](MEASUREMENT_INTEGRATION.md). Node:
[inspection_node.md](../../laserHHRP/docs/inspection_node.md).

## What was built

| Part | Where |
| --- | --- |
| Algorithms as git submodules (`git submodule update --init`), built as static libraries into `laser_hhrp` | `laserHHRP/third_party/laser_csk_inspection` (`optimized_algorithm`), `laserHHRP/third_party/rivet_flushness` (`main`) |
| `inspection_node`: one scan at a time after `inspection/detection_finalized`, features on 4 threads, `inspection/measurement` + `measurement.csv` | [inspection_node.cpp](../../laserHHRP/src/inspection_node.cpp), [measurement_input.hpp](../../laserHHRP/include/laser_hhrp/measurement_input.hpp) |
| Messages: `MeasuredFeature`, `MeasurementFinalized` (new); `DetectionFinalized.output_dir` (added, filled by both detectors) | [laserHHRP/msg](../../laserHHRP/msg) |
| Limits and z direction | `hhrp.yaml` `inspection.*` (CSK depth GO 0.947 – 1.147 mm; flushness GO −0.15 – +0.05 mm) |
| Backend: the scan completes only with its measurement, exactly one per target; values, verdicts and reasons stored; CSV columns; Ready needs the node | [state.py](../../ui_backend/laser_hhrp_ui/state.py), [models.py](../../ui_backend/laser_hhrp_ui/models.py), [ros_bridge.py](../../ui_backend/laser_hhrp_ui/ros_bridge.py), [export.py](../../ui_backend/laser_hhrp_ui/export.py) |
| Screens: GO / NO-GO / not-measured counts, map coloured by verdict, feature detail = id, position, value, verdict, GO range or reason in operator words | [FeatureDetail.tsx](../../ui_web/src/ui/FeatureDetail.tsx), [MeasureCounts.tsx](../../ui_web/src/ui/MeasureCounts.tsx), [format.ts](../../ui_web/src/lib/format.ts) |
| Mock source measures too (values around nominal, a few out of limits) | [mock_ros.py](../../ui_backend/laser_hhrp_ui/mock_ros.py) |

## Checks (laptop, Humble, 2026-11-21)

| Check | Command | Result |
| --- | --- | --- |
| The real node on the algorithms' own test folders (copies), values vs their expected files, failure cases | `HHRP_WS=… tools/ui_verify/measurement_check.sh` | **25/25**; CSK within 0.69 µm of `expected_depth.csv`, rivets within 0.5 µm / same reasons (`scan_distorted`) as `expected.csv` |
| C++ input parsing (summary.csv, PCD, limits) | `build/laser_hhrp/test_logic --gtest_filter='MeasurementInput.*'` | **5/5** (13/13 in all) |
| Backend | `cd ui_backend && python3 -m pytest -q test/` | **151** passed |
| Planted backend bugs | `python3 tools/ui_verify/mutation_check.py` | **41/41** caught (9 new for the measurement) |
| Frontend | `cd ui_web && npm test` | **40/40** |
| Planted UI bugs | `python3 tools/ui_verify/web_mutation_check.py` | **19/19** caught |
| Screens in a real browser (mock), layout rules at 800 × 480 | `tools/ui_verify/phase4_screens.sh` | **136/136** |
| Live ROS: simulated core (with `inspection_node`), backend, browser | `HHRP_WS=… tools/ui_verify/phase4_live.sh` | **40/40** |
| Live ROS identity and detectors | `phase3_ros_check.sh`, `phase3_identity_check.sh`, `phase3_detector_check.sh` (both detectors) | **27, 19, 16, 16** passed |

Timing on the laptop (4 threads): 20 countersinks 0.63 s, 10 rivets 0.58–0.74 s at full line density.
On the Pi 5 (2026-11-20, csk_depth alone): 20 countersinks 0.67–0.70 s with 4 workers.

## On the Pi

```bash
cd ~/aric_ws/src/laserHHRP-ros2
git fetch && git checkout inspection-algorithms && git pull --ff-only
git submodule update --init                       # the two algorithm repos (needs GitHub access)
cd ~/aric_ws && colcon build --packages-select hole_detector rivet_detector laser_hhrp laser_hhrp_ui
HHRP_WS=~/aric_ws src/laserHHRP-ros2/tools/ui_verify/measurement_check.sh   # Pi values vs expected, timing
```

`ui_web/dist` is built on the laptop (`npm run build`) and copied to the Pi (no Node there).

**First real scans:** check `inspection.rivet_invert_z` / `csk_invert_z` against the scanner's `.sc1`: with the
wrong value every feature reads *Shape not recognised* (`unexpected_shape`), never a wrong number. Each scan folder
(`~/.local/share/laser_hhrp/ui/detector-output/<inspection>/<scan>/`) then has `measurement.csv` with the values,
point counts, times and the engineer detail of every not-measured feature: the raw material for repeatability.

## Open

- Not yet run on the Pi (it was not reachable on 2026-11-21): build, `measurement_check.sh`, a real scan per mode.
- Accuracy against gauge readings: not established; the verdicts use the limits given for the trials.
