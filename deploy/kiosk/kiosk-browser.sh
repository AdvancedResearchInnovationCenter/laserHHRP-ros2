#!/usr/bin/env bash
# Runs inside cage: the operator screen in Chromium, full screen. If Chromium exits it is opened again, except
# for desktop mode (request file): then this script ends and cage with it (cage aborts when it is signalled).
# It opens starting.html, which switches to the operator UI as soon as the backend answers.
HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
PROFILE="$HOME/snap/chromium/common/hhrp-kiosk"
LOG="${HHRP_WS:-$HOME/aric_ws}/operator_ui/logs/kiosk/browser.log"
REQUEST="${HHRP_WS:-$HOME/aric_ws}/operator_ui/kiosk/request"
while [ "$(cat "$REQUEST" 2>/dev/null)" != desktop ]; do
  echo "$(date -Is) start chromium" >>"$LOG"
  /snap/bin/chromium --kiosk --ozone-platform=wayland --user-data-dir="$PROFILE" --no-first-run \
    --no-default-browser-check --password-store=basic --noerrdialogs --disable-infobars \
    --disable-session-crashed-bubble --disable-pinch --overscroll-history-navigation=0 \
    --check-for-update-interval=31536000 "file://$HERE/starting.html" >>"$LOG" 2>&1 </dev/null
  echo "$(date -Is) chromium exited ($?): opening it again" >>"$LOG"
  sleep 1
done
