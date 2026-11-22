#!/usr/bin/env bash
# The whole stack on a bag recorded on the device, in place of the drivers (raw_replay.py): synchronizer,
# odometry, point cloud, scan controller, recorder, calibration, measurement and the operator UI backend with its
# detector, driven through an inspection like an operator would (accept after each scan).
#
#   tools/replay/replay_stack.sh BAG MODE COUNTS OUT [CALIBRATION]
#     BAG          a bag of `ros2 bag record -a` on the device (laser/raw, nano/* and scan/status in it)
#     MODE         csk_depth | rivet_flushness
#     COUNTS       expected count per scan, comma separated ("" = keep the backend's)
#     OUT          output folder (logs, the backend's db and detector output, results.json)
#     CALIBRATION  the device's calibration.yaml (default ~/laser_hhrp/calibration.yaml)
# Env: HHRP_WS (colcon workspace with install/, default the one holding this repo), RATE (replay speed, 1),
#      ROS_DOMAIN_ID (default 89). Isolated: own HOME under OUT, LOCALHOST discovery.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
WS="${HHRP_WS:-$(cd "$REPO/../.." && pwd)}"
BAG="$(readlink -f "$1")"; MODE="$2"; COUNTS="$3"; OUT="$(mkdir -p "$4" && cd "$4" && pwd)"
CAL="${5:-$HOME/laser_hhrp/calibration.yaml}"
[ -f "$CAL" ] || { echo "no calibration file $CAL" >&2; exit 2; }

export PYTHONUSERBASE="${PYTHONUSERBASE:-$HOME/.local}"
export HOME="$OUT/home" ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-89}" ROS_LOG_DIR="$OUT/home/roslog"
export ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST ROS_LOCALHOST_ONLY=1
mkdir -p "$HOME/laser_hhrp" "$OUT/logs"
cp "$CAL" "$HOME/laser_hhrp/calibration.yaml"
set +u; source "$WS/install/setup.bash"; set -u
PARAMS="$WS/install/laser_hhrp/share/laser_hhrp/config/hhrp.yaml"
BIN="$WS/install/laser_hhrp/lib/laser_hhrp"

pids=()
for node in calibration_node acquisition_node odometry_node synchronizer_node point_cloud_node recorder_node \
            inspection_node scan_controller_node; do
  "$BIN/$node" --ros-args --params-file "$PARAMS" -p sync.hardware_trigger:=false > "$OUT/logs/$node.log" 2>&1 &
  pids+=($!)
done
STATIC=()
[ -d "$REPO/ui_web/dist" ] && STATIC=(--static "$REPO/ui_web/dist")
"$WS/install/laser_hhrp_ui/lib/laser_hhrp_ui/server" --port 8099 --db "$OUT/ui.db" "${STATIC[@]}" \
  > "$OUT/logs/backend.log" 2>&1 &
pids+=($!)

stop() {
  for p in "${pids[@]}"; do kill -TERM "$p" 2>/dev/null; done
  for _ in $(seq 1 40); do
    alive=0; for p in "${pids[@]}"; do kill -0 "$p" 2>/dev/null && alive=1; done
    [ $alive = 0 ] && break; sleep 0.25
  done
  for p in "${pids[@]}"; do kill -KILL "$p" 2>/dev/null; done
}
trap stop EXIT
sleep 3
python3 "$HERE/raw_replay.py" "$BAG" --ui http://127.0.0.1:8099 --mode "$MODE" --counts "$COUNTS" \
  --rate "${RATE:-1}" --wait "${WAIT:-300}" --out "$OUT/results.json" 2>&1 | tee "$OUT/logs/replay.log"
rc=${PIPESTATUS[0]}
sleep 2
exit $rc
