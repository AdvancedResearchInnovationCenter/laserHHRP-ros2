#!/usr/bin/env bash
# Operator kiosk: at power-on the Pi logs in straight into the operator screen (no desktop, no terminal).
#   sudo ./install.sh            install (cage from apt, the session, autologin into it)
#   sudo ./install.sh --undo     back to the normal Ubuntu desktop at login
# GDM autologin of the user must already be on (/etc/gdm3/custom.conf). Reboot afterwards.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
USER_NAME="${SUDO_USER:-pi}"
UID_NUM="$(id -u "$USER_NAME")"
SESSION=/usr/share/wayland-sessions/hhrp-kiosk.desktop
ACCOUNT="/org/freedesktop/Accounts/User$UID_NUM"

set_session() {  # name type
  busctl call org.freedesktop.Accounts "$ACCOUNT" org.freedesktop.Accounts.User SetSession s "$1"
  busctl call org.freedesktop.Accounts "$ACCOUNT" org.freedesktop.Accounts.User SetSessionType s "$2"
  echo "login session of $USER_NAME: $(busctl get-property org.freedesktop.Accounts "$ACCOUNT" org.freedesktop.Accounts.User Session)"
}

if [ "${1:-}" = "--undo" ]; then
  set_session ubuntu ""
  rm -f "$SESSION"
  AUTO="$(getent passwd "$USER_NAME" | cut -d: -f6)/.config/autostart/hhrp-operator-ui.desktop"
  [ -f "$AUTO" ] && sed -i 's/^X-GNOME-Autostart-enabled=.*/X-GNOME-Autostart-enabled=true/' "$AUTO"
  echo "Done: the next login is the Ubuntu desktop (its autostart runs start_hardware.sh again)."
  exit 0
fi

grep -qi '^AutomaticLoginEnable *= *true' /etc/gdm3/custom.conf || echo "WARNING: GDM autologin is not on" >&2
command -v cage >/dev/null || apt-get install -y cage
chmod +x "$HERE/kiosk-session.sh" "$HERE/kiosk-browser.sh"
HOME_DIR="$(getent passwd "$USER_NAME" | cut -d: -f6)"
# Service menu PIN (developers): created once with 2580; change it with: echo 1234 > <file>
PIN="$HOME_DIR/aric_ws/operator_ui/service_pin"
if [ ! -f "$PIN" ]; then
  install -D -o "$USER_NAME" -g "$USER_NAME" -m 600 /dev/null "$PIN"
  echo 2580 > "$PIN"
  echo "service PIN 2580 written to $PIN: change it"
fi
# The old GNOME autostart of start_hardware.sh would start the scanner software again in desktop mode.
AUTO="$HOME_DIR/.config/autostart/hhrp-operator-ui.desktop"
if [ -f "$AUTO" ] && ! grep -q '^X-GNOME-Autostart-enabled=false' "$AUTO"; then
  sed -i 's/^X-GNOME-Autostart-enabled=.*/X-GNOME-Autostart-enabled=false/' "$AUTO"
  grep -q '^X-GNOME-Autostart-enabled=' "$AUTO" || echo 'X-GNOME-Autostart-enabled=false' >> "$AUTO"
  echo "GNOME autostart of start_hardware.sh turned off ($AUTO)"
fi
cat > "$SESSION" <<DESKTOP
[Desktop Entry]
Name=Laser roller operator screen
Comment=Only the operator UI: no desktop
Exec=$HERE/kiosk-session.sh
Type=Application
DesktopNames=HHRP
DESKTOP
set_session hhrp-kiosk wayland
echo "Done. Reboot: the Pi starts straight into the operator screen. Undo: sudo $0 --undo"
