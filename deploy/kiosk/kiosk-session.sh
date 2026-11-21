#!/usr/bin/env bash
# Operator kiosk session (GDM autologin, Wayland): the laser roller core, the operator UI backend and the
# operator screen in Chromium under cage. Nothing else: no desktop, no terminal, nothing to close.
# A process that exits is started again. Turning off: the screen's Turn off (systemctl poweroff).
# Installed by install.sh in this folder. Logs: ~/aric_ws/operator_ui/logs/kiosk/
set -u
HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
WS="${HHRP_WS:-$HOME/aric_ws}"
LOGS="$WS/operator_ui/logs/kiosk"
NANO_PORT="${NANO_PORT:-/dev/ttyUSB0}"
mkdir -p "$LOGS" "$WS/operator_ui/.locks"
exec >>"$LOGS/session.log" 2>&1
echo "=== $(date -Is) kiosk session"

# The start scripts and this session never run together (same lock as start_hardware.sh / start_simulation.sh).
exec 9>"$WS/operator_ui/.locks/ui.lock"
OWN_STACK=1
flock -n 9 || { OWN_STACK=""; echo "another operator UI holds the lock: showing only the screen"; }

# Chromium asks the desktop portal for its settings at start. GNOME's portal needs gnome-shell, which this
# session does not run: without this it waits 25 s. The GTK portal answers at once.
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

if [ -n "$OWN_STACK" ]; then
  keep core ros2 launch laser_hhrp hhrp.launch.py holes:=false ui:=false "nano_port:=$NANO_PORT" &
  keep backend "$WS/install/laser_hhrp_ui/lib/laser_hhrp_ui/server" --port 8080 \
    --static "$WS/src/laserHHRP-ros2/ui_web/dist" --power-off-command "systemctl poweroff" &
fi
exec cage -s -- "$HERE/kiosk-browser.sh"
