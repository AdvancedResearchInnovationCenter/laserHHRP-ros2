#!/usr/bin/env bash
# Phase 4 screen check with the mock source (no ROS needed): builds ui_web, starts the backend with --mock
# serving the built frontend, then phase4_screens.py taps through every screen in a headless
# Chromium-family browser at 800 x 480, checks layout rules and saves screenshots.
#
#   tools/ui_verify/phase4_screens.sh [OUT_DIR]       # ~2 min; writes only below OUT_DIR
#   SKIP_BUILD=1 tools/ui_verify/phase4_screens.sh     # reuse ui_web/dist
#   BROWSER=chromium tools/ui_verify/phase4_screens.sh
set -o pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
OUT="${1:-$(mktemp -d -t hhrp_phase4.XXXXXX)}"
PORT="${PORT:-8097}"
mkdir -p "$OUT/shots" "$OUT/media"
echo "repo $(git -C "$REPO" rev-parse --abbrev-ref HEAD) @ $(git -C "$REPO" rev-parse --short HEAD); out $OUT"

if [ -z "$SKIP_BUILD" ]; then
  (cd "$REPO/ui_web" && npm run build) > "$OUT/web-build.log" 2>&1 || { echo "web build FAILED ($OUT/web-build.log)"; exit 1; }
fi

# time quality "synced" so labels are date-based; USB export only sees OUT/media (nothing mounted there)
(cd "$REPO/ui_backend" && LASER_HHRP_CLOCK_QUALITY=synced exec python3 -m laser_hhrp_ui.server --mock \
   --mock-speed 1.5 --port "$PORT" --db "$OUT/ui.db" --static "$REPO/ui_web/dist" --usb-root "$OUT/media" \
   --analysis-fail-s 30) > "$OUT/backend.log" 2>&1 &
BACKEND=$!
for _ in $(seq 1 50); do curl -sf "http://127.0.0.1:$PORT/api/v1/health" >/dev/null && break; sleep 0.2; done

timeout 400 python3 -u "$HERE/phase4_screens.py" --base "http://127.0.0.1:$PORT" --out "$OUT/shots" \
  ${BROWSER:+--browser "$BROWSER"} --backend-pid "$BACKEND" 2>&1 | tee "$OUT/check.log"
RC=${PIPESTATUS[0]}
[ "$RC" = 124 ] && echo "FAIL  check timed out after 400 s"
kill -TERM "$BACKEND" 2>/dev/null; wait "$BACKEND" 2>/dev/null
echo "screenshots: $OUT/shots   backend log: $OUT/backend.log"
exit "$RC"
