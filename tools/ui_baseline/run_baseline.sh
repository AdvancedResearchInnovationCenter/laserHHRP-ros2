#!/usr/bin/env bash
# Operator UI, Phase 0: reproduce the baseline (build, tests, simulation probes) without touching
# the developer's workspace, home directory or ROS graph.
#
#   tools/ui_baseline/run_baseline.sh [OUT_DIR]
#
# Everything is written below OUT_DIR (default: a new mktemp dir): colcon build/install/log, a fake
# HOME (calibration.yaml, recorded scans, detector output, ROS logs) and the result logs.
# The ROS graph is isolated: ROS_DOMAIN_ID=87 and a loopback-only CycloneDDS config.
# Detector bag tests report SKIP/NODATA unless the bags sit next to the packages (docs/operator_ui/BASELINE.md).
set -o pipefail   # no -u: ROS setup scripts read unset variables

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
OUT="${1:-$(mktemp -d -t hhrp_baseline.XXXXXX)}"
mkdir -p "$OUT/home"
DISTRO="${ROS_DISTRO:-$(ls /opt/ros | head -1)}"
# shellcheck disable=SC1090
source "/opt/ros/$DISTRO/setup.bash"
echo "repo: $REPO ($(git -C "$REPO" rev-parse --abbrev-ref HEAD) @ $(git -C "$REPO" rev-parse --short HEAD))"
echo "out:  $OUT    ros: $DISTRO    $(lsb_release -ds 2>/dev/null) $(uname -m)"

echo "=== build"
colcon --log-base "$OUT/log" build --base-paths "$REPO" --build-base "$OUT/build" \
  --install-base "$OUT/install" --cmake-args -DBUILD_TESTING=ON > "$OUT/build.log" 2>&1 \
  && echo "build OK" || { echo "build FAILED (see $OUT/build.log)"; exit 1; }
grep -i "warning" -A2 "$OUT/build.log" | grep -i "sdk" | head -2
# shellcheck disable=SC1091
source "$OUT/install/setup.bash"

echo "=== laser_hhrp gtest (test_logic)"
"$OUT/build/laser_hhrp/test_logic" 2>&1 | tail -1
echo "=== laser_hhrp detectors launch test"
(cd "$REPO" && python3 -m pytest -q laserHHRP/test/test_detectors_launch.py 2>&1 | tail -1)

echo "=== hole_detector (each test separately; SKIP/NODATA = bag not present)"
(cd "$REPO" && python3 "$HERE/run_tests_each.py" hole_detector/test/test_detector.py \
  "test_launch_single_node();test_rviz_is_light();test_writer_drains();test_empty_cloud_is_ignored();test_synthetic_plate();test_downsample_matches_voxel_down(synthetic_plate());test_synthetic_incremental();test_reverse_scan();test_rejection_reported_once();test_crop_edge_is_not_a_rejection();test_surface_strips();test_band_slabs_on_tilted_plate();test_hole_markers();test_hole_near_scan_start();test_batch_matches_reference();test_real_bag_counts();test_replay_matches_baseline();test_boundary_margin();test_panel_bags();test_latency();test_bag_frames_rebuild()" 2>&1 \
  | grep -E "^(PASS|FAIL|NODATA|SKIP)")

echo "=== rivet_detector (each test separately)"
(cd "$REPO" && python3 "$HERE/run_tests_each.py" rivet_detector/test/test_rivets.py \
  "test_launch_single_node();test_rviz_is_light();test_synthetic_batch();test_side_clip(synthetic_panel());test_downsample_matches_voxel_down(synthetic_panel());test_synthetic_both_signs(synthetic_panel());test_synthetic_incremental(synthetic_panel());test_boundary_is_raw(synthetic_panel(), test_synthetic_incremental(synthetic_panel()));test_results_have_no_nan(test_synthetic_incremental(synthetic_panel()));test_panel2_bags();test_panel_bags_generic();test_latency()" 2>&1 \
  | grep -E "^(PASS|FAIL|NODATA|SKIP)")

# ---- simulation: isolated graph and HOME ----
export HOME="$OUT/home" ROS_DOMAIN_ID=87 ROS_LOCALHOST_ONLY=0 ROS_LOG_DIR="$OUT/home/roslog"
export CYCLONEDDS_URI="file://$HERE/cyclonedds_loopback.xml"
stop_stack() {  # SIGINT the launch (it stops its nodes), then kill whatever of this build is left
  kill -INT "$1" 2>/dev/null
  for _ in $(seq 1 40); do pgrep -f "$OUT/install/" > /dev/null || break; sleep 0.25; done
  pkill -KILL -f "$OUT/install/" 2>/dev/null; wait "$1" 2>/dev/null
}
run_sim() {  # run_sim LOG EXTRA_LAUNCH_ARGS -- PROBE_CMD...
  local log="$1" args="$2"; shift 3
  if pgrep -f "$OUT/install/" > /dev/null; then echo "a stack from $OUT is still running: abort"; exit 1; fi
  # shellcheck disable=SC2086
  ros2 launch laser_hhrp hhrp.launch.py simulate:=true ui:=false scan_switch:=false $args > "$log" 2>&1 &
  local lp=$!
  timeout 150 "$@"
  stop_stack "$lp"
}

echo "=== simulation probe: scan 3 s, listen 10 s; scan 3 s + rapid 0.15 s re-press, listen 10 s"
run_sim "$OUT/launch_probe.log" "" -- python3 "$HERE/probe_sim.py" 3 10 0.15 2>&1 | tee "$OUT/probe.log" | grep -v "^$"
echo "detector results logged: $(grep -cE '[1-9][0-9]* new (hole|rivet)' "$OUT/launch_probe.log")"

echo "=== same-second scans (recorder session directory collision)"
before="$(ls "$HOME/laser_hhrp/scans")"
run_sim "$OUT/launch_collide.log" "holes:=false" -- python3 "$HERE/same_second_scans.py" 2>&1 | grep -v "^$"
echo "new session dirs: $(comm -13 <(echo "$before") <(ls "$HOME/laser_hhrp/scans") | tr '\n' ' ')"
grep "session closed" "$OUT/launch_collide.log" | sed 's/.*recorder\]: //'
echo "done: $OUT"
