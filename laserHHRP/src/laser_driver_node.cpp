// laser_driver_node (layer 1: hardware interface): the scanCONTROL laser profiler.
//
// Publishes
//   laser/raw      laser_hhrp/RawProfile        every profile, stamped with its exposure time
//   laser/profile  sensor_msgs/PointCloud2      every Nth profile, for the live view in RViz
//   laser/status   laser_hhrp/LaserStatus       connected or not (1 Hz)
// Trigger: sync.hardware_trigger true = one profile per Nano pulse on In1; false = free running
// at sync.software_rate_hz. A missing scanner is retried every 2 s.
#include <algorithm>
#include <cstdint>
#include <cstdlib>
#include <ament_index_cpp/get_package_share_directory.hpp>
#include <filesystem>
#include <fstream>
#include <rclcpp/rclcpp.hpp>
#include <stdexcept>

#include "laser_hhrp/msg/laser_status.hpp"
#include "laser_hhrp/msg/raw_profile.hpp"
#include "laser_hhrp/profile_cloud.hpp"
#include "laser_hhrp/scancontrol.hpp"
#include "laser_hhrp/scanner_clock.hpp"

namespace laser_hhrp {

class LaserDriverNode : public rclcpp::Node {
 public:
  // ---- setup: scanner settings from the parameters ----
  LaserDriverNode() : Node("laser_driver") {
    settings_.interface = declare_parameter("laser.interface", "");
    settings_.config_file = config_path(declare_parameter("laser.config_file", ""));
    settings_.config_ignore_calibration = declare_parameter("laser.config_ignore_calibration", true);
    settings_.resolution = unsigned_param("laser.resolution", 0);
    settings_.exposure_time_us = unsigned_param("laser.exposure_time_us", 0);
    settings_.exposure_automatic = declare_parameter("laser.exposure_automatic", false);
    settings_.idle_time_us = unsigned_param("laser.idle_time_us", 0);
    settings_.inputs_24v = declare_parameter("laser.inputs_24v", false);
    settings_.input_pulldown = declare_parameter("laser.input_pulldown", true);
    settings_.digital_in_mode = unsigned_param("laser.digital_in_mode", 2);
    settings_.trigger_mode = unsigned_param("laser.trigger_mode", 0);
    settings_.trigger_input_digital = declare_parameter("laser.trigger_input_digital", true);
    settings_.trigger_polarity_high = declare_parameter("laser.trigger_polarity_high", true);
    settings_.queue_size = unsigned_param("laser.queue_size", 256);
    settings_.external_trigger = declare_parameter("sync.hardware_trigger", false);
    // Software sync: the scanner runs free; ScanControl sets idle = 1/rate - exposure.
    if (!settings_.external_trigger) {
      expected_hz_ = std::max(1.0, declare_parameter("sync.software_rate_hz", 120.0));
      settings_.software_rate_hz = expected_hz_;
    }
    live_view_every_ = std::max<int64_t>(1, declare_parameter("laser.live_view_every", 10));

    raw_pub_ = create_publisher<msg::RawProfile>("laser/raw", rclcpp::QoS(200));
    cloud_pub_ = create_publisher<sensor_msgs::msg::PointCloud2>("laser/profile", rclcpp::SensorDataQoS());
    status_pub_ = create_publisher<msg::LaserStatus>("laser/status", 10);
    connect();
    timer_ = create_wall_timer(std::chrono::milliseconds(1), [this] { poll(); });
    status_timer_ = create_wall_timer(std::chrono::seconds(1), [this] { publish_status(); });
  }

 private:
  // A negative value would wrap into a wrong scanner register value: refuse it.
  uint32_t unsigned_param(const std::string& name, int64_t default_value) {
    const int64_t v = declare_parameter(name, default_value);
    if (v < 0 || v > UINT32_MAX) throw std::invalid_argument(name + " must be 0 or more: " + std::to_string(v));
    return static_cast<uint32_t>(v);
  }

  // laser.config_file: "" = none; "~/..." = home; relative = this package's config/ folder.
  // A wrong name stops the node: scanning with the sensor's old setup would go unnoticed.
  std::string config_path(const std::string& name) {
    if (name.empty()) return name;
    const char* home = std::getenv("HOME");
    std::filesystem::path path(name.rfind("~/", 0) == 0 && home ? std::string(home) + name.substr(1) : name);
    if (path.is_relative())
      path = std::filesystem::path(ament_index_cpp::get_package_share_directory("laser_hhrp")) / "config" / path;
    if (!std::filesystem::is_regular_file(path) || !std::ifstream(path)) {
      RCLCPP_FATAL(get_logger(), "laser.config_file: cannot read %s", path.c_str());
      throw std::runtime_error("laser.config_file not found: " + path.string());
    }
    return path.string();
  }

  // ---- connect (at start, then every 2 s while disconnected) ----
  void connect() {
    clock_.reset();  // a reconnected scanner may have restarted its clock
    if (scanner_.connect(settings_, message_)) {
      RCLCPP_INFO(get_logger(), "scanner connected (%s trigger); %s",
                  settings_.external_trigger ? "external In1" : "internal", message_.c_str());
      message_ = "connected; " + message_;
    } else {
      RCLCPP_WARN_THROTTLE(get_logger(), *get_clock(), 10000, "scanner: %s", message_.c_str());
    }
    last_attempt_ = now();
  }

  // ---- main loop (every 1 ms): take all queued profiles and publish them ----
  void poll() {
    if (!scanner_.connected()) {
      if ((now() - last_attempt_).seconds() >= 2.0) connect();
      return;
    }
    LaserProfile p;
    std::string error;
    while (scanner_.read(p, error)) {
      auto m = std::make_unique<msg::RawProfile>();
      // Exposure midpoint in Pi time from the scanner's own clock (removes network jitter).
      m->header.stamp = rclcpp::Time(clock_.map(p.shutter_open_s, p.shutter_close_s, p.arrival_ns));
      m->header.frame_id = "laser";
      m->profile_counter = p.profile_counter;
      m->shutter_open_s = p.shutter_open_s;
      m->x_m = p.x_m;
      m->z_m = p.z_m;
      m->valid = p.valid;
      count_rate(p);
      if (++count_ % live_view_every_ == 0) cloud_pub_->publish(to_cloud(*m));
      raw_pub_->publish(std::move(m));
    }
    // The SDK reported the scanner lost: drop the connection, poll() reconnects in 2 s.
    if (!error.empty()) {
      message_ = error;
      RCLCPP_ERROR(get_logger(), "scanner: %s", error.c_str());
      scanner_.disconnect();
    }
    report_rate();
  }

  // ---- rate report ----
  // made = (counter span + 1) / seconds: the scanner numbers every profile it makes, so gaps in
  // the counter are profiles lost on the way; received < made means network losses.
  void count_rate(const LaserProfile& p) {
    if (window_received_++ == 0) {
      window_first_counter_ = p.profile_counter;
      window_first_lost_ = p.lost_profiles;
    }
    window_last_counter_ = p.profile_counter;
    window_last_lost_ = p.lost_profiles;
  }
  void report_rate() {
    if (window_start_.nanoseconds() == 0) window_start_ = now();
    const double seconds = (now() - window_start_).seconds();
    if (seconds < 5.0) return;
    if (window_received_ == 0) {
      RCLCPP_WARN(get_logger(), "no profiles in %.0f s%s", seconds,
                  settings_.external_trigger ? " (no trigger pulses? roll the roller during a scan)" : "");
    } else {
      const uint32_t span = (window_last_counter_ - window_first_counter_) & 0xFFFFFFu;
      const double made = window_received_ > 1 ? (span + 1.0) / seconds : window_received_ / seconds;
      RCLCPP_INFO(get_logger(), "profiles: %.1f /s received, %.1f /s made by the scanner, %u lost",
                  window_received_ / seconds, made, window_last_lost_ - window_first_lost_);
      // Asked for more than it can do (2900, full field: > ~140 Hz) it falls to ~18 profiles/s.
      if (expected_hz_ > 0 && made < 0.8 * expected_hz_)
        RCLCPP_ERROR(get_logger(), "scanner makes %.1f profiles/s instead of %.1f: lower "
                     "sync.software_rate_hz (2900, full field: max ~140)", made, expected_hz_);
    }
    window_start_ = now();
    window_received_ = 0;
  }

  void publish_status() {
    msg::LaserStatus s;
    s.header.stamp = now();
    s.connected = scanner_.connected();
    s.message = message_;
    status_pub_->publish(s);
  }

  ScanControl scanner_;
  ScanControlSettings settings_;
  ScannerClock clock_;
  double expected_hz_ = 0;
  int64_t live_view_every_ = 10;
  uint64_t count_ = 0;
  std::string message_;
  rclcpp::Time last_attempt_;
  rclcpp::Time window_start_{0, 0, RCL_ROS_TIME};
  uint32_t window_received_ = 0, window_first_counter_ = 0, window_last_counter_ = 0;
  uint32_t window_first_lost_ = 0, window_last_lost_ = 0;
  rclcpp::Publisher<msg::RawProfile>::SharedPtr raw_pub_;
  rclcpp::Publisher<sensor_msgs::msg::PointCloud2>::SharedPtr cloud_pub_;
  rclcpp::Publisher<msg::LaserStatus>::SharedPtr status_pub_;
  rclcpp::TimerBase::SharedPtr timer_, status_timer_;
};

}  // namespace laser_hhrp

int main(int argc, char** argv) {
  rclcpp::init(argc, argv);
  rclcpp::spin(std::make_shared<laser_hhrp::LaserDriverNode>());
  rclcpp::shutdown();
  return 0;
}
