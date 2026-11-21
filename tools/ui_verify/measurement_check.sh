#!/usr/bin/env bash
# The real inspection_node (CSK depth + rivet flushness) on the algorithms' own test folders (isolated ROS graph).
#   HHRP_WS=<dir with build/ install/> tools/ui_verify/measurement_check.sh [OUT_DIR]
# Needs the submodules (git submodule update --init) and a laser_hhrp build.
set -o pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
OUT="${1:-$(mktemp -d -t hhrp_measure.XXXXXX)}"
export HHRP_WS="${HHRP_WS:?set HHRP_WS to a built workspace (build/ install/)}"
mkdir -p "$OUT/home"
DISTRO="${ROS_DISTRO:-$(ls /opt/ros | head -1)}"
# shellcheck disable=SC1090
source "/opt/ros/$DISTRO/setup.bash"
# shellcheck disable=SC1091
source "$HHRP_WS/install/setup.bash"
export PYTHONUSERBASE="${PYTHONUSERBASE:-$HOME/.local}"
export HOME="$OUT/home" ROS_DOMAIN_ID=91 ROS_LOCALHOST_ONLY=0 ROS_LOG_DIR="$OUT/home/roslog" CHECK_OUT="$OUT"
export CYCLONEDDS_URI="file://$REPO/tools/ui_baseline/cyclonedds_loopback.xml"
timeout 900 python3 -u "$HERE/measurement_check.py" 2>&1 | tee "$OUT/check.log"
FAILS=${PIPESTATUS[0]}
pkill -KILL -f "$HHRP_WS/install/laser_hhrp/lib/laser_hhrp/inspection_node" 2>/dev/null
echo "logs: $OUT (inspection_node.log, scans/*/measurement.csv)"
exit "$FAILS"
