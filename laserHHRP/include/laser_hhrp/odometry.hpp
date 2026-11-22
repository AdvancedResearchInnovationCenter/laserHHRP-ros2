// Odometry: the pose of the device relative to the zero position, from the calibrated IMU and
// the encoder. Used by odometry_node once the extrinsics are calibrated (IMU <-> base rotation,
// metres per count). Times are Nano clock. Frames: base = the device on the surface (x forward,
// z up); world = the base at the zero position.
//
// Orientation (per IMU sample)
//   q = q * rotation(gyro * dt)                       integrate the gyro (bias already removed)
//   at rest only: pull the tilt towards the measured gravity (complementary filter, time
//   constant tilt_tau_s); while rolling, accelerations would tilt it wrongly.
//   Heading is gyro only (no magnetometer): it drifts slowly.
//
// Rolled distance: Kalman filter with state [v = forward speed, b = accelerometer bias]
//   predict (IMU):    a = forward acceleration (world, gravity removed)
//                     v = v + (a - b) * dt,  P = F P F' + Q,  F = [[1, -dt], [0, 1]]
//   update (encoder): z = encoder speed = sign * metres_per_count * dcount / dt
//                     K = P H' / (H P H' + r),  H = [1, 0],  x = x + K (z - v)
//   at rest (IMU quiet and encoder stopped): update with z = 0 (zero-velocity update), which
//   also estimates the bias b.
//
// Roller slip: to reach a speed the device must accelerate, and the IMU feels that. An encoder
// speed the IMU cannot explain (normalized innovation y^2 / (P + r) above the gate, or |y| above
// max_speed_error_mps) is rejected; after slip_samples rejections in a row the roller is
// "slipping" (spun by hand, lifted, skidding): the counts then add no distance, the filter's
// speed does, and the distance of the last lookback_s is corrected. It ends after
// recover_samples accepted samples.
//
// Position: every distance step is added along the current base x axis (in the world frame).
#pragma once

#include <cmath>
#include <cstdint>

#include "laser_hhrp/calibration_data.hpp"
#include "laser_hhrp/rotation.hpp"

namespace laser_hhrp {

struct OdometrySettings {
  double tilt_tau_s = 1.0;            // accelerometer tilt correction time constant
  double tilt_accel_window = 0.5;     // | |a| - g | below this: accelerometer trusted for tilt
  double accel_noise_mps2 = 0.1;      // process noise of the forward acceleration (MPU6050)
  double bias_walk_mps2 = 0.01;       // accelerometer bias random walk [m/s^2/sqrt(s)]
  double encoder_speed_noise_mps = 0.005;
  double gate = 25.0;                 // innovation gate (NIS, 5 sigma)
  double still_gyro_radps = 0.05;     // rest detection (zero-velocity update)
  double still_accel_mps2 = 0.3;
  double still_s = 0.2;
  double min_speed_error_mps = 0.01;  // innovations below this are never slip
  double max_speed_error_mps = 0.05;  // innovations above this are always slip
  int slip_samples = 3;               // consecutive rejected encoder samples -> slipping
  int recover_samples = 10;           // consecutive accepted samples -> slip over
  double lookback_s = 0.3;            // on slip, encoder distance of this past replaced by the IMU's
  double max_slip_s = 1.0;            // a "slip" longer than this is the filter's speed gone wrong, not the
                                      // roller (2026-11-22 rolls: residual tilt/bias error; without this it
                                      // never recovered and the pose ran off by km): speed re-anchored to the
                                      // encoder, slip over. 0 = never.
};

struct OdometryPose {
  Vec3 position_m{};           // base origin in the world frame (zero position)
  Quat orientation{1, 0, 0, 0};  // base -> world
  double distance_m = 0;       // rolled distance (signed, along base x)
  double speed_mps = 0;        // filtered forward speed
  bool slipping = false;       // encoder rejected: roller turning without the base moving
  double slip_m = 0;           // encoder distance rejected so far (diagnostics)
  bool still = false;          // device at rest
  int reanchors = 0;           // slips that outlasted max_slip_s: filter speed reset to the encoder (diagnostics)
};

class Odometry {
 public:
  explicit Odometry(const OdometrySettings& s = OdometrySettings()) : s_(s) {}

  /// Uses a calibration (extrinsics must be calibrated) and restarts at the zero position.
  void configure(const Extrinsics& e) {
    e_ = e;
    imu_from_base_ = transpose(quat_to_mat(e.base_from_imu));
    q_wi_ = e.base_from_imu;  // world = base at start (level until the tilt correction)
    reset_filter();
    pose_ = OdometryPose();
    pose_.orientation = base_orientation();
    configured_ = e.calibrated && e.metres_per_count > 0;
  }
  bool configured() const { return configured_; }

  /// Zero position: origin here, heading zero (tilt kept).
  void zero() {
    pose_.position_m = Vec3{0, 0, 0};
    pose_.distance_m = 0;
    const Vec3 x = mul(quat_to_mat(base_orientation()), Vec3{1, 0, 0});
    const double yaw = std::atan2(x[1], x[0]);
    q_wi_ = quat_normalized(quat_mul(quat_from_rotvec({0, 0, -yaw}), q_wi_));
    pose_.orientation = base_orientation();
  }

  /// IMU sample (bias-corrected gyro rad/s, accel m/s^2) at Nano time t_ns.
  void imu(const Vec3& gyro, const Vec3& accel, int64_t t_ns) {
    // dt since the previous sample (skip the first one and gaps over 0.1 s)
    if (!configured_) return;
    const double dt = last_imu_ns_ != 0 ? (t_ns - last_imu_ns_) * 1e-9 : 0.0;
    last_imu_ns_ = t_ns;
    if (dt <= 0 || dt > 0.1) return;
    const double g = e_.gravity_mps2;
    // Orientation: gyro, then a small pull of the tilt towards gravity.
    q_wi_ = quat_normalized(quat_mul(q_wi_, quat_from_rotvec(scale(gyro, dt))));
    const Vec3 f_w = mul(quat_to_mat(q_wi_), accel);
    if (pose_.still && std::fabs(norm(f_w) - g) < s_.tilt_accel_window) {
      const Vec3 err = cross(normalized(f_w), Vec3{0, 0, 1});
      q_wi_ = quat_normalized(quat_mul(quat_from_rotvec(scale(err, dt / s_.tilt_tau_s)), q_wi_));
    }
    // Rest detection: gyro quiet and |accel| ~ g for still_s.
    const bool at_rest = norm(gyro) < s_.still_gyro_radps &&
                         std::fabs(norm(accel) - g) < s_.still_accel_mps2;
    still_s_ = at_rest ? still_s_ + dt : 0.0;
    // At rest: IMU quiet and the encoder stopped, or turning while slipping (motion ruled out).
    pose_.still = still_s_ >= s_.still_s && (encoder_still_s_ >= s_.still_s || pose_.slipping);
    // Kalman prediction: forward acceleration = (accel in world - gravity) . base x axis.
    const Vec3 x_w = mul(quat_to_mat(base_orientation()), Vec3{1, 0, 0});
    const double a = dot(sub(mul(quat_to_mat(q_wi_), accel), Vec3{0, 0, g}), x_w);
    v_ += (a - b_) * dt;
    // P = F P F' + Q with F = [[1, -dt], [0, 1]]
    const double p00 = P_[0][0] - dt * (P_[1][0] + P_[0][1]) + dt * dt * P_[1][1];
    const double p01 = P_[0][1] - dt * P_[1][1];
    P_[0][0] = p00 + s_.accel_noise_mps2 * s_.accel_noise_mps2 * dt * dt;
    P_[0][1] = P_[1][0] = p01;
    P_[1][1] += s_.bias_walk_mps2 * s_.bias_walk_mps2 * dt;
    if (pose_.still) update(0.0, 1e-6);  // zero-velocity update
    // Without a trusted encoder the filter's speed moves the base.
    if (!encoder_trusted_) {
      advance(v_ * dt, x_w);
      if (pose_.slipping) slip_.add(-v_ * dt, x_w);   // undone if the slip turns out to be the filter's error
    }
    pose_.speed_mps = v_;
    pose_.orientation = base_orientation();
  }

  /// Encoder sample (multi-turn count) at Nano time t_ns: speed measurement + slip check.
  void encoder(int32_t count, int64_t t_ns) {
    if (!configured_) return;
    if (!has_count_) {
      has_count_ = true;
      last_count_ = count;
      last_enc_ns_ = t_ns;
      return;
    }
    const double dt = (t_ns - last_enc_ns_) * 1e-9;
    const int32_t dc = count - last_count_;
    last_count_ = count;
    last_enc_ns_ = t_ns;
    if (dt <= 0 || dt > 0.5) return;
    encoder_still_s_ = dc == 0 ? encoder_still_s_ + dt : 0.0;
    const double ds = e_.encoder_direction_sign * e_.metres_per_count * dc;
    const double v_enc = ds / dt;
    // Speed quantization (one count per sample) adds to the measurement noise.
    const double q = e_.metres_per_count / dt;
    const double r = s_.encoder_speed_noise_mps * s_.encoder_speed_noise_mps + q * q / 3.0;
    // Innovation y = encoder speed - filter speed; NIS = y^2 / (P + r).
    const double y = v_enc - v_;
    const double nis = y * y / (P_[0][0] + r);
    // Not explainable by the IMU: rejected (slip after slip_samples in a row).
    if ((nis > s_.gate && std::fabs(y) > s_.min_speed_error_mps) ||
        std::fabs(y) > s_.max_speed_error_mps) {
      rejected_ = rejected_ + 1;
      accepted_ = 0;
      pose_.slip_m += std::fabs(ds);
      const Vec3 x_w = mul(quat_to_mat(base_orientation()), Vec3{1, 0, 0});
      if (rejected_ >= s_.slip_samples && !pose_.slipping) {
        pose_.slipping = true;
        slip_start_ns_ = t_ns;
        slip_ = Track();
        undo_recent(t_ns);  // the slip began before it was certain
      }
      if (pose_.slipping) slip_.add(ds, x_w);
      if (pose_.slipping && s_.max_slip_s > 0 && (t_ns - slip_start_ns_) * 1e-9 > s_.max_slip_s) {
        // The encoder kept turning smoothly for max_slip_s against the filter: the filter is wrong. The slip's
        // distance becomes the encoder's again.
        restore(slip_);
        slip_ = Track();
        v_ = v_enc;
        b_ = 0;
        P_[0][0] = r;
        P_[0][1] = P_[1][0] = 0;
        P_[1][1] = 0.05 * 0.05;
        pose_.slipping = false;
        encoder_trusted_ = true;
        rejected_ = accepted_ = 0;
        ++pose_.reanchors;
        return;
      }
      encoder_trusted_ = false;  // the IMU speed moves the base meanwhile
      return;
    }
    // Accepted.
    accepted_ = accepted_ + 1;
    rejected_ = 0;
    if (pose_.slipping && accepted_ < s_.recover_samples) {
      update(v_enc, r);  // follow the encoder again, distance still from the filter
      return;
    }
    pose_.slipping = false;
    encoder_trusted_ = true;
    const double ds_filter = v_ * dt;  // the IMU's opinion, before the encoder corrects it
    update(v_enc, r);
    const Vec3 x_w = mul(quat_to_mat(base_orientation()), Vec3{1, 0, 0});
    advance(ds, x_w);
    recent_[recent_head_] = Recent{t_ns, ds - ds_filter, x_w};
    recent_head_ = (recent_head_ + 1) % kRecent;
  }

  const OdometryPose& pose() const { return pose_; }

 private:
  Quat base_orientation() const {
    return quat_normalized(mat_to_quat(mul(quat_to_mat(q_wi_), imu_from_base_)));
  }
  void reset_filter() {
    v_ = b_ = 0;
    P_[0][0] = 1e-4;
    P_[0][1] = P_[1][0] = 0;
    P_[1][1] = 0.05 * 0.05;
    still_s_ = encoder_still_s_ = 0;
    has_count_ = false;
    encoder_trusted_ = true;
    rejected_ = accepted_ = 0;
    for (Recent& r : recent_) r = Recent();
    last_imu_ns_ = last_enc_ns_ = 0;
  }
  /// Kalman update with a speed measurement z (variance r); H = [1, 0].
  void update(double z, double r) {
    const double s = P_[0][0] + r;
    const double k0 = P_[0][0] / s, k1 = P_[1][0] / s;
    const double y = z - v_;
    v_ += k0 * y;
    b_ += k1 * y;  // P carries the sign of v' = a - b
    const double p00 = P_[0][0], p01 = P_[0][1], p11 = P_[1][1];
    P_[0][0] = (1 - k0) * p00;
    P_[0][1] = P_[1][0] = (1 - k0) * p01;
    P_[1][1] = p11 - k1 * p01;
  }
  /// Replaces the encoder distance of the last lookback_s by the filter's (slip detected late).
  void undo_recent(int64_t now_ns) {
    for (Recent& r : recent_) {
      if (r.t_ns == 0 || now_ns - r.t_ns > static_cast<int64_t>(s_.lookback_s * 1e9)) continue;
      advance(-r.excess_m, r.x_w);
      if (pose_.slipping) slip_.add(r.excess_m, r.x_w);
      pose_.slip_m += std::fabs(r.excess_m);
      r = Recent();
    }
  }
  void advance(double ds, const Vec3& x_w) {
    pose_.distance_m += ds;
    pose_.position_m = add(pose_.position_m, scale(x_w, ds));
  }
  /// What a slip changed in the pose, to be put right if the slip is judged false: the encoder distance it
  /// rejected and the filter distance it added instead (sum of ds and of x_w * ds).
  struct Track {
    double ds = 0;
    Vec3 p{0, 0, 0};
    void add(double d, const Vec3& x_w) { ds += d; p = Vec3{p[0] + x_w[0] * d, p[1] + x_w[1] * d, p[2] + x_w[2] * d}; }
  } slip_;
  void restore(const Track& t) {
    pose_.distance_m += t.ds;
    pose_.position_m = add(pose_.position_m, t.p);
  }

  OdometrySettings s_;
  Extrinsics e_;
  bool configured_ = false;
  Mat3 imu_from_base_ = identity3();
  Quat q_wi_{1, 0, 0, 0};  // IMU -> world
  double v_ = 0, b_ = 0;
  int64_t slip_start_ns_ = 0;
  double P_[2][2] = {{1e-4, 0}, {0, 0.0025}};
  double still_s_ = 0, encoder_still_s_ = 0;
  bool has_count_ = false, encoder_trusted_ = true;
  int32_t last_count_ = 0;
  int rejected_ = 0, accepted_ = 0;
  int64_t last_imu_ns_ = 0, last_enc_ns_ = 0;
  OdometryPose pose_;
  struct Recent {
    int64_t t_ns = 0;
    double excess_m = 0;  // accepted encoder distance minus the filter's
    Vec3 x_w{};
  };
  static constexpr int kRecent = 128;  // > lookback_s at 200 Hz
  Recent recent_[kRecent];
  int recent_head_ = 0;
};

}  // namespace laser_hhrp
