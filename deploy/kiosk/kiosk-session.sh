#!/usr/bin/env bash
# Operator kiosk session (GDM autologin, Wayland): the laser roller core, the operator UI backend and the
# operator screen in Chromium under cage. Nothing else: no desktop, no terminal, nothing to close.
# A process that exits is started again. Turning off: ⋮ → Turn off on the screen (systemctl poweroff).
#
# Developers: ⋮ → Service… → PIN (~/aric_ws/operator_ui/service_pin). The backend writes a request into
# ~/aric_ws/operator_ui/kiosk/request, which this script acts on:
#   restart   stop the core and the backend (and so the detector), start them again
#   desktop   stop them and the screen, run the Ubuntu desktop (terminal, Wi-Fi settings) until logout,
#             then everything starts again. A reboot always comes back to the operator screen.
# Installed by install.sh in this folder. Logs: ~/aric_ws/operator_ui/logs/kiosk/
set -u
HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
WS="${HHRP_WS:-$HOME/aric_ws}"
LOGS="$WS/operator_ui/logs/kiosk"
CTL="$WS/operator_ui/kiosk"
PIN="$WS/operator_ui/service_pin"
NANO_PORT="${NANO_PORT:-/dev/ttyUSB0}"
mkdir -p "$LOGS" "$CTL" "$WS/operator_ui/.locks"
exec >>"$LOGS/session.log" 2>&1
echo "=== $(date -Is) kiosk session"
rm -f "$CTL/request" "$CTL"/*.pid

# The start scripts and this session's scanner software never run together (same lock as start_hardware.sh /
# start_simulation.sh). Children do not inherit the lock's descriptor (9>&-), so stopping them releases nothing
# and the desktop mode can hand the lock over (flock -u) to a start script run by hand.
exec 9>"$WS/operator_ui/.locks/ui.lock"

# Chromium asks the desktop portal for its settings at start. GNOME's portal needs gnome-shell, which the kiosk
# does not run: without this it waits 25 s. The GTK portal answers at once.
export XDG_CURRENT_DESKTOP=HHRP
mkdir -p "$HOME/.config/xdg-desktop-portal"
printf '[preferred]\ndefault=gtk\n' > "$HOME/.config/xdg-desktop-portal/hhrp-portals.conf"

# cage must use the display controller (vc4), not the render-only v3d; their card numbers can change.
for c in /sys/class/drm/card[0-9]; do
  case "$(basename "$(readlink -f "$c/device/driver")")" in vc4*) export WLR_DRM_DEVICES="/dev/dri/$(basename "$c")";; esac
done
echo "display ${WLR_DRM_DEVICES:-auto}"
# Turn off needs this session to be the active local one (logind / polkit): "yes" here, not "challenge".
echo "may power off: $(busctl call org.freedesktop.login1 /org/freedesktop/login1 org.freedesktop.login1.Manager CanPowerOff 2>&1)"
[ -f "$PIN" ] || echo "no service PIN in $PIN: the service menu is off"

set +u
source /opt/ros/jazzy/setup.bash
source "$WS/install/setup.bash"
set -u
export ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST
unset RMW_IMPLEMENTATION

keep() {  # name command...: run it; when it exits, start it again (a crash must not leave the device dead)
  local name=$1; shift
  while :; do
    echo "$(date -Is) start $name"
    "$@" >>"$LOGS/$name.log" 2>&1 </dev/null
    echo "$(date -Is) $name exited ($?): starting again in 3 s"
    sleep 3
  done
}
export -f keep
export LOGS

start_stack() {  # each in its own process group (setsid), so stop_stack reaches every child
  if ! flock -n 9; then
    echo "$(date -Is) another operator UI holds the lock: scanner software not started here"
    return
  fi
  setsid bash -c 'keep "$@"' _ core ros2 launch laser_hhrp hhrp.launch.py holes:=false ui:=false \
    "nano_port:=$NANO_PORT" 9>&- &
  echo $! > "$CTL/core.pid"
  setsid bash -c 'keep "$@"' _ backend "$WS/install/laser_hhrp_ui/lib/laser_hhrp_ui/server" --port 8080 \
    --static "$WS/src/laserHHRP-ros2/ui_web/dist" --power-off-command "systemctl poweroff" \
    --reboot-command "systemctl reboot" \
    --service-pin-file "$PIN" --kiosk-control "$CTL" 9>&- &
  echo $! > "$CTL/backend.pid"
}

stop_stack() {  # backend first (it stops the detector), then the core; SIGTERM, then SIGKILL after 20 s
  local name pid
  for name in backend core; do
    pid="$(cat "$CTL/$name.pid" 2>/dev/null)" || continue
    kill -TERM -- "-$pid" 2>/dev/null
    for _ in $(seq 1 40); do kill -0 -- "-$pid" 2>/dev/null || break; sleep 0.5; done
    kill -KILL -- "-$pid" 2>/dev/null
    rm -f "$CTL/$name.pid"
    echo "$(date -Is) stopped $name"
  done
  flock -u 9
}

watch_requests() {  # restart is done here; desktop ends the screen, the main loop does the rest
  while sleep 0.5; do
    case "$(cat "$CTL/request" 2>/dev/null)" in
      restart)
        rm -f "$CTL/request"
        echo "$(date -Is) request: restart"
        stop_stack; start_stack ;;
      desktop)       # close Chromium: kiosk-browser.sh then ends, and cage exits by itself
        pkill -TERM -f -- "--user-data-dir=$HOME/snap/chromium/common/hhrp-kiosk" 2>/dev/null ;;
    esac
  done
}

wifi_watch() {  # Every 20 s: does the router answer? Changes are logged with the Wi-Fi state. Reconnect (allowed
                # from this, the active session) when the router stops answering, and when there is no connection
                # for 40 s: on 5 GHz radar channels the association can take > 25 s at boot; NetworkManager then
                # gives up for good ("no-secrets") until someone reconnects by hand (2026-11-21).
  local gw dev con state last="" offline=0
  while sleep 20; do
    read -r gw dev < <(ip -4 route show default 2>/dev/null | awk '{print $3, $5; exit}')
    if [ -z "${gw:-}" ]; then state="no default route"
    elif ping -c 3 -W 2 -I "$dev" "$gw" >/dev/null 2>&1; then state="router $gw answers on $dev"
    else state="router $gw NOT answering on $dev"
    fi
    if [ "$state" != "$last" ]; then
      echo "$(date -Is) network: $state ($(iw dev "${dev:-wlan0}" get power_save 2>&1 | tr -d '\t'); $(iw dev "${dev:-wlan0}" link 2>&1 | grep -E 'SSID|freq' | tr -d '\t' | tr '\n' ' '))"
      last="$state"
    fi
    if [ "$state" = "no default route" ]; then
      offline=$((offline + 1))
      if [ "$offline" -ge 2 ]; then   # device connect also clears NetworkManager's autoconnect block
        if nmcli device connect wlan0 >/dev/null 2>&1; then echo "$(date -Is) network: wlan0 connected again"
        else echo "$(date -Is) network: connecting wlan0 failed (next try in 40 s)"; fi
        offline=0; last=""
      fi
      continue
    fi
    offline=0
    case "$state" in *NOT*)
      con="$(nmcli -t -g GENERAL.CONNECTION device show "$dev" 2>/dev/null)"
      if [ -n "$con" ] && nmcli connection up "$con" >/dev/null 2>&1; then
        echo "$(date -Is) network: reconnected $con"
      else
        echo "$(date -Is) network: reconnecting ${con:-$dev} failed"
      fi
      last=""; sleep 30 ;;
    esac
  done
}

desktop() {  # the Ubuntu desktop as in ubuntu-wayland.desktop, until the developer logs out. GNOME's Log Out
             # ends the whole login session: GDM's timed login (install.sh) then starts the kiosk again.
  echo "$(date -Is) desktop mode"
  env XDG_CURRENT_DESKTOP=ubuntu:GNOME XDG_SESSION_DESKTOP=ubuntu DESKTOP_SESSION=ubuntu \
    GNOME_SHELL_SESSION_MODE=ubuntu /usr/bin/gnome-session --session=ubuntu 9>&- </dev/null
  echo "$(date -Is) desktop closed ($?): back to the operator screen"
}

# The session ends (logout, power off): stop the scanner software cleanly; the screen and the watcher go with it.
trap 'echo "$(date -Is) session ending"; echo desktop > "$CTL/request"
      pkill -TERM -f -- "--user-data-dir=$HOME/snap/chromium/common/hhrp-kiosk"; stop_stack
      kill $(jobs -p) 2>/dev/null; exit 0' TERM INT HUP     # the next session start clears the request

start_stack
watch_requests &            # keeps descriptor 9: a restart takes the lock again
wifi_watch 9>&- &
while :; do
  if [ "$(cat "$CTL/request" 2>/dev/null)" = desktop ]; then
    rm -f "$CTL/request"
    stop_stack
    desktop
    start_stack
  fi
  cage -s -- "$HERE/kiosk-browser.sh" 9>&- &
  echo $! > "$CTL/cage.pid"
  wait $!
  echo "$(date -Is) screen closed ($?)"
  sleep 1
done
