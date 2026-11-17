#!/usr/bin/env bash
# Phase 3: the real hole_detector node in tagged mode, fed the synthetic plate (isolated ROS graph).
#   HHRP_WS=<dir with build/ install/> [CHECK_DETECTOR=rivet_detector] tools/ui_verify/phase3_detector_check.sh [OUT_DIR]
set -o pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
OUT="${1:-$(mktemp -d -t hhrp_phase3det.XXXXXX)}"
export HHRP_WS="${HHRP_WS:?set HHRP_WS to a built workspace (build/ install/)}"
mkdir -p "$OUT/home"
DISTRO="${ROS_DISTRO:-$(ls /opt/ros | head -1)}"
# shellcheck disable=SC1090
source "/opt/ros/$DISTRO/setup.bash"
# shellcheck disable=SC1091
source "$HHRP_WS/install/setup.bash"
export PYTHONUSERBASE="${PYTHONUSERBASE:-$HOME/.local}"
export HOME="$OUT/home" ROS_DOMAIN_ID=90 ROS_LOCALHOST_ONLY=0 ROS_LOG_DIR="$OUT/home/roslog" CHECK_OUT="$OUT"
export CYCLONEDDS_URI="file://$REPO/tools/ui_baseline/cyclonedds_loopback.xml"
timeout 600 python3 -u "$HERE/phase3_detector_check.py" 2>&1 | tee "$OUT/check.log"
FAILS=${PIPESTATUS[0]}
pkill -KILL -f "$HHRP_WS/install/(hole|rivet)_detector/" 2>/dev/null
echo "logs: $OUT (detector.log)"
exit "$FAILS"
