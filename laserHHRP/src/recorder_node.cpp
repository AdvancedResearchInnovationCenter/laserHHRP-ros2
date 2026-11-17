// recorder_node (layer 6: storage and export): one folder per scan in system.storage_path,
// named YYYYmmdd-HHMMSS (YYYYmmdd-HHMMSS-2, -3 ... when scans start within the same second: a
// folder is never reused, frames.bin never overwritten):
//   frames.bin    every ScanFrame: [uint32 length][ROS 2 serialized ScanFrame] ...
//                 (read it with scripts/read_session.py)
//   results.json  inspection results
//   session.json  start/stop time, frame count, synchronization summary, all parameters
// A session opens when a scan starts (scan/status). At the stop, the synchronizer still flushes its
// last frames (published in reaction to the same scan/status message, so they can arrive after it):
// the session stays open for the next consecutive frame indices until scan/stream_closed gives the
// scan's frame count, or at most 2 s, then closes. Late detector results are not waited for.
//
// Subscribes  scan/frame, inspection/result, scan/status, scan/sync
// Publishes   scan/storage            laser_hhrp/StorageStatus (2 Hz)
//             scan/recording_session  laser_hhrp/ScanRecordingSession on every open / close, with the
//                                     stamp of the scan/status message that caused it (operator UI)
#include <sys/statvfs.h>

#include <ctime>
#include <filesystem>
#include <fstream>
#include <sstream>

#include <rclcpp/rclcpp.hpp>
#include <rclcpp/serialization.hpp>

#include "laser_hhrp/msg/inspection_result.hpp"
#include "laser_hhrp/msg/scan_frame.hpp"
#include "laser_hhrp/msg/scan_recording_session.hpp"
#include "laser_hhrp/msg/scan_status.hpp"
#include "laser_hhrp/msg/scan_stream_closed.hpp"
#include "laser_hhrp/msg/storage_status.hpp"
#include "laser_hhrp/msg/sync_stats.hpp"

namespace laser_hhrp {

using ScanStatus = msg::ScanStatus;

std::string json_escape(const std::string& s) {
  std::string o;
  for (char c : s) {
    if (c == '"' || c == '\\') o += '\\';
    o += c;
  }
  return o;
}

std::string iso_time(std::time_t t) {
  char buf[32];
  std::strftime(buf, sizeof(buf), "%Y-%m-%dT%H:%M:%SZ", std::gmtime(&t));
  return buf;
}

class RecorderNode : public rclcpp::Node {
 public:
  // All parameters of the launch file are accepted, so session.json can list them.
  RecorderNode()
      : Node("recorder", rclcpp::NodeOptions().automatically_declare_parameters_from_overrides(true)) {
    if (!has_parameter("system.storage_path")) declare_parameter("system.storage_path", "~/laser_hhrp/scans");
    root_ = get_parameter("system.storage_path").as_string();
    if (!root_.empty() && root_[0] == '~') root_ = std::string(getenv("HOME")) + root_.substr(1);

    status_pub_ = create_publisher<msg::StorageStatus>("scan/storage", 10);
    session_pub_ = create_publisher<msg::ScanRecordingSession>(
        "scan/recording_session", rclcpp::QoS(50).reliable().transient_local());
    frame_sub_ = create_subscription<msg::ScanFrame>(
        "scan/frame", rclcpp::QoS(200), [this](const msg::ScanFrame& f) { write(f); });
    result_sub_ = create_subscription<msg::InspectionResult>(
        "inspection/result", 50, [this](const msg::InspectionResult& r) { add_result(r); });
    stats_sub_ = create_subscription<msg::SyncStats>("scan/sync", 10, [this](const msg::SyncStats& s) { stats_ = s; });
    scan_sub_ = create_subscription<ScanStatus>("scan/status", 10, [this](const ScanStatus& s) {
      const bool on = s.state == ScanStatus::SCANNING || s.state == ScanStatus::PAUSED;
      if (on && !scanning_) begin(s.header.stamp);
      if (!on && scanning_) start_closing(s.header.stamp);
      scanning_ = on;
    });
    closed_sub_ = create_subscription<msg::ScanStreamClosed>(
        "scan/stream_closed", rclcpp::QoS(50).reliable().transient_local(), [this](const msg::ScanStreamClosed& c) {
          if (!closing_) return;
          expected_frames_ = c.frame_count;
          have_expected_ = true;
          finish_closing(false);
        });
    timer_ = create_wall_timer(std::chrono::milliseconds(500), [this] { publish_status(); });
    close_timer_ = create_wall_timer(std::chrono::milliseconds(100), [this] { finish_closing(false); });
  }

  ~RecorderNode() override { end(); }

 private:
  // ---- session start: a new folder named after the local time, frames.bin opened ----
  // create_directory fails if the folder exists: the first free name wins, so a scan started within
  // the same second as the previous one never truncates its frames.bin.
  void begin(const builtin_interfaces::msg::Time& status_stamp) {
    if (closing_) finish_closing(true);           // a new scan: the previous session ends now
    end(status_stamp);
    start_ = std::time(nullptr);
    char name[32];
    std::strftime(name, sizeof(name), "%Y%m%d-%H%M%S", std::localtime(&start_));
    std::error_code ec;
    std::filesystem::create_directories(root_, ec);
    session_id_.clear();
    for (int n = 1; n < 1000; ++n) {
      const std::string id = n == 1 ? std::string(name) : std::string(name) + "-" + std::to_string(n);
      if (std::filesystem::create_directory(root_ + "/" + id, ec)) {
        session_id_ = id;
        break;
      }
      if (ec) break;                                 // not "exists": a real error
    }
    dir_ = session_id_.empty() ? "" : root_ + "/" + session_id_;
    if (!dir_.empty()) file_.open(dir_ + "/frames.bin", std::ios::binary | std::ios::trunc);
    results_.clear();
    frames_ = 0;
    write_failed_ = !file_.is_open();
    if (write_failed_) RCLCPP_ERROR(get_logger(), "cannot open a session folder in %s", root_.c_str());
    else RCLCPP_INFO(get_logger(), "recording to %s", dir_.c_str());
    publish_session(write_failed_ ? msg::ScanRecordingSession::OPEN_FAILED : msg::ScanRecordingSession::OPENED,
                    status_stamp);
  }

  void publish_session(uint8_t event, const builtin_interfaces::msg::Time& status_stamp) {
    msg::ScanRecordingSession m;
    m.header.stamp = now();
    m.event = event;
    m.status_stamp = status_stamp;
    m.session_id = session_id_;
    m.session_dir = dir_;
    m.frame_count = static_cast<uint32_t>(frames_);
    m.write_failed = write_failed_;
    try {
      session_pub_->publish(m);
    } catch (const std::exception& e) {   // e.g. at shutdown, after the context ended
      RCLCPP_WARN(get_logger(), "recording session event not published: %s", e.what());
    }
  }

  // ---- session end: close frames.bin, write results.json and session.json ----
  void end(const builtin_interfaces::msg::Time& status_stamp = builtin_interfaces::msg::Time()) {
    if (!file_.is_open()) return;
    file_.flush();
    if (!file_) write_failed_ = true;
    file_.close();
    std::ofstream results(dir_ + "/results.json");
    results << "[\n";
    for (size_t i = 0; i < results_.size(); ++i)
      results << "  " << results_[i] << (i + 1 < results_.size() ? ",\n" : "\n");
    results << "]\n";
    std::ofstream session(dir_ + "/session.json");
    session << "{\n  \"start\": \"" << iso_time(start_) << "\",\n  \"stop\": \"" << iso_time(std::time(nullptr))
            << "\",\n  \"frames\": " << frames_ << ",\n  \"summary\": {\"missing_profiles\": "
            << stats_.missing_profiles << ", \"missing_triggers\": " << stats_.missing_triggers
            << ", \"timing_mismatches\": " << stats_.timing_mismatches
            << ", \"length_m\": " << stats_.last_position_m << "},\n  \"parameters\": " << parameters_json()
            << "\n}\n";
    RCLCPP_INFO(get_logger(), "session closed: %lu frames in %s", static_cast<unsigned long>(frames_), dir_.c_str());
    if (session_pub_) publish_session(msg::ScanRecordingSession::CLOSED, status_stamp);
  }

  // ---- stop: keep the session open for the flushed last frames ----
  void start_closing(const builtin_interfaces::msg::Time& status_stamp) {
    closing_ = true;
    have_expected_ = false;
    close_stamp_ = status_stamp;
    close_deadline_ = now() + rclcpp::Duration::from_seconds(2.0);
    finish_closing(false);
  }

  void finish_closing(bool force) {
    if (!closing_) return;
    const bool complete = have_expected_ && frames_ >= expected_frames_;
    if (!force && !complete && now() < close_deadline_) return;
    if (have_expected_ && frames_ != expected_frames_)
      RCLCPP_WARN(get_logger(), "session closes with %lu frames, the scan published %u",
                  static_cast<unsigned long>(frames_), expected_frames_);
    closing_ = false;
    end(close_stamp_);
  }

  // ---- one frame: [uint32 length][ROS 2 serialized ScanFrame] (read_session.py reads it) ----
  void write(const msg::ScanFrame& f) {
    if (!file_.is_open()) return;
    if (closing_ && f.index != frames_) return;   // after the stop: only the scan's own next frames
    rclcpp::SerializedMessage data;
    serializer_.serialize_message(&f, &data);
    const uint32_t length = static_cast<uint32_t>(data.size());
    file_.write(reinterpret_cast<const char*>(&length), sizeof(length));
    file_.write(reinterpret_cast<const char*>(data.get_rcl_serialized_message().buffer), length);
    if (!file_) write_failed_ = true;
    ++frames_;
    if (closing_) finish_closing(false);
  }

  void add_result(const msg::InspectionResult& r) {
    if (!file_.is_open()) return;
    std::ostringstream o;
    o << "{\"feature_type\": \"" << json_escape(r.feature_type) << "\", \"position_m\": [" << r.position_m[0]
      << ", " << r.position_m[1] << ", " << r.position_m[2] << "], \"flushness_m\": " << r.flushness_m
      << ", \"countersink_depth_m\": " << r.countersink_depth_m
      << ", \"surface_deviation_m\": " << r.surface_deviation_m << ", \"passed\": " << (r.passed ? "true" : "false")
      << ", \"detail\": \"" << json_escape(r.detail) << "\"}";
    results_.push_back(o.str());
  }

  std::string parameters_json() {
    std::ostringstream o;
    o << "{";
    bool first = true;
    for (const auto& name : list_parameters({}, 0).names) {
      if (name == "use_sim_time") continue;
      const rclcpp::Parameter p = get_parameter(name);
      const std::string value = p.get_type() == rclcpp::ParameterType::PARAMETER_STRING
                                    ? "\"" + json_escape(p.as_string()) + "\""
                                    : p.value_to_string();
      o << (first ? "" : ", ") << "\"" << name << "\": " << value;
      first = false;
    }
    return o.str() + "}";
  }

  // ---- scan/storage (2 Hz): free space = available blocks * block size ----
  void publish_status() {
    std::error_code ec;
    std::filesystem::create_directories(root_, ec);
    struct statvfs s {};
    msg::StorageStatus m;
    m.header.stamp = now();
    m.recording = file_.is_open();
    m.session_dir = dir_;
    m.free_mb = statvfs(root_.c_str(), &s) == 0
                    ? static_cast<float>(static_cast<double>(s.f_bavail) * s.f_frsize / (1024.0 * 1024.0))
                    : -1.0f;
    m.write_failed = write_failed_;
    status_pub_->publish(m);
  }

  std::string root_, dir_, session_id_;
  std::ofstream file_;
  std::vector<std::string> results_;
  uint64_t frames_ = 0;
  bool write_failed_ = false, scanning_ = false;
  bool closing_ = false, have_expected_ = false;   // stop seen, waiting for the flushed last frames
  uint32_t expected_frames_ = 0;
  builtin_interfaces::msg::Time close_stamp_;
  rclcpp::Time close_deadline_{0, 0, RCL_ROS_TIME};
  rclcpp::Subscription<msg::ScanStreamClosed>::SharedPtr closed_sub_;
  rclcpp::TimerBase::SharedPtr close_timer_;
  std::time_t start_ = 0;
  msg::SyncStats stats_;
  rclcpp::Serialization<msg::ScanFrame> serializer_;
  rclcpp::Publisher<msg::StorageStatus>::SharedPtr status_pub_;
  rclcpp::Publisher<msg::ScanRecordingSession>::SharedPtr session_pub_;
  rclcpp::Subscription<msg::ScanFrame>::SharedPtr frame_sub_;
  rclcpp::Subscription<msg::InspectionResult>::SharedPtr result_sub_;
  rclcpp::Subscription<msg::SyncStats>::SharedPtr stats_sub_;
  rclcpp::Subscription<ScanStatus>::SharedPtr scan_sub_;
  rclcpp::TimerBase::SharedPtr timer_;
};

}  // namespace laser_hhrp

int main(int argc, char** argv) {
  rclcpp::init(argc, argv);
  rclcpp::spin(std::make_shared<laser_hhrp::RecorderNode>());
  rclcpp::shutdown();
  return 0;
}
