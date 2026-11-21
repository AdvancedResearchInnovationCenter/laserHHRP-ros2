# Operator kiosk (Raspberry Pi)

At power-on the Pi logs in straight into the operator screen: no desktop, no terminal, nothing an operator can
close. GDM autologin starts the session `hhrp-kiosk`, which runs

| Part | How |
| --- | --- |
| core (`hhrp.launch.py holes:=false ui:=false nano_port:=…`) | started again if it exits |
| backend (`laser_hhrp_ui` server, port 8080, `--power-off-command "systemctl poweroff"`) | started again if it exits; it starts the detector |
| screen: Chromium full screen under [cage](https://github.com/cage-kiosk/cage) | opened again if it exits; shows *Starting the scanner…* until the backend answers |

The operator turns the device off with the ⏻ button in the header (with a confirmation). It is refused while a
scan runs or an analysis is pending. The gyro is calibrated from the screens (*Calibrate* / *Calibrate gyro*).

## Install / undo (on the Pi, once)

```bash
cd ~/aric_ws/src/laserHHRP-ros2/deploy/kiosk
sudo ./install.sh          # apt install cage, the session file, autologin into it; then reboot
sudo ./install.sh --undo   # back to the Ubuntu desktop (its autostart runs start_hardware.sh)
```

Maintenance: SSH as before. `Ctrl+Alt+F3` gives a text console on the device. The session uses the same lock
as `start_hardware.sh` / `start_simulation.sh`: while it runs, those refuse to start.

Logs: `~/aric_ws/operator_ui/logs/kiosk/` (`session.log`, `core.log`, `backend.log`, `browser.log`).
`session.log` shows the display card used and whether the session may power off (`CanPowerOff` must be `yes`).
