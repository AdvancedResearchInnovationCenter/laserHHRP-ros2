# laserHHRP — laser-based roller inspection handheld

A hand-held roller carrying a Micro-Epsilon **scanCONTROL LLT2900-10/BL** laser line profiler.
While the operator rolls it over a part, the roller's **AMT222C-V** encoder gives the position, an
**MPU6050** IMU the orientation and motion, and every laser profile is placed along the scan
axis. The profiles become a 3D point cloud for inspection (e.g. rivet flushness).

| Folder | Runs on | What it is |
| --- | --- | --- |
| [`arduino_nano/`](arduino_nano/README.md) | Arduino Nano | Firmware: encoder, IMU, laser trigger, USB link to the Pi |
| [`laserHHRP/`](laserHHRP/README.md) | Raspberry Pi 5 (ROS 2) | ROS 2 package `laser_hhrp`: drivers, synchronization, calibration, recording, RViz operator panel |
| [`hole_detector/`](hole_detector/README.md) | Raspberry Pi 5 (ROS 2) | ROS 2 package `hole_detector` (Python): finds holes or rivet heads (`mode:=holes|rivets`) during the scan from `scan/frame`, publishes `inspection/result` and the RViz displays; started by `hhrp.launch.py` (`holes:=false` to leave it out) |

The two parts only share the serial protocol (`arduino_nano/protocol.h` ↔
`laserHHRP/include/laser_hhrp/serial_protocol.hpp`). Developer docs for every node:
[`laserHHRP/docs/`](laserHHRP/docs/); synchronization:
[`laserHHRP/docs/synchronization.md`](laserHHRP/docs/synchronization.md).

## Setup overview

| Step | Where | Once / when |
| --- | --- | --- |
| [1. Hardware and wiring](#1-hardware-and-wiring) | device | once |
| [2. Ubuntu and ROS 2 on the Pi](#2-ubuntu-and-ros-2-on-the-raspberry-pi-5) | Pi | once |
| [3. scanCONTROL SDK](#3-scancontrol-sdk) | Pi | once |
| [4. Build the ROS 2 package](#4-build-the-ros-2-package) | Pi | once, and after every change |
| [5. Laser network](#5-laser-network) | Pi | once per scanner |
| [6. Arduino Nano firmware](#6-arduino-nano-firmware) | PC or Pi | once, and after firmware changes |
| [7. Nano serial port](#7-nano-serial-port-on-the-pi) | Pi | once |
| [8. Operator UI](#8-operator-ui-on-a-pc-or-on-the-pi) | PC (or Pi with a screen) | once |
| [9. First run](#9-first-run-step-by-step) | device | once per device |
| [10. Everyday use](#10-everyday-use) · [11. Simulation](#11-without-hardware-simulation) · [12. Troubleshooting](#12-troubleshooting) | | |

## 1. Hardware and wiring

| Component | Connection |
| --- | --- |
| Raspberry Pi 5 | Ethernet → PoE injector LAN port (laser data); USB → Arduino Nano |
| scanCONTROL LLT2900-10/BL | PoE cable to the injector (power + data); trigger input In1 ← Nano D3 (hardware sync only) |
| Arduino Nano (ATmega328P) | USB to the Pi (power + data, 1 Mbaud) |
| AMT222C-V encoder (12 bit, SPI) | SPI to the Nano, 5 V from the Nano |
| MPU6050 IMU (GY-521 board) | I2C to the Nano |
| Power | 48 V supply → main switch → fuse/protection → (1) PoE injector for the laser, (2) 48→5 V DC-DC for the Pi (5 V, up to 5 A) and the Nano |

Nano pins:

| Nano pin | Connects to |
| --- | --- |
| D2 | AMT22 /CS |
| D11 / D12 / D13 | AMT22 MOSI / MISO / SCLK |
| 5V, GND | AMT22 +5 V, GND; MPU6050 VCC, GND |
| A4 / A5 | MPU6050 SDA / SCL (AD0 → GND: address 0x68) |
| D3 | Laser multi-function socket **In1** (pin 6, yellow) — hardware sync only |
| GND | Laser **GND-In1** (pin 4, green) — hardware sync only |

Two synchronization modes, chosen when starting (section 10):

- **Software sync** (default): no trigger wire; the laser runs free at 120 profiles/s and each
  profile gets the encoder position at its exposure time.
- **Hardware sync**: D3 → In1 and GND → GND-In1 wired; the Nano triggers one profile every 4
  encoder counts (92 µm of travel).

## 2. Ubuntu and ROS 2 on the Raspberry Pi 5

Tested: **Ubuntu 24.04 (64-bit) + ROS 2 Jazzy**. Ubuntu 22.04 + ROS 2 Humble works too: write
`humble` instead of `jazzy` below.

1. Flash **Ubuntu Server 24.04 LTS (64-bit)** for Raspberry Pi with Raspberry Pi Imager. In its
   settings set the hostname, user, Wi-Fi (optional) and enable SSH. No screen is needed.
2. Boot, log in over SSH and update:

   ```bash
   sudo apt update && sudo apt full-upgrade -y && sudo reboot
   ```

3. Install ROS 2 Jazzy (official guide:
   <https://docs.ros.org/en/jazzy/Installation/Ubuntu-Install-Debs.html>):

   ```bash
   sudo apt install -y software-properties-common curl
   sudo add-apt-repository -y universe
   export ROS_APT_SOURCE_VERSION=$(curl -s https://api.github.com/repos/ros-infrastructure/ros-apt-source/releases/latest | grep -F "tag_name" | awk -F\" '{print $4}')
   curl -L -o /tmp/ros2-apt-source.deb "https://github.com/ros-infrastructure/ros-apt-source/releases/download/${ROS_APT_SOURCE_VERSION}/ros2-apt-source_${ROS_APT_SOURCE_VERSION}.$(. /etc/os-release && echo ${UBUNTU_CODENAME:-${VERSION_CODENAME}})_all.deb"
   sudo dpkg -i /tmp/ros2-apt-source.deb
   sudo apt update
   sudo apt install -y ros-jazzy-ros-base ros-dev-tools
   echo "source /opt/ros/jazzy/setup.bash" >> ~/.bashrc && source ~/.bashrc
   ```

4. Packages this project needs:

   ```bash
   sudo apt install -y git build-essential cmake pkg-config \
     ros-jazzy-std-srvs ros-jazzy-sensor-msgs ros-jazzy-geometry-msgs ros-jazzy-pluginlib \
     libaravis-dev libusb-1.0-0-dev meson ninja-build
   sudo rosdep init 2>/dev/null; rosdep update
   ```

RViz is **not** needed on the Pi when the operator UI runs on a PC (section 8).

## 3. scanCONTROL SDK

The Micro-Epsilon **scanCONTROL Linux SDK** (LGPL-3.0) is installed once, separately from the
ROS 2 package. Get it from Micro-Epsilon (download area of the scanCONTROL product page, or the
CD supplied with the scanner) and copy the SDK folder (it contains `libmescan/` and `libllt/`) to
the Pi, e.g. `~/scancontrol-linux-sdk`. Build and install `libmescan` first, then `libllt`:

```bash
cd ~/scancontrol-linux-sdk/libmescan
meson setup builddir && ninja -C builddir && sudo ninja -C builddir install

cd ~/scancontrol-linux-sdk/libllt
# Ubuntu 24.04 (newer GLib): llt.h includes Aravis inside extern "C" and does not compile;
# including Aravis first fixes it without changing the SDK:
CXXFLAGS="$(pkg-config --cflags aravis-0.8) -include arv.h" meson setup builddir
ninja -C builddir && sudo ninja -C builddir install

sudo ldconfig
pkg-config --modversion llt mescan    # prints 1.0.0 twice: installed
```

Without the SDK the package still builds, but the laser driver reports "built without the
scanCONTROL SDK" (the simulation still works).

## 4. Build the ROS 2 package

```bash
mkdir -p ~/ros2_ws/src && cd ~/ros2_ws/src
git clone https://github.com/AhmedHumais/laserHHRP-ros2.git
cd ~/ros2_ws
rosdep install --from-paths src -y --ignore-src
colcon build
echo "source ~/ros2_ws/install/setup.bash" >> ~/.bashrc && source ~/.bashrc
```

With the SDK installed, the build output contains `scanCONTROL SDK found: building the real laser
driver`. Check the build:

```bash
colcon test --packages-select laser_hhrp && colcon test-result --verbose   # unit tests
```

After changing code or `laserHHRP/config/hhrp.yaml`: `cd ~/ros2_ws && colcon build` again.

## 5. Laser network

The scanner has a **fixed IP address** (no DHCP); the Pi's Ethernet must be on its subnet. The
script below finds the scanner's address while it boots and configures `eth0` permanently.

1. Connect the PoE injector's LAN port to the Pi's Ethernet. Log in over **Wi-Fi** (or a local
   screen): reconfiguring `eth0` drops an SSH session that runs over it (the script refuses unless
   `--force`).
2. Run:

   ```bash
   cd ~/ros2_ws/src/laserHHRP-ros2/laserHHRP
   sudo scripts/setup_scanner_network_rpi.sh       # on a development PC: setup_scanner_network_pc.sh
   ```

3. When it prints `POWER-CYCLE THE SCANNER NOW`, unplug the injector's power and plug it back in
   (within 90 s). The script then detects the scanner's IP, gives the Pi address `.1` on that
   subnet (no gateway, so Wi-Fi keeps the internet route), enlarges the network receive buffers,
   checks the link speed, pings the scanner and writes the IP into `config/hhrp.yaml`
   (`laser.interface`).
4. Rebuild (`cd ~/ros2_ws && colcon build`) so the installed parameter file has the new IP.

IP already known: `sudo scripts/setup_scanner_network_rpi.sh -s 192.168.144.15`.

## 6. Arduino Nano firmware

Flash the firmware once, and again after every change in `arduino_nano/`. Stop the ROS 2 nodes
first: they keep the serial port open.

**Option A — Arduino IDE (any PC):**

1. Install the Arduino IDE (<https://www.arduino.cc/en/software>) and connect the Nano by USB.
2. Open `arduino_nano/arduino_nano.ino`.
3. *Tools → Board*: **Arduino Nano**; *Processor*: **ATmega328P (Old Bootloader)** for most
   clones, **ATmega328P** for a genuine/recent Nano; *Port*: the Nano's port.
4. Click *Upload*.

**Option B — PlatformIO (command line, works on the Pi itself, no screen needed):**

```bash
sudo apt install -y python3-venv
python3 -m venv ~/.pio-venv && ~/.pio-venv/bin/pip install platformio      # once
cd ~/ros2_ws/src/laserHHRP-ros2/arduino_nano
~/.pio-venv/bin/pio run -t upload                  # old bootloader (most clones)
~/.pio-venv/bin/pio run -e nano_new -t upload      # new bootloader (if the line above fails)
# several serial devices: add --upload-port /dev/ttyUSB0
```

The settings (pins, sample rate, encoder resolution) are constants at the top of
`arduino_nano.ino`; see [arduino_nano/README.md](arduino_nano/README.md).

## 7. Nano serial port on the Pi

```bash
sudo usermod -aG dialout $USER        # access to /dev/ttyUSB*, /dev/ttyACM*; log out and in again
ls /dev/ttyUSB* /dev/ttyACM*          # CH340/FTDI Nano: /dev/ttyUSB0 (default); original Nano: /dev/ttyACM0
```

Optional fixed name `/dev/hhrp_nano` (vendor id from `lsusb`: CH340 = 1a86, FTDI = 0403):

```bash
echo 'SUBSYSTEM=="tty", ATTRS{idVendor}=="1a86", SYMLINK+="hhrp_nano"' | sudo tee /etc/udev/rules.d/90-hhrp-nano.rules
sudo udevadm control --reload && sudo udevadm trigger
```

Then start with `nano_port:=/dev/hhrp_nano`.

## 8. Operator UI (on a PC or on the Pi)

The operator panel is an RViz plugin of this package. It talks to the Pi only through ROS 2, so it
can run on any computer on the same network.

**On a PC (when the Pi has no screen):**

1. Install ROS 2 with RViz on the PC (Ubuntu 24.04 + Jazzy or 22.04 + Humble, the
   `ros-<distro>-desktop` package) — the PC's ROS 2 distribution should match the Pi's.
2. Build the package on the PC as in section 4 (the SDK is not needed there).
3. PC and Pi on the same network (Ethernet or Wi-Fi), with the same `ROS_DOMAIN_ID` (default 0;
   if other ROS systems share the network, put `export ROS_DOMAIN_ID=<n>` in `~/.bashrc` on both).
4. Start the nodes on the Pi **without** the UI (`ros2 launch laser_hhrp hhrp.launch.py ui:=false`),
   then on the PC:

   ```bash
   ros2 topic list                 # /scan/status must be listed: the PC sees the Pi
   ros2 launch laser_hhrp ui.launch.py
   ```

**On the Pi with a touchscreen:** install `ros-jazzy-desktop` on the Pi and rebuild the package (the
panel is only built when RViz is installed). `ros2 launch laser_hhrp hhrp.launch.py` from the
Pi's desktop then opens the UI together with the nodes (launch argument `ui`, default `true`).

## 9. First run, step by step

1. **Nano.** Connect the Nano and start the nodes: `ros2 launch laser_hhrp hhrp.launch.py`
   (add `ui:=false` on a Pi without a screen and start the UI on the PC, section 8).
   `ros2 topic echo /imu/data` shows about 9.81 m/s² on the axis pointing up;
   `ros2 topic echo /nano/encoder` changes when you turn the roller by hand. Without the laser the
   panel shows "laser profiler disconnected" (expected).
2. **Laser.** With the network set up (section 5) the log shows `scanner connected` and, every
   5 s, `profiles: 120 /s received`. The RViz *Live profile* display shows the laser line.
3. **Calibrate the gyro.** Put the device down, keep it still and press *Calibrate gyro* (or
   `ros2 service call /calibration/start std_srvs/srv/Trigger`). This writes
   `~/laser_hhrp/calibration.yaml`. Scanning is refused until this is done.
4. **Roller direction and diameter.** Roll forward: the position in the panel must increase
   (otherwise set `encoder.direction_sign: -1` in `config/hhrp.yaml`). Measure the roller and set
   `encoder.roller_diameter_m`, or let step 5 measure it.
5. **Extrinsic calibration (recommended).** On a flat, level surface with a ruler, press
   *Calibrate extrinsics* and follow the prompts in the panel: hold still; roll forward 10 cm;
   backward 20 cm; forward 30 cm; holding still after each. This measures the effective roller
   diameter and encoder direction (used from then on) and enables the device pose.
6. **First scan.** *Start scan*, roll, *Stop scan*. The session is in
   `~/laser_hhrp/scans/<date-time>/`; `ros2 run laser_hhrp read_session.py ~/laser_hhrp/scans/<date-time>`
   prints a summary.
7. **Hardware sync (optional).** Wire D3 → In1 and GND → GND-In1, start with
   `hardware_trigger:=true`, scan and roll: there must be no "missing profiles" or "cannot be
   matched" warnings.
8. Scan a flat reference surface and a gauge with a known step to check the scan-axis scaling and
   height accuracy before inspecting real parts.

## 10. Everyday use

```bash
ros2 launch laser_hhrp hhrp.launch.py                         # all nodes + operator UI, software sync
ros2 launch laser_hhrp hhrp.launch.py hardware_trigger:=true  # hardware sync (D3 -> In1 wired)
ros2 launch laser_hhrp hhrp.launch.py ui:=false               # without the UI (Pi without a screen)
ros2 launch laser_hhrp hhrp.launch.py nano_port:=/dev/ttyACM0 # another serial port
ros2 launch laser_hhrp ui.launch.py                           # the UI alone (e.g. on a PC)
```

Launch arguments (combine freely): `ui` (default `true`), `hardware_trigger` (`false`),
`simulate` (`false`), `nano_port` (`/dev/ttyUSB0`), `params_file` (`config/hhrp.yaml`).

Panel buttons (each is a service: `ros2 service call /<name> std_srvs/srv/Trigger`):

| Button | Service |
| --- | --- |
| Start scan (refused until the gyro is calibrated) | `scan/start` |
| Pause / resume | `scan/pause` |
| Stop scan | `scan/stop` |
| Zero position (not while scanning) | `scan/zero` |
| Calibrate gyro | `calibration/start` |
| Reset calibration | `calibration/reset` |
| Calibrate extrinsics | `calibration/extrinsics` |

- **Parameters:** `laserHHRP/config/hhrp.yaml` (rebuild after editing), or at run time, e.g.
  `ros2 param set /scan_controller encoder.counts_per_trigger 8`.
- **Data:** every scan is recorded to `~/laser_hhrp/scans/<date-time>/`.
- **Logs:** the launch terminal, or `ros2 run rqt_console rqt_console`.

Optional — start the nodes automatically when the Pi boots:

```bash
sudo tee /etc/systemd/system/laser-hhrp.service >/dev/null <<EOF
[Unit]
Description=laserHHRP ROS 2 nodes
After=network-online.target

[Service]
User=$USER
ExecStart=/bin/bash -c "source /opt/ros/jazzy/setup.bash && source $HOME/ros2_ws/install/setup.bash && ros2 launch laser_hhrp hhrp.launch.py ui:=false"
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl enable --now laser-hhrp
# stop it before flashing the Nano: sudo systemctl stop laser-hhrp
```

## 11. Without hardware (simulation)

All nodes except the drivers run unchanged; a simulator stands in for the Nano and the laser
(a rolling roller, a flat surface with one rivet head at 20 mm):

```bash
ros2 launch laser_hhrp hhrp.launch.py simulate:=true          # terminal 1 (add hardware_trigger:=true for hardware sync, ui:=false without a screen)
ros2 run laser_hhrp timed_scan.py 3                            # terminal 2: calibrate, scan 3 s, stop
ros2 run laser_hhrp check_session.py                           # checks positions, IMU and the rivet position
```

## 12. Troubleshooting

| Symptom | Check |
| --- | --- |
| Upload fails (`not in sync`) | The other bootloader (IDE: *Old Bootloader* processor; PlatformIO: `-e nano_new`); ROS 2 nodes stopped |
| "serial port did not open" | `nano_port`, dialout group (log out and in), cable |
| "Nano silent" | Firmware flashed? `nano.baud` in `config/hhrp.yaml` = `SERIAL_BAUD` in the firmware |
| "built without the scanCONTROL SDK" | Section 3, then `colcon build` again |
| "no scanCONTROL found" / "connect failed" | PoE power and cable; Pi on the scanner's subnet (section 5); `laser.interface` in `config/hhrp.yaml` |
| "scanner makes 18 profiles/s instead of …" | `sync.software_rate_hz` above the scanner's limit (2900, full field: ~140 Hz) |
| Hardware sync: "no profiles in 5 s" | Trigger wire D3 → In1, GND → GND-In1; roll while scanning |
| "profiles cannot be matched to triggers" | Trigger wire; laser input level (TTL 5 V) |
| Overspeed warnings | Roll slower (max speed = 120 Hz × pitch ≈ 11 mm/s by default), or a larger `encoder.counts_per_trigger` |
| RViz dies at start (`could not connect to display`, `symbol lookup error`) | No screen: start with `ui:=false`; started from a VS Code (snap) terminal: use a normal terminal |
| UI on the PC: "Waiting for the laser roller ..." | Same network and `ROS_DOMAIN_ID`; `ros2 topic list` on the PC shows `/scan/status`; firewall |
| "calibration required" | Calibrate the gyro (section 9, step 3) |

More detail: [laserHHRP/README.md](laserHHRP/README.md) (package, nodes, parameters, monitoring)
and [arduino_nano/README.md](arduino_nano/README.md) (firmware, protocol).
