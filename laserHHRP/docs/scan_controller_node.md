# scan_controller_node

The scan's state machine and the system monitor. It answers the operator services (start, stop,
pause, zero), sends the matching commands to the Nano, evaluates the monitoring rules 10 times
per second (a fatal fault stops the scan) and publishes `scan/status`, which the RViz panel shows
and which tells the synchronizer and the recorder when a scan starts and stops.

Source: `src/scan_controller_node.cpp`, rules in `include/laser_hhrp/monitor_rules.hpp`.

## Interfaces

| Kind | Name | Type | Notes |
| --- | --- | --- | --- |
| sub | `nano/status` | `laser_hhrp/NanoStatus` | Nano alive if received < 1.5 s ago |
| sub | `laser/status` | `laser_hhrp/LaserStatus` | laser ok if received < 3 s ago and `connected` |
| sub | `scan/motion` | `laser_hhrp/Motion` | speed, tilt, vibration |
| sub | `scan/sync` | `laser_hhrp/SyncStats` | frames, missing, locked |
| sub | `scan/storage` | `laser_hhrp/StorageStatus` | free space, write failure, session folder |
| sub | `calibration/status` | `laser_hhrp/CalibrationStatus` | latched |
| sub | `odometry/slipping` | `std_msgs/Bool` | latched |
| pub | `nano/command` | `laser_hhrp/NanoCommand` | START / STOP / ZERO |
| pub | `scan/status` | `laser_hhrp/ScanStatus` | every 200 ms and on every state change |
| srv | `scan/start`, `scan/stop`, `scan/pause`, `scan/zero` | `std_srvs/Trigger` | |

| Parameter | Default | Use |
| --- | --- | --- |
| `encoder.counts_per_trigger` | 4 | pitch, sent with START |
| `encoder.roller_diameter_m`, `encoder.counts_per_turn`, `encoder.direction_sign` | 0.030, 4096, 1 | scale, unless the extrinsic calibration measured it (`EncoderScale`) |
| `imu.period_ms` | 0 | sent with START (0 = firmware default) |
| `laser.max_profile_rate_hz` | 120.0 | trigger rate limit and max speed |
| `sync.hardware_trigger`, `sync.software_rate_hz`, `sync.max_line_spacing_m` | false, 120.0, 0.0005 | which max-speed formula; software sync: 60 mm/s |
| `speed.warning_fraction` | 0.8 | |
| `speed.min_speed_mps` | 0.001 | stall threshold |
| `speed.stall_timeout_s` | 2.0 | |
| `system.tilt_limit_rad` | 0.087 | |
| `system.slip_warning` | true (hhrp.yaml: false) | `odometry/slipping` raises the "roller slip" warning; off while the odometry gives false slips (docs/odometry_node.md) |
| `system.vibration_limit_radps` | 1.0 | while scanning only (carrying the device between scans is no vibration) |
| `system.storage_min_free_mb` | 500.0 | |

All parameters are read when used, so `ros2 param set` takes effect immediately (the pitch at the
next START).

## Formulas

```
metres_per_count = π · roller_diameter / counts_per_turn      (or the calibrated value)
max_speed_mps    = max_profile_rate_hz · metres_per_count · counts_per_trigger      (hardware sync)
                 = sync.software_rate_hz · sync.max_line_spacing_m                (software sync: the laser runs
                   free and skips no profile; faster only spreads the scan lines; late triggers are not reported)
min_trigger_period_us = ceil(1e6 / max_profile_rate_hz)        (0 if the rate is 0)
```
Defaults: 23.0 µm/count × 4 = 92 µm pitch, × 120 Hz = 11.0 mm/s.

## Scan states

```
          start (calibrated, Nano + laser ok)
  IDLE ─────────────────────────────► SCANNING ◄──pause──┐
   ▲  ▲                                 │   │            │
   │  └──────────── stop ───────────────┘   └──pause──► PAUSED
   │  └──────────── stop ───────────────────────────────┘
   │
 ERROR ◄── fatal fault while SCANNING / PAUSED     (start works again from ERROR)
 calibration reset while scanning → stop → IDLE
```

## Services

```
scan/start:
    refuse "already scanning"            if SCANNING or PAUSED
    refuse "calibration running…" / "calibration required…" unless calibration == CALIBRATED
    refuse "devices not ready"           unless nano fresh and laser ok
    send START; state = SCANNING; reset stall timer; publish status
scan/stop:   stop_scan(→ IDLE)
    refuse "not scanning" unless SCANNING or PAUSED
    send STOP; state = final; publish
    reply "stopped: <frames> frames in <session folder>"
scan/pause:
    SCANNING → send STOP,  PAUSED,   "paused"
    PAUSED   → send START, SCANNING, "resumed"   (Nano restarts its trigger numbering; the
                                                  synchronizer re-locks, positions keep their zero)
    else refuse "not scanning"
scan/zero:
    refuse while SCANNING; send ZERO ("encoder zeroed")
send(command):
    START carries counts_per_trigger, min_trigger_period_us, imu_period_ms
```

## Calibration callback — `on_calibration()`

```
update the encoder scale
if new state == CALIBRATION_REQUIRED and old == CALIBRATED: stop_scan(→ IDLE)   # reset
if old == CALIBRATING and new != CALIBRATING: remember its message for 20 s     # outcome shown
store
```

## Monitor — every 100 ms

```
build MonitorInput:
    speed = |motion.speed|, max_speed (formula above)
    stalled_for = time since speed < min_speed (only while SCANNING, else timer reset)
    overspeed_triggers = nano.overspeed (only while SCANNING)
    nano_ok = nano fresh;  encoder_ok, imu_ok from nano status
    laser_ok, tilt, rotation, missing/timing counters, sync_locked = locked or software sync,
    triggers_seen, free_mb (1e9 until the recorder reports), write_failed, calibrated, slipping
faults = evaluate(input)
if any FATAL and SCANNING/PAUSED: stop_scan(→ ERROR), log the fault
```

### Rules (`evaluate`)

| Condition | Source | Severity |
| --- | --- | --- |
| laser not ok | laser | FATAL |
| Nano not fresh | nano | FATAL |
| Nano ok, encoder not ok | nano | FATAL |
| Nano ok, IMU not ok | imu | WARNING |
| *scanning:* speed > max | scan | ERROR |
| *scanning:* speed > warning_fraction · max (else-branch) | scan | WARNING |
| *scanning:* overspeed triggers > 0 | scan | WARNING |
| *scanning:* stalled_for > stall_timeout | scan | WARNING |
| *scanning:* missing profiles > 0 / missing triggers > 0 / timing mismatches > 0 | sync | WARNING |
| *scanning:* hardware sync not locked and > 50 triggers | sync | ERROR |
| IMU ok, tilt > tilt limit | imu | WARNING |
| IMU ok, rotation > vibration limit | imu | WARNING |
| roller slipping | odometry | WARNING |
| not calibrated | calibration | WARNING |
| storage write failed | storage | FATAL |
| else free space < min | storage | WARNING |

## Status — every 200 ms and after each change

```
frames, missing counts, position = scan/sync
speed, tilt = scan/motion;  max_speed (formula)
active_faults = number of faults;  faults[] = "SEVERITY source: message"
message = the most severe fault's message
    overridden by the calibration message while CALIBRATING,
    or by the last calibration outcome for 20 s after it ended
```

## Edge cases

- Before the first `nano/status`, the Nano counts as lost (`nano_ok` false): start is refused.
- The rules run all the time, so device faults show in the panel even when idle; only FATAL
  faults during a scan change the state.
- Calibration reset during a scan stops it (IDLE, not ERROR).

## Where in the code

`ScanControllerNode::start_scan`, `stop_scan`, `pause`, `send`, `max_speed_mps`, `on_calibration`,
`monitor`, `publish_status` · `evaluate()` in `monitor_rules.hpp` · `EncoderScale`.
