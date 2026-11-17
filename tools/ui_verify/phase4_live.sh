#!/usr/bin/env bash
# Phase 4 on live ROS input: simulated core + live backend serving the built UI + headless browser.
#
#   tools/ui_verify/phase4_live.sh [OUT_DIR]      # ~3 min; writes only below OUT_DIR
#   HHRP_WS=<dir with build/ install/> tools/ui_verify/phase4_live.sh   # reuse a build (no C++ recompile)
#
# The build is memory-limited (one package at a time, 2 compile jobs): a full-parallel C++ build can
# exhaust RAM on a machine without swap.
#
# Builds all packages into OUT_DIR (and ui_web unless SKIP_WEB=1), starts the core (simulate:=true holes:=false
# ui:=false scan_switch:=false) and the backend serving ui_web/dist, then phase4_live.py taps through the
# screens in a headless Chromium-family browser. Screenshots: OUT_DIR/shots.
set -o pipefail   # no -u: ROS setup scripts read unset variables

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
OUT="${1:-$(mktemp -d -t hhrp_phase4live.XXXXXX)}"
WS="${HHRP_WS:-$OUT}"
mkdir -p "$OUT/home" "$OUT/ui"
DISTRO="${ROS_DISTRO:-$(ls /opt/ros | head -1)}"
# shellcheck disable=SC1090
source "/opt/ros/$DISTRO/setup.bash"
echo "repo $(git -C "$REPO" rev-parse --abbrev-ref HEAD) @ $(git -C "$REPO" rev-parse --short HEAD); out $OUT; ros $DISTRO"

if [ -z "$SKIP_WEB" ]; then
  (cd "$REPO/ui_web" && npm run build) > "$OUT/web-build.log" 2>&1 || { echo "web build FAILED"; exit 1; }
fi
MAKEFLAGS="-j2" colcon --log-base "$OUT/log" build --base-paths "$REPO" --build-base "$WS/build" \
  --install-base "$WS/install" --parallel-workers 1 > "$OUT/build.log" 2>&1 || { echo "build FAILED ($OUT/build.log)"; exit 1; }
# shellcheck disable=SC1091
source "$WS/install/setup.bash"
# keep pip --user packages (e.g. FastAPI on a dev laptop) visible after HOME moves to the scratch dir
export PYTHONUSERBASE="${PYTHONUSERBASE:-$HOME/.local}"
export HOME="$OUT/home" ROS_DOMAIN_ID=94 ROS_LOCALHOST_ONLY=0 ROS_LOG_DIR="$OUT/home/roslog"
export CYCLONEDDS_URI="file://$REPO/tools/ui_baseline/cyclonedds_loopback.xml"
export UI_BASE="http://127.0.0.1:8098"

ros2 launch laser_hhrp hhrp.launch.py simulate:=true holes:=false ui:=false scan_switch:=false > "$OUT/core.log" 2>&1 &
CORE=$!
# the installed executable directly: the `ros2 run` wrapper does not forward SIGTERM and would orphan the server
"$WS/install/laser_hhrp_ui/lib/laser_hhrp_ui/server" --port 8098 --db "$OUT/ui/inspections.db" --static "$REPO/ui_web/dist" > "$OUT/backend.log" 2>&1 &
BACKEND=$!

# unbuffered and logged, capped at 5 min so a stuck step cannot hold the machine
timeout 300 python3 -u "$HERE/phase4_live.py" "$OUT/shots" 2>&1 | tee "$OUT/check.log"
FAILS=${PIPESTATUS[0]}
[ "$FAILS" = 124 ] && echo "FAIL  check timed out after 300 s (see $OUT/check.log)"

echo "--- shutdown"
stop() {  # SIGTERM, bounded wait, then SIGKILL
  kill -TERM "$1" 2>/dev/null
  for _ in $(seq 1 60); do kill -0 "$1" 2>/dev/null || break; sleep 0.5; done
  kill -KILL "$1" 2>/dev/null; wait "$1" 2>/dev/null
}
stop "$BACKEND"
left=$(pgrep -fc "$WS/install/(hole|rivet)_detector/lib" || true)
if [ "$left" = "0" ]; then echo "PASS  backend SIGTERM leaves no detector process"; else echo "FAIL  $left detector process(es) left"; FAILS=$((FAILS+1)); fi
stop "$CORE"
pkill -KILL -f "$WS/install/(laser_hhrp|hole_detector|rivet_detector)/" 2>/dev/null
echo "backend log: $OUT/backend.log   detector logs: $OUT/ui/logs/"
exit "$FAILS"
