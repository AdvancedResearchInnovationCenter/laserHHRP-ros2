// scan_controller_node (layer 7 logic + system monitoring): the scan state (IDLE, SCANNING,
// PAUSED, ERROR), the operator services, the monitoring rules (monitor_rules.hpp; a FATAL fault
// stops the scan) and the scan status for the UI.
//
// Services (std_srvs/Trigger): scan/start, scan/stop, scan/pause (pause / resume), scan/zero
// Subscribes  nano/status, laser/status, scan/motion, scan/sync, scan/storage,
//             calibration/status, odometry/slipping
// Publishes   nano/command (to the Nano), scan/status (5 Hz and on every change)
#include <cmath>

#include <rclcpp/rclcpp.hpp>
#include <std_msgs/msg/bool.hpp>
#include <std_srvs/srv/trigger.hpp>

#include "laser_hhrp/encoder_scale.hpp"
#include "laser_hhrp/monitor_rules.hpp"
#include "laser_hhrp/msg/calibration_status.hpp"
#include "laser_hhrp/msg/laser_status.hpp"
#include "laser_hhrp/msg/motion.hpp"
#include "laser_hhrp/msg/nano_command.hpp"
#include "laser_hhrp/msg/nano_status.hpp"
#include "laser_hhrp/msg/scan_status.hpp"
#include "laser_hhrp/msg/storage_status.hpp"
#include "laser_hhrp/msg/sync_stats.hpp"

namespace laser_hhrp {

using Status = msg::ScanStatus;
using Command = msg::NanoCommand;
using Calibration = msg::CalibrationStatus;
using Trigger = std_srvs::srv::Trigger;

class ScanControllerNode : public rclcpp::Node {
 public:
  // ---- setup: parameters, the latest state of every input, services, timers ----
  // monitor() runs at 10 Hz, publish_status() at 5 Hz (and on every state change).
  ScanControllerNode() : Node("scan_controller"), scale_(*this) {
    declare_parameter("encoder.counts_per_trigger", 4);
    declare_parameter("imu.period_ms", 0);
    declare_parameter("laser.max_profile_rate_hz", 120.0);
    declare_parameter("speed.warning_fraction", 0.8);
    declare_parameter("speed.min_speed_mps", 0.001);
    declare_parameter("speed.stall_timeout_s", 2.0);
    declare_parameter("system.tilt_limit_rad", 0.087);
    declare_parameter("system.vibration_limit_radps", 1.0);
    declare_parameter("system.storage_min_free_mb", 500.0);

    command_pub_ = create_publisher<Command>("nano/command", 10);
    status_pub_ = create_publisher<Status>("scan/status", 10);
    nano_sub_ = create_subscription<msg::NanoStatus>("nano/status", 10, [this](const msg::NanoStatus& m) {
      nano_ = m;
      nano_time_ = now();
    });
    laser_sub_ = create_subscription<msg::LaserStatus>("laser/status", 10, [this](const msg::LaserStatus& m) {
      laser_ = m;
      laser_time_ = now();
    });
    motion_sub_ = create_subscription<msg::Motion>("scan/motion", 10, [this](const msg::Motion& m) { motion_ = m; });
    stats_sub_ = create_subscription<msg::SyncStats>("scan/sync", 10, [this](const msg::SyncStats& m) { stats_ = m; });
    storage_sub_ = create_subscription<msg::StorageStatus>("scan/storage", 10, [this](const msg::StorageStatus& m) {
      storage_ = m;
      have_storage_ = true;
    });
    slip_sub_ = create_subscription<std_msgs::msg::Bool>(
        "odometry/slipping", rclcpp::QoS(1).transient_local(), [this](const std_msgs::msg::Bool& b) { slipping_ = b.data; });
    calibration_sub_ = create_subscription<Calibration>(
        "calibration/status", rclcpp::QoS(1).transient_local(), [this](const Calibration& c) { on_calibration(c); });

    start_srv_ = service("scan/start", [this](std::string& m) { return start_scan(m); });
    stop_srv_ = service("scan/stop", [this](std::string& m) { return stop_scan(m, Status::IDLE); });
    pause_srv_ = service("scan/pause", [this](std::string& m) { return pause(m); });
    zero_srv_ = service("scan/zero", [this](std::string& m) {
      if (state_ == Status::SCANNING) { m = "stop the scan first"; return false; }
      send(Command::ZERO);
      m = "encoder zeroed";
      return true;
    });

    monitor_timer_ = create_wall_timer(std::chrono::milliseconds(100), [this] { monitor(); });
    status_timer_ = create_wall_timer(std::chrono::milliseconds(200), [this] { publish_status(); });
  }

 private:
  rclcpp::Service<Trigger>::SharedPtr service(const std::string& name, std::function<bool(std::string&)> f) {
    return create_service<Trigger>(name, [f](const std::shared_ptr<Trigger::Request>,
                                             std::shared_ptr<Trigger::Response> res) {
      res->success = f(res->message);
    });
  }

  bool nano_fresh() { return nano_time_.nanoseconds() != 0 && (now() - nano_time_).seconds() < 1.5; }
  bool laser_ok() { return laser_time_.nanoseconds() != 0 && (now() - laser_time_).seconds() < 3.0 && laser_.connected; }
  bool active() const { return state_ == Status::SCANNING || state_ == Status::PAUSED; }

  // ---- operator actions ----
  // start: refused until calibrated and both devices report in; then START to the Nano.
  bool start_scan(std::string& message) {
    if (active()) { message = "already scanning"; return false; }
    if (calibration_.state != Calibration::CALIBRATED) {
      message = calibration_.state == Calibration::CALIBRATING
                    ? "calibration running: wait until it finishes"
                    : "calibration required: hold the device still and press Calibrate gyro";
      return false;
    }
    if (!nano_fresh() || !laser_ok()) { message = "devices not ready (Nano or laser)"; return false; }
    send(Command::START);
    state_ = Status::SCANNING;
    stalled_since_ = rclcpp::Time(0, 0, RCL_ROS_TIME);
    publish_status();
    message = "scanning";
    RCLCPP_INFO(get_logger(), "scan started, max speed %.1f mm/s", max_speed_mps() * 1e3);
    return true;
  }

  bool stop_scan(std::string& message, uint8_t final_state) {
    if (!active()) {
      message = "not scanning";
      if (final_state == Status::ERROR) state_ = Status::ERROR;
      return false;
    }
    send(Command::STOP);
    state_ = final_state;
    publish_status();
    message = "stopped: " + std::to_string(stats_.frames) + " frames in " + storage_.session_dir;
    RCLCPP_INFO(get_logger(), "scan stopped: %u frames, %u missing profiles", stats_.frames, stats_.missing_profiles);
    return true;
  }

  bool pause(std::string& message) {
    if (state_ == Status::SCANNING) {
      send(Command::STOP);
      state_ = Status::PAUSED;
      message = "paused";
    } else if (state_ == Status::PAUSED) {
      send(Command::START);  // the synchronizer locks again, positions keep their reference
      state_ = Status::SCANNING;
      message = "resumed";
    } else {
      message = "not scanning";
      return false;
    }
    publish_status();
    return true;
  }

  // min_trigger_period_us = ceil(1e6 / laser.max_profile_rate_hz): the Nano never triggers faster.
  void send(uint8_t command) {
    Command c;
    c.command = command;
    if (command == Command::START) {
      const double rate = get_parameter("laser.max_profile_rate_hz").as_double();
      c.counts_per_trigger = static_cast<uint16_t>(get_parameter("encoder.counts_per_trigger").as_int());
      c.min_trigger_period_us = static_cast<uint16_t>(rate > 0 ? std::ceil(1e6 / rate) : 0);
      c.imu_period_ms = static_cast<uint16_t>(get_parameter("imu.period_ms").as_int());
    }
    command_pub_->publish(c);
  }

  // max speed = profile rate * pitch, pitch = counts_per_trigger * metres_per_count
  // (defaults: 120 Hz * 4 * 23 um = 11 mm/s).
  double max_speed_mps() {
    return get_parameter("laser.max_profile_rate_hz").as_double() * scale_.metres_per_count() *
           static_cast<double>(get_parameter("encoder.counts_per_trigger").as_int());
  }

  void on_calibration(const Calibration& c) {
    scale_.set_calibration(c);
    if (c.state == Calibration::CALIBRATION_REQUIRED && calibration_.state == Calibration::CALIBRATED) {
      std::string ignored;  // calibration reset: a running scan stops
      stop_scan(ignored, Status::IDLE);
    }
    // A calibration just ended: keep its outcome on screen for 20 s.
    if (calibration_.state == Calibration::CALIBRATING && c.state != Calibration::CALIBRATING) {
      calibration_result_ = c.message;
      calibration_result_time_ = now();
    }
    calibration_ = c;
  }

  // ---- monitoring (10 Hz) ----
  /// "1.83 s ago", or "never" before the first message.
  std::string age(const rclcpp::Time& t) {
    if (t.nanoseconds() == 0) return "never";
    char text[32];
    std::snprintf(text, sizeof(text), "%.2f s ago", (now() - t).seconds());
    return text;
  }

  // Snapshot of every input -> monitor_rules.hpp -> active faults; a FATAL fault stops the scan.
  void monitor() {
    MonitorInput in;
    in.scanning = state_ == Status::SCANNING;
    in.speed_mps = std::fabs(motion_.speed_mps);
    in.max_speed_mps = max_speed_mps();
    in.warning_fraction = get_parameter("speed.warning_fraction").as_double();
    if (in.scanning && in.speed_mps < get_parameter("speed.min_speed_mps").as_double()) {
      if (stalled_since_.nanoseconds() == 0) stalled_since_ = now();
      in.stalled_for_s = (now() - stalled_since_).seconds();
    } else {
      stalled_since_ = rclcpp::Time(0, 0, RCL_ROS_TIME);
    }
    in.stall_timeout_s = get_parameter("speed.stall_timeout_s").as_double();
    in.overspeed_triggers = in.scanning ? nano_.overspeed : 0;
    in.nano_ok = nano_fresh();
    in.encoder_ok = nano_.encoder_ok;
    in.imu_ok = nano_.imu_ok;
    in.tilt_rad = motion_.tilt_rad;
    in.tilt_limit_rad = get_parameter("system.tilt_limit_rad").as_double();
    in.rotation_radps = motion_.rotation_radps;
    in.vibration_limit_radps = get_parameter("system.vibration_limit_radps").as_double();
    in.laser_ok = laser_ok();
    in.missing_profiles = stats_.missing_profiles;
    in.missing_triggers = stats_.missing_triggers;
    in.timing_mismatches = stats_.timing_mismatches;
    in.sync_locked = stats_.locked || !stats_.hardware_trigger;
    in.triggers_seen = nano_.triggers;
    in.free_mb = have_storage_ ? storage_.free_mb : 1e9;
    in.min_free_mb = get_parameter("system.storage_min_free_mb").as_double();
    in.storage_write_failed = storage_.write_failed;
    in.calibrated = calibration_.state == Calibration::CALIBRATED;
    in.slipping = slipping_;
    faults_ = evaluate(in);
    for (const Fault& f : faults_) {
      if (f.severity == Severity::FATAL && active()) {
        std::string ignored;
        stop_scan(ignored, Status::ERROR);
        // The input ages show which side was late (limits: Nano 1.5 s, laser 3 s).
        RCLCPP_ERROR(get_logger(), "scan stopped by a critical fault: %s (last nano/status %s, "
                     "last laser/status %s)", f.message.c_str(), age(nano_time_).c_str(),
                     age(laser_time_).c_str());
        break;
      }
    }
  }

  // ---- scan/status for the operator panel ----
  void publish_status() {
    Status s;
    s.header.stamp = now();
    s.state = state_;
    s.frames = stats_.frames;
    s.missing_profiles = stats_.missing_profiles;
    s.missing_triggers = stats_.missing_triggers;
    s.speed_mps = motion_.speed_mps;
    s.max_speed_mps = static_cast<float>(max_speed_mps());
    s.tilt_rad = motion_.tilt_rad;
    s.position_m = stats_.last_position_m;
    s.active_faults = static_cast<uint32_t>(faults_.size());
    s.calibration = calibration_.state;
    s.extrinsics_calibrated = calibration_.extrinsics_calibrated;
    // Operator message: the calibration's while it runs, else the most severe fault.
    int worst = 0;
    for (const Fault& f : faults_) {
      s.faults.push_back(std::string(severity_name(f.severity)) + " " + f.source + ": " + f.message);
      if (static_cast<int>(f.severity) > worst) {
        worst = static_cast<int>(f.severity);
        s.message = f.message;
      }
    }
    if (calibration_.state == Calibration::CALIBRATING) {
      s.message = calibration_.message;
    } else if (!calibration_result_.empty() && (now() - calibration_result_time_).seconds() < 20.0) {
      s.message = calibration_result_;
    }
    status_pub_->publish(s);
  }

  EncoderScale scale_;
  uint8_t state_ = Status::IDLE;
  std::vector<Fault> faults_;
  msg::NanoStatus nano_;
  msg::LaserStatus laser_;
  msg::Motion motion_;
  msg::SyncStats stats_;
  msg::StorageStatus storage_;
  Calibration calibration_;
  bool have_storage_ = false, slipping_ = false;
  std::string calibration_result_;
  rclcpp::Time nano_time_{0, 0, RCL_ROS_TIME}, laser_time_{0, 0, RCL_ROS_TIME};
  rclcpp::Time stalled_since_{0, 0, RCL_ROS_TIME}, calibration_result_time_{0, 0, RCL_ROS_TIME};
  rclcpp::Publisher<Command>::SharedPtr command_pub_;
  rclcpp::Publisher<Status>::SharedPtr status_pub_;
  rclcpp::Subscription<msg::NanoStatus>::SharedPtr nano_sub_;
  rclcpp::Subscription<msg::LaserStatus>::SharedPtr laser_sub_;
  rclcpp::Subscription<msg::Motion>::SharedPtr motion_sub_;
  rclcpp::Subscription<msg::SyncStats>::SharedPtr stats_sub_;
  rclcpp::Subscription<msg::StorageStatus>::SharedPtr storage_sub_;
  rclcpp::Subscription<std_msgs::msg::Bool>::SharedPtr slip_sub_;
  rclcpp::Subscription<Calibration>::SharedPtr calibration_sub_;
  rclcpp::Service<Trigger>::SharedPtr start_srv_, stop_srv_, pause_srv_, zero_srv_;
  rclcpp::TimerBase::SharedPtr monitor_timer_, status_timer_;
};

}  // namespace laser_hhrp

int main(int argc, char** argv) {
  rclcpp::init(argc, argv);
  rclcpp::spin(std::make_shared<laser_hhrp::ScanControllerNode>());
  rclcpp::shutdown();
  return 0;
}
