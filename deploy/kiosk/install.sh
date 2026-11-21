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
  echo "Done: the next login is the Ubuntu desktop (the GNOME autostart runs start_hardware.sh)."
  exit 0
fi

grep -qi '^AutomaticLoginEnable *= *true' /etc/gdm3/custom.conf || echo "WARNING: GDM autologin is not on" >&2
command -v cage >/dev/null || apt-get install -y cage
chmod +x "$HERE/kiosk-session.sh" "$HERE/kiosk-browser.sh"
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
