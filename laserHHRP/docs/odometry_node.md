# odometry_node

Layer 2. Estimates the pose of the device (base frame: x forward, z up) relative to a zero
position from the calibrated IMU and the encoder, detects roller slip (roller turning without the
device moving), and provides the laser's pose in Nano time so the synchronizer can attach a pose
to each scan frame. It is active only once the extrinsic calibration exists.

Source: `src/odometry_node.cpp`, `include/laser_hhrp/odometry.hpp`, `include/laser_hhrp/pose.hpp`,
`include/laser_hhrp/rotation.hpp`, `include/laser_hhrp/calibration_data.hpp`.

## Interfaces

| Kind | Name | Type / default | Notes |
| --- | --- | --- | --- |
| sub | `imu/corrected` | `laser_hhrp/ImuSample` | calibrated |
| sub | `nano/encoder` | `laser_hhrp/EncoderSample` | |
| sub | `nano/clock` | `laser_hhrp/ClockMap` | for Pi stamps |
| sub | `calibration/status` | `laser_hhrp/CalibrationStatus` | reloads the file on change |
| pub | `base/pose` | `geometry_msgs/PoseStamped` | frame `odom`, one per IMU sample, Pi time |
| pub | `odometry/laser_pose` | `laser_hhrp/DevicePose` | laser pose, Nano time |
| pub | `odometry/slipping` | `std_msgs/Bool` | latched, on change |
| srv | `odometry/zero` | `std_srvs/Trigger` | pose → zero position (heading zero, tilt kept) |
| param | `calibration.file` | `~/laser_hhrp/calibration.yaml` | `~` expanded |

## Processing

```
on calibration/status c:
    key = state + extrinsics flag + message; ignore if unchanged
    if not (CALIBRATED and extrinsics_calibrated): filter = unconfigured (no pose), return
    load the calibration file; if no extrinsics there: warn, return
    filter.configure(extrinsics)          world = base at start; filter reset; pose = zero

on nano/encoder e:   queue it (the encoder is a little ahead of the IMU)

on imu/corrected m:
    feed every queued encoder sample with time <= m.time to filter.encoder(count, t)
    if not configured: return
    filter.imu(gyro, accel, t)
    publish base/pose  (stamp = ClockMap mapping of t, or now before the first map)
    laser = laser_pose(base, extrinsics)  publish odometry/laser_pose (Nano time)
    if slipping changed: log, publish odometry/slipping
```

### Filter (`Odometry` in odometry.hpp)

State: IMU→world orientation `q`, forward speed `v`, accelerometer bias along x `b`, 2×2
covariance `P`. `R_ib` = base→IMU rotation from the calibration.

```
imu(gyro, accel, t):                     dt = t − t_prev, skipped if <= 0 or > 0.1 s
    q = normalize(q ⊗ exp(gyro·dt))                                   gyro integration
    f_w = R(q)·accel
    if still and | |f_w| − g | < 0.5:                                   tilt pull towards gravity
        err = normalize(f_w) × (0,0,1);  q = normalize(exp(err·dt/τ) ⊗ q),  τ = 1 s
    at_rest = |gyro| < 0.05 and | |accel| − g | < 0.3
    still = rest for 0.2 s and (encoder stopped 0.2 s or slipping)
    x_w = R(base orientation)·(1,0,0)
    a = (R(q)·accel − (0,0,g)) · x_w                                    forward acceleration
    predict:  v += (a − b)·dt
              P = F P Fᵀ + Q,  F = [[1, −dt],[0, 1]],
              Q00 = 0.1²·dt²,  Q11 = 0.01²·dt
    if still: update(z = 0, r = 1e-6)                                   zero-velocity update
    if encoder not trusted: position += v·dt · x_w

encoder(count, t):                       dt skipped if <= 0 or > 0.5 s
    ds = sign · metres_per_count · Δcount ;  v_enc = ds/dt
    r = 0.005² + (metres_per_count/dt)²/3                              noise + quantization
    y = v_enc − v ;  nis = y²/(P00 + r)
    slip sample if (nis > 25 and |y| > 0.01) or |y| > 0.05:
        3 in a row -> slipping, undo the last 0.3 s of encoder distance (replace by the filter's)
        slipping for over max_slip_s (1 s): the filter's speed is what went wrong: v = v_enc, b = 0,
            P reset, slip over (reanchors + 1). Without it a drifted speed never recovered (2026-11-22:
            the pose ran off by km on 0.5 m rolls)
        encoder not trusted (IMU speed moves the base)
    else (accepted):
        while slipping and < 10 accepted in a row: update(v_enc, r) only
        else: slipping = false; update(v_enc, r); position += ds · x_w; remember ds − v·dt

update(z, r):                            H = [1, 0]
    s = P00 + r;  k = (P00/s, P10/s);  y = z − v
    v += k0·y;  b += k1·y
    P00 = (1−k0)P00;  P01 = P10 = (1−k0)P01;  P11 = P11 − k1·P01
```

- `laser_pose`: `p_laser = p_base + R(q_base)·laser_in_roller`, `q_laser = q_base ⊗ roller_from_laser`
  (identity until the laser extrinsics are measured, so laser pose = base pose).
- `zero()`: position and distance = 0; the yaw of the base x axis (`atan2(x_y, x_x)`) is removed
  from `q`, tilt kept.

## Edge cases

- No extrinsics: nothing is published except `odometry/slipping: false`; `odometry/zero` answers
  "calibrate the extrinsics first".
- At most 100 encoder samples are queued.
- Heading drifts slowly (gyro only, no magnetometer).

## Where in the code

`OdometryNode::follow_calibration/on_imu/publish_slip`, `to_ros()`; `Odometry::configure/imu/
encoder/update/undo_recent/advance/zero`; `laser_pose()` (pose.hpp).

## Known limitation (2026-11-22)

On the hand-held rolls of 2026-11-22 the filter's IMU speed drifted from the encoder's many times per scan
(about 0.5 m/s² unexplained forward acceleration) although the encoder was right (hole pitch and roundness):
10–20 false slips per scan even with the re-anchoring. So the operator warning is off
(`system.slip_warning: false`, scan_controller) and `base/pose` / `scan/pose` are not to be trusted until the filter
is validated against a known motion. Frame positions (the scan axis of every detection and measurement) come from
the encoder alone and are not affected. Replay harness: feed the bag's `imu/corrected` and `nano/encoder` in Nano
time order to `Odometry::encoder/imu`.
