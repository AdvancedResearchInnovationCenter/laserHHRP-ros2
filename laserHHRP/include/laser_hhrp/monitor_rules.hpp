// System monitoring rules: from a snapshot of the system, the list of active faults.
// Used by scan_controller_node; a FATAL fault stops a running scan.
#pragma once

#include <string>
#include <vector>

namespace laser_hhrp {

enum class Severity { WARNING = 1, ERROR = 2, FATAL = 3 };

struct Fault {
  std::string source;  // laser, nano, imu, scan, sync, storage, calibration, odometry
  Severity severity;
  std::string message;
};

struct MonitorInput {
  bool scanning = false;
  // speed
  double speed_mps = 0;      // |roller speed|
  double max_speed_mps = 0;  // profile rate x pitch
  double warning_fraction = 0.8;
  double stalled_for_s = 0;  // time below the minimum speed while scanning
  double stall_timeout_s = 2;
  uint32_t overspeed_triggers = 0;
  bool software_sync = false;  // free-running laser: too fast spreads the scan lines, it skips no profile
  // IMU
  bool imu_ok = true;
  double tilt_rad = 0, tilt_limit_rad = 0.087;
  double rotation_radps = 0, vibration_limit_radps = 1.0;
  // devices
  bool laser_ok = true;
  bool nano_ok = true;
  bool encoder_ok = true;
  // synchronization
  uint32_t missing_profiles = 0;
  uint32_t missing_triggers = 0;
  uint32_t timing_mismatches = 0;
  bool sync_locked = true;
  uint32_t triggers_seen = 0;
  // storage
  double free_mb = 1e9, min_free_mb = 500;
  bool storage_write_failed = false;
  // calibration / odometry
  bool calibrated = true;
  bool slipping = false;
};

inline std::vector<Fault> evaluate(const MonitorInput& in) {
  using S = Severity;
  std::vector<Fault> f;
  if (!in.laser_ok) f.push_back({"laser", S::FATAL, "laser profiler disconnected"});
  if (!in.nano_ok) f.push_back({"nano", S::FATAL, "Nano (encoder/IMU) link lost"});
  if (in.nano_ok && !in.encoder_ok) f.push_back({"nano", S::FATAL, "encoder error"});
  if (in.nano_ok && !in.imu_ok) f.push_back({"imu", S::WARNING, "IMU error"});
  if (in.scanning) {
    if (in.max_speed_mps > 0 && in.speed_mps > in.max_speed_mps)
      f.push_back({"scan", S::ERROR, in.software_sync ? "scan speed too high: scan lines too far apart"
                                                      : "scan speed too high: profiles are skipped"});
    else if (in.max_speed_mps > 0 && in.speed_mps > in.warning_fraction * in.max_speed_mps)
      f.push_back({"scan", S::WARNING, "scan speed close to the maximum"});
    if (in.overspeed_triggers > 0)
      f.push_back({"scan", S::WARNING, "triggers fired late (overspeed)"});
    if (in.stalled_for_s > in.stall_timeout_s)
      f.push_back({"scan", S::WARNING, "encoder not moving (roller stalled)"});
    if (in.missing_profiles > 0) f.push_back({"sync", S::WARNING, "missing profiles"});
    if (in.missing_triggers > 0)
      f.push_back({"sync", S::WARNING, "profiles without trigger record"});
    if (in.timing_mismatches > 0)
      f.push_back({"sync", S::WARNING, "profile/trigger timing mismatch"});
    if (!in.sync_locked && in.triggers_seen > 50)
      f.push_back({"sync", S::ERROR,
                   "profiles cannot be matched to triggers (check the trigger cable)"});
  }
  if (in.imu_ok && in.tilt_rad > in.tilt_limit_rad)
    f.push_back({"imu", S::WARNING, "device tilt too large"});
  // While scanning only: between scans the operator carries the device (up to ~2 rad/s on 2026-11-22, while
  // scanning at most 0.2), which is no vibration and was shown as a warning on Ready and Results.
  if (in.scanning && in.imu_ok && in.rotation_radps > in.vibration_limit_radps)
    f.push_back({"imu", S::WARNING, "excess vibration"});
  if (in.slipping)
    f.push_back({"odometry", S::WARNING, "roller slip: the roller turns but the device does not move"});
  if (!in.calibrated)
    f.push_back({"calibration", S::WARNING,
                 "calibration required: hold the device still and press Calibrate gyro"});
  if (in.storage_write_failed)
    f.push_back({"storage", S::FATAL, "cannot write scan data"});
  else if (in.free_mb >= 0 && in.free_mb < in.min_free_mb)
    f.push_back({"storage", S::WARNING, "storage space low"});
  return f;
}

inline const char* severity_name(Severity s) {
  return s == Severity::FATAL ? "FATAL" : s == Severity::ERROR ? "ERROR" : "WARNING";
}

}  // namespace laser_hhrp
