# laser_hhrp — ROS 2 package for the laser roller (Raspberry Pi 5)

ROS 2 (Humble or Jazzy) package that runs on the Raspberry Pi 5 of the laser-based roller
inspection handheld. It reads the Arduino Nano (encoder, IMU, trigger records) and the
Micro-Epsilon scanCONTROL laser, synchronizes every laser profile with the roller position and
the IMU, calibrates the IMU, records each scan, and provides the RViz operator panel.

## Build

```bash
# once: the scanCONTROL SDK (separate, not part of this package) - see "scanCONTROL SDK" below
mkdir -p ~/ros2_ws/src && cd ~/ros2_ws/src
cp -r /path/to/laserHHRP .          # or git clone
cd ~/ros2_ws
rosdep install --from-paths src -y --ignore-src
colcon build
source install/setup.bash
```

Without the SDK the package still builds: `laser_driver_node` then reports "built without the
scanCONTROL SDK" and you can work with `simulate:=true`.

## Run

```bash
ros2 launch laser_hhrp hhrp.launch.py                          # hardware, software sync, with the UI
ros2 launch laser_hhrp hhrp.launch.py hardware_trigger:=true   # hardware sync (Nano D3 -> laser In1)
ros2 launch laser_hhrp hhrp.launch.py simulate:=true           # no hardware at all
ros2 launch laser_hhrp hhrp.launch.py ui:=false                # without RViz (Pi without a screen)
ros2 launch laser_hhrp hhrp.launch.py mode:=holes              # holes only (default: both, detection.mode in hhrp.yaml)
ros2 launch laser_hhrp hhrp.launch.py network:=true            # topics visible to other hosts (RViz on a PC)
ros2 launch laser_hhrp hhrp.launch.py holes:=false             # without hole/rivet detection
ros2 launch laser_hhrp ui.launch.py                            # RViz with the operator panel alone (e.g. on a PC)
# which RViz displays start shown: config/rviz_displays.yaml (or ui.launch.py displays_file:=...)
```

Launch arguments: `ui` (default `true`: also starts RViz with the operator panel), `simulate`,
`hardware_trigger`, `nano_port` (default `/dev/ttyUSB0`), `params_file` (default `config/hhrp.yaml`),
`holes` (default `true`: also starts hole/rivet detection, see `../hole_detector/`, `../rivet_detector/`),
`mode` (`holes`, `rivets` or `both`; default `detection.mode` in `config/hhrp.yaml`, `both`), `holes_out_dir`
(default `~/aric_ws/src/laserHHRP-ros2/hole_detector/hole_detector_out`), `network` (default `false`).

`network`: ROS discovery stays on this host (`ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST`), because other
machines' nodes on the Wi-Fi pulled the laser data and stopped scans. For RViz on a PC: on the Pi
`hhrp.launch.py network:=true`; on the PC, same `ROS_DOMAIN_ID`,
`ros2 daemon stop; ROS_AUTOMATIC_DISCOVERY_RANGE=SUBNET ros2 launch laser_hhrp ui.launch.py`.
After changing the range in a shell, run `ros2 daemon stop` once.

Operator actions (the panel buttons call the same services):

```bash
ros2 service call /calibration/start std_srvs/srv/Trigger       # gyro: hold the device still ~2 s
ros2 service call /calibration/extrinsics std_srvs/srv/Trigger  # rolling procedure (prompts in /scan/status)
ros2 service call /calibration/reset std_srvs/srv/Trigger
ros2 service call /scan/start std_srvs/srv/Trigger              # refused until calibrated
ros2 service call /scan/pause std_srvs/srv/Trigger              # pause / resume
ros2 service call /scan/stop std_srvs/srv/Trigger
ros2 service call /scan/zero std_srvs/srv/Trigger               # zero the encoder (not while scanning)
ros2 service call /odometry/zero std_srvs/srv/Trigger           # zero the pose
ros2 topic echo /scan/status
```

Test the whole chain without hardware:

```bash
ros2 launch laser_hhrp hhrp.launch.py simulate:=true     # terminal 1 (add hardware_trigger:=true for the other mode, ui:=false without a screen)
ros2 run laser_hhrp timed_scan.py 3                       # terminal 2: calibrate, scan 3 s, stop
ros2 run laser_hhrp check_session.py                      # frames, positions, IMU, simulated rivet at 20 mm
colcon test --packages-select laser_hhrp && colcon test-result --verbose   # unit tests
```

## How the nodes fit together

The nodes follow the layers of the architecture document ("Laser-Based Roller Inspection
Handheld"). They only talk through topics and services.

```
 Arduino Nano ──USB──► nano_driver ──► nano/encoder, nano/trigger, nano/imu, nano/clock
 scanCONTROL ──Eth──► laser_driver ──► laser/raw (+ laser/profile live view)
        (simulator replaces both when simulate:=true)

 nano/imu ──► acquisition ──► imu/corrected, scan/motion
              odometry ──────► base/pose, odometry/laser_pose
 nano/* + laser/raw + imu/corrected ──► synchronizer ──► scan/frame ──► point_cloud ──► scan/cloud ──► inspection
                                                                   └──► recorder (scans/<date>/)
 calibration ──► calibration/status          scan_controller ──► nano/command, scan/status ◄── RViz panel
```

## Files

### Nodes (`src/`)

| File | Layer | What it does |
| --- | --- | --- |
| `nano_driver_node.cpp` | 1 hardware interface | Serial link to the Arduino Nano: publishes encoder samples, trigger records, IMU samples (also as `sensor_msgs/Imu` on `imu/data`) and status; sends commands; maps the Nano clock to the Pi clock |
| `laser_driver_node.cpp` | 1 hardware interface | The scanCONTROL laser: publishes every profile (`laser/raw`) stamped with its exposure time, a live view (`laser/profile`, PointCloud2) and the connection status; logs the profile rate |
| `scancontrol.cpp` | 1 hardware interface | Driver class for the scanCONTROL SDK (connect, configure trigger/exposure/resolution, receive profiles) used by `laser_driver_node` |
| `simulator_node.cpp` | 1 (simulation) | Stands in for both drivers without hardware: a rolling roller, the Nano's trigger logic and a laser seeing a flat surface with one rivet head |
| `acquisition_node.cpp` | 2 data acquisition | Applies the IMU calibration (`imu/corrected`); roller speed, tilt and vibration (`scan/motion`) |
| `odometry_node.cpp` | 2 data acquisition | Pose of the device from IMU + encoder (`base/pose`), roller slip detection, laser pose for the synchronizer |
| `synchronizer_node.cpp` | 3 synchronization | One `ScanFrame` per profile: position along the scan axis, IMU and pose at the same instant; hardware or software sync |
| `point_cloud_node.cpp` | 4 point cloud | **To be developed.** Now merges frames into a simple cloud (`scan/cloud`, `scan/cloud_view`) |
| `inspection_node.cpp` | 5 inspection | **To be developed.** Receives the cloud, will publish `inspection/result` |
| `recorder_node.cpp` | 6 storage | One folder per scan: `frames.bin`, `results.json`, `session.json`; free disk space |
| `calibration_node.cpp` | calibration | Gyro calibration and the extrinsic (rolling) calibration; loads/saves the calibration file |
| `scan_controller_node.cpp` | 7 + monitoring | Scan state and the operator services; monitoring rules (a fatal fault stops the scan); `scan/status` for the UI |
| `gui/operator_panel.cpp` | 7 user interface | RViz panel: state, calibration, speed, tilt, faults and the touch buttons |

### Algorithms (`include/laser_hhrp/`)

Plain C++ without ROS: easy to read and unit-tested in `test/test_logic.cpp`.

| File | What it does |
| --- | --- |
| `frame_pairer.hpp` | Hardware sync: matches trigger records with laser profiles by number (locks the counter offset from the timing pattern), detects lost ones, checks timing |
| `software_pairer.hpp` | Software sync: gives each free-running profile the encoder count interpolated at its exposure time |
| `clock_sync.hpp` | Nano clock → Pi clock (offset + drift) from clock exchanges |
| `scanner_clock.hpp` | Laser's own shutter clock → Pi clock (removes network jitter) |
| `serial_protocol.hpp` | Packet layout and checksum of the Nano link (same as `arduino_nano/protocol.h`) |
| `scancontrol.hpp` | Interface of the scanCONTROL driver class (`src/scancontrol.cpp`) |
| `gyro_calibrator.hpp` | Gyro bias from ~2 s of stillness; restarts if the device moves |
| `extrinsic_calibrator.hpp` | Rolling procedure (+10, −20, +30 cm): IMU ↔ base rotation, metres per count, encoder direction |
| `calibration_data.hpp` | The calibration values and their YAML file (load/save) |
| `odometry.hpp` | Orientation filter + Kalman filter on the rolled distance, slip detection |
| `pose.hpp` | Laser pose from the base pose; interpolation between two poses |
| `rotation.hpp` | Small vector / matrix / quaternion helpers |
| `monitor_rules.hpp` | System monitoring: which faults are active for the current state |
| `encoder_scale.hpp` | Metres per count and direction: measured value after the extrinsic calibration, else the parameters |
| `profile_cloud.hpp` | A laser profile as `sensor_msgs/PointCloud2` |

### Messages (`msg/`)

| File | Topic(s) |
| --- | --- |
| `EncoderSample.msg`, `TriggerRecord.msg`, `ImuSample.msg`, `NanoStatus.msg`, `NanoCommand.msg`, `ClockMap.msg` | Nano data (`nano/*`), all with the Nano time |
| `RawProfile.msg`, `LaserStatus.msg` | `laser/raw`, `laser/status` |
| `Motion.msg`, `DevicePose.msg` | `scan/motion`, `odometry/laser_pose` |
| `ScanFrame.msg`, `SyncStats.msg` | `scan/frame` (the synchronized output), `scan/sync` |
| `CalibrationStatus.msg` | `calibration/status` |
| `ScanStatus.msg`, `StorageStatus.msg`, `InspectionResult.msg` | `scan/status`, `scan/storage`, `inspection/result` |

### Developer docs (`docs/`)

| File | Contents |
| --- | --- |
| `synchronization.md` | The synchronization contract: the three clocks, clock mapping, hardware and software sync step by step |
| `<node_name>.md` | One per node (and `operator_panel.md`): interfaces, step-by-step processing, formulas, edge cases |

### Other

| File | What it does |
| --- | --- |
| `launch/hhrp.launch.py` | Starts all nodes (or the simulator instead of the drivers) and, unless `ui:=false`, the UI |
| `launch/ui.launch.py` | Starts RViz with the operator panel |
| `config/hhrp.yaml` | All parameters (sensor, scan speed, system, calibration, inspection) |
| `config/hhrp.rviz` | RViz layout: operator panel, live profile, 3D scan |
| `plugins.xml` | Registers the operator panel with RViz |
| `scripts/timed_scan.py` | Calibrate if needed, scan N seconds, stop (testing) |
| `scripts/read_session.py` | Reads a recorded scan folder (also usable as a Python module) |
| `scripts/check_session.py` | Checks a simulated scan (positions, IMU, rivet position) |
| `scripts/setup_scanner_network*.sh` | Puts the Pi's (or PC's) Ethernet on the scanner's subnet and writes its IP to `config/hhrp.yaml` |
| `test/test_logic.cpp` | Unit tests of the algorithms |

## Synchronization

**Hardware sync** (`hardware_trigger:=true`, wire Nano D3 → laser In1, GND → GND-In1): the Nano
pulses the laser every `encoder.counts_per_trigger` counts (default 4 counts = 92 µm with a 30 mm
roller and a 12-bit encoder), never faster than `laser.max_profile_rate_hz` (120). Each pulse is
reported as a trigger record; `frame_pairer.hpp` pairs record *k* with profile *k* using the laser's
profile counter. Profiles are equally spaced in distance; maximum speed = rate × pitch (11 mm/s by
default; faster rolling is flagged overspeed).

**Software sync** (default, no trigger wire): the laser runs free at `sync.software_rate_hz`
(120). Each profile's exposure time (from the laser's own clock) is matched against the Nano's
240 Hz encoder samples and the count interpolated (`software_pairer.hpp`). Profiles are equally
spaced in time.

In both modes the IMU is interpolated at each profile's instant in the Nano clock, and the frame
is stamped in Pi time. Nothing else changes between the modes.

## Calibration

The calibration file (`calibration.file`, default `~/laser_hhrp/calibration.yaml`) is per device.

1. **Gyro** (required before scanning): put the device down, press *Calibrate gyro* and keep it
   still ~2 s. It restarts if the device moves and gives up after 20 s.
2. **Extrinsics** (optional, after the gyro): on a flat surface with a ruler, press *Calibrate
   extrinsics* and follow the prompts: hold still, roll forward 10 cm, backward 20 cm, forward
   30 cm, holding still after each. Gives the IMU ↔ base rotation, the effective roller diameter
   (used from then on instead of `encoder.roller_diameter_m`) and the encoder direction. It also
   enables the pose (`base/pose`, `scan/pose`).
3. *Reset calibration* deletes the file.

## Recorded data

`system.storage_path` (default `~/laser_hhrp/scans`) gets one folder per scan:
`frames.bin` (every `ScanFrame`), `results.json`, `session.json` (times, counts, all parameters).

```python
from read_session import read_frames          # scripts/read_session.py
for f in read_frames("~/laser_hhrp/scans/20261011-190838"):
    f.position_m, f.x_m, f.z_m, f.accel_mps2
```

## Monitoring

`scan_controller_node` checks 10 times per second and shows the most severe fault in the panel:

| Condition | Severity |
| --- | --- |
| laser disconnected, Nano link lost, encoder error, storage write failed | fatal — scan stopped |
| speed above maximum, profiles cannot be matched to triggers | error |
| speed > 80 % of maximum, overspeed triggers, roller stalled, missing profiles / triggers, timing mismatch, tilt, vibration, IMU error, roller slip, storage low, calibration required | warning |

## scanCONTROL SDK

Installed once, separately from this package (Micro-Epsilon scanCONTROL Linux SDK, LGPL-3.0):

```bash
sudo apt install meson ninja-build libaravis-dev libusb-1.0-0-dev
git clone https://github.com/Micro-Epsilon/scanCONTROL-Linux-SDK.git    # or the SDK archive
cd scanCONTROL-Linux-SDK/libmescan && meson builddir && cd builddir && ninja && sudo ninja install
cd ../../libllt
# Ubuntu 24.04 (newer GLib): llt.h includes Aravis inside extern "C", which fails to compile;
# including Aravis first fixes it without changing the SDK
CXXFLAGS="$(pkg-config --cflags aravis-0.8) -include arv.h" meson setup builddir
ninja -C builddir && sudo ninja -C builddir install
sudo ldconfig
pkg-config --modversion llt      # 1.0.0: found
```

CMake finds it through pkg-config (`llt`). Then rebuild this package.

Scanner network (fixed IP, no DHCP): `sudo scripts/setup_scanner_network_rpi.sh` (Pi, `eth0`) or
`..._pc.sh` (PC, `eno1`); power-cycle the scanner when asked.

Nano serial port: `sudo usermod -aG dialout $USER` (log in again). Optional fixed name:
`echo 'SUBSYSTEM=="tty", ATTRS{idVendor}=="1a86", SYMLINK+="hhrp_nano"' | sudo tee /etc/udev/rules.d/90-hhrp-nano.rules`,
then `nano_port:=/dev/hhrp_nano`.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| "serial port did not open" | `nano_port`, dialout group, cable |
| "Nano silent" | Baud (`nano.baud` = `SERIAL_BAUD` in the firmware) |
| "no scanCONTROL found" | Ethernet / PoE, the Pi on the scanner's subnet (`setup_scanner_network_rpi.sh`) |
| "scanner makes 18 profiles/s instead of …" | `sync.software_rate_hz` above the scanner's limit (2900 full field: ~140 Hz) |
| hardware sync: "no profiles in 5 s" | Trigger wire D3 → In1, GND → GND-In1; roll the roller while scanning |
| "profiles cannot be matched to triggers" | Trigger wire; laser input level 5 V |
| overspeed warnings | Roll slower, larger `encoder.counts_per_trigger` |
