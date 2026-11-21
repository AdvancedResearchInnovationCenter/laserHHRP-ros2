# Operator kiosk (Raspberry Pi)

At power-on the Pi logs in straight into the operator screen: no desktop, no terminal, nothing an operator can
close. GDM autologin starts the session `hhrp-kiosk`, which runs

| Part | How |
| --- | --- |
| core (`hhrp.launch.py holes:=false ui:=false nano_port:=…`) | started again if it exits |
| backend (`laser_hhrp_ui` server, port 8080, `--power-off-command "systemctl poweroff"`) | started again if it exits; it starts the detector |
| screen: Chromium full screen under [cage](https://github.com/cage-kiosk/cage) | opened again if it exits; shows *Starting the scanner…* until the backend answers |

The operator turns the device off with **⋮ → Turn off… → Turn off** in the header (deliberately not one tap). The
⋮ menu is shown only while no scan runs or is being analysed, and the backend refuses it then as well. The gyro
is calibrated from the screens (*Calibrate* / *Calibrate gyro*).

## Developers: ⋮ → Service… → PIN

The PIN is in `~/aric_ws/operator_ui/service_pin` (4–8 digits; `install.sh` writes `2580` if there is none:
change it, `echo 1234 > ~/aric_ws/operator_ui/service_pin`, used at once). Five wrong PINs lock it for 60 s.
The service menu shows the network (Wi-Fi name, IP addresses) and offers:

| Action | What happens |
| --- | --- |
| **Desktop mode** | the scanner software (core, backend, detector) and the screen stop; the Ubuntu desktop opens: terminal, Settings → Wi-Fi, `start_hardware.sh` by hand (the lock is free). **Log out** and the operator screen comes back with everything started; a reboot also always comes back to it. |
| **Restart scanner software** | core and backend (with the detector) stop and start again; the screen stays and reconnects |

Both are refused during a scan or a pending analysis. Wi-Fi without the desktop: over SSH or on the text
console (`Ctrl+Alt+F3`, with a keyboard) `nmcli device wifi connect "<name>" password "<password>"`.

## Install / undo (on the Pi, once)

```bash
cd ~/aric_ws/src/laserHHRP-ros2/deploy/kiosk
sudo ./install.sh          # apt install cage, the session file, autologin into it; then reboot
sudo ./install.sh --undo   # back to the Ubuntu desktop (its autostart runs start_hardware.sh)
```

Maintenance: SSH as before, or desktop mode. `Ctrl+Alt+F3` gives a text console on the device. The session holds
the same lock as `start_hardware.sh` / `start_simulation.sh` (released in desktop mode). `install.sh` turns off the
old GNOME autostart of `start_hardware.sh`, so desktop mode does not start the scanner software by itself.

Logs: `~/aric_ws/operator_ui/logs/kiosk/` (`session.log`, `core.log`, `backend.log`, `browser.log`).
`session.log` shows the display card used and whether the session may power off (`CanPowerOff` must be `yes`).
