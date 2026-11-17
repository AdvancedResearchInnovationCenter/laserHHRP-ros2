#!/usr/bin/env bash
# Phase 3 identity layer check: simulated core only (controller, synchronizer, recorder), isolated ROS graph.
#   HHRP_WS=<dir with build/ install/> tools/ui_verify/phase3_identity_check.sh [OUT_DIR]
set -o pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
OUT="${1:-$(mktemp -d -t hhrp_phase3id.XXXXXX)}"
WS="${HHRP_WS:?set HHRP_WS to a built workspace (build/ install/)}"
mkdir -p "$OUT/home"
DISTRO="${ROS_DISTRO:-$(ls /opt/ros | head -1)}"
# shellcheck disable=SC1090
source "/opt/ros/$DISTRO/setup.bash"
# shellcheck disable=SC1091
source "$WS/install/setup.bash"
export PYTHONUSERBASE="${PYTHONUSERBASE:-$HOME/.local}"
export HOME="$OUT/home" ROS_DOMAIN_ID=89 ROS_LOCALHOST_ONLY=0 ROS_LOG_DIR="$OUT/home/roslog"
export CYCLONEDDS_URI="file://$REPO/tools/ui_baseline/cyclonedds_loopback.xml"
ros2 launch laser_hhrp hhrp.launch.py simulate:=true holes:=false ui:=false scan_switch:=false > "$OUT/core.log" 2>&1 &
CORE=$!
timeout 120 python3 -u "$HERE/phase3_identity_check.py" 2>&1 | tee "$OUT/check.log"
FAILS=${PIPESTATUS[0]}
kill -TERM "$CORE" 2>/dev/null
for _ in $(seq 1 40); do kill -0 "$CORE" 2>/dev/null || break; sleep 0.25; done
pkill -KILL -f "$WS/install/laser_hhrp/" 2>/dev/null; wait "$CORE" 2>/dev/null
echo "logs: $OUT"
exit "$FAILS"
