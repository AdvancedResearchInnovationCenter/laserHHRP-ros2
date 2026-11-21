// synchronizer_node (layer 3: synchronization): one ScanFrame per laser profile, with its position
// along the scan axis (encoder) and the IMU at the same instant.
//
// Two modes (parameter sync.hardware_trigger):
//   true  (hardware sync) the Nano triggers the laser every N counts; each profile is paired with
//         its trigger record by number (frame_pairer.hpp).
//   false (software sync) the laser runs free; each profile gets the encoder count interpolated at
//         its exposure time between the Nano's encoder samples (software_pairer.hpp).
// Pairing and IMU interpolation use the Nano clock; frames are stamped in Pi time (nano/clock).
// Frames are made only while a scan runs (scan/status).
//
// Scan identity (operator UI): every frame is also published as scan/frame_tagged with its scan's
// UUID from scan/session_state. scan/status alone still starts, stops and flushes the scan; the
// identity is matched by the exact stamp of the scan/status message that started it (either may
// arrive first: tagged copies wait in a bounded buffer). After the stop flush, scan/stream_closed
// gives the number of tagged frames published. Pairs still inside the pairer at stop are not
// published (unchanged capture policy) and are not counted.
//
// Subscribes  nano/trigger, nano/encoder, laser/raw, imu/corrected, nano/clock, scan/status,
//             scan/motion, odometry/laser_pose, calibration/status
// Publishes
//   scan/frame  laser_hhrp/ScanFrame          one per profile
//   scan/pose   geometry_msgs/PoseStamped     laser pose at each frame (same stamp), once the
//                                             extrinsics are calibrated
//   scan/sync   laser_hhrp/SyncStats          10 Hz
//   scan/frame_tagged    laser_hhrp/TaggedScanFrame   the same frames with their scan_id
//   scan/stream_closed   laser_hhrp/ScanStreamClosed  once per scan, after its last tagged frame
#include <deque>

#include <geometry_msgs/msg/pose_stamped.hpp>
#include <rclcpp/rclcpp.hpp>

#include "laser_hhrp/encoder_scale.hpp"
#include "laser_hhrp/frame_pairer.hpp"
#include "laser_hhrp/msg/clock_map.hpp"
#include "laser_hhrp/msg/device_pose.hpp"
#include "laser_hhrp/msg/encoder_sample.hpp"
#include "laser_hhrp/msg/imu_sample.hpp"
#include "laser_hhrp/msg/motion.hpp"
#include "laser_hhrp/msg/scan_frame.hpp"
#include "laser_hhrp/msg/scan_session_state.hpp"
#include "laser_hhrp/msg/scan_stream_closed.hpp"
#include "laser_hhrp/msg/scan_status.hpp"
#include "laser_hhrp/msg/sync_stats.hpp"
#include "laser_hhrp/msg/tagged_scan_frame.hpp"
#include "laser_hhrp/msg/trigger_record.hpp"
#include "laser_hhrp/pose.hpp"
#include "laser_hhrp/software_pairer.hpp"

namespace laser_hhrp {

namespace {
constexpr int64_t kImuWaitNs = 100000000;   // a frame waits up to 100 ms for the IMU after it
constexpr int64_t kHistoryNs = 2000000000;  // IMU samples / poses kept for interpolation
constexpr size_t kTagBuffer = 600;          // tagged frames waiting for their scan_id (~5 s at 120 Hz)
constexpr int64_t kIdentityWaitNs = 5000000000;  // after the stop, how long the identity may still come
}  // namespace

using Pair = FramePairer::Pair;
using ScanStatus = msg::ScanStatus;

constexpr size_t kTaggedHistory = 1200;  // scan/frame_tagged frames kept for late-joining detectors

class SynchronizerNode : public rclcpp::Node {
 public:
  // ---- setup ----
  // Callbacks only collect data: triggers / encoder references / profiles go into the pairer of
  // the active mode (only while a scan runs), IMU samples and poses into 2 s histories. The
  // pairing itself runs in step() every 2 ms.
  SynchronizerNode() : Node("synchronizer"), scale_(*this) {
    hardware_ = declare_parameter("sync.hardware_trigger", false);
    software_.set_latency(declare_parameter("sync.profile_latency_s", 0.002));

    const auto data = rclcpp::QoS(500);
    frame_pub_ = create_publisher<msg::ScanFrame>("scan/frame", rclcpp::QoS(200));
    pose_pub_ = create_publisher<geometry_msgs::msg::PoseStamped>("scan/pose", 200);
    stats_pub_ = create_publisher<msg::SyncStats>("scan/sync", 10);
    // Transient local, 1200 deep (10 s at the 120 Hz maximum): a detector whose subscription the synchronizer
    // matches only after the scan started (Fast DDS on the Pi: up to ~2 s after the backend sees it) still gets
    // every frame. The detectors ignore frames exposed before they started (earlier scans).
    tagged_pub_ = create_publisher<msg::TaggedScanFrame>("scan/frame_tagged",
                                                         rclcpp::QoS(kTaggedHistory).reliable().transient_local());
    closed_pub_ = create_publisher<msg::ScanStreamClosed>(
        "scan/stream_closed", rclcpp::QoS(50).reliable().transient_local());
    session_sub_ = create_subscription<msg::ScanSessionState>(
        "scan/session_state", rclcpp::QoS(50).reliable().transient_local(),
        [this](const msg::ScanSessionState& m) { on_session(m); });

    clock_sub_ = create_subscription<msg::ClockMap>(
        "nano/clock", rclcpp::QoS(10).transient_local(), [this](const msg::ClockMap& c) { clock_ = c; have_clock_ = true; });
    calibration_sub_ = create_subscription<msg::CalibrationStatus>(
        "calibration/status", rclcpp::QoS(1).transient_local(),
        [this](const msg::CalibrationStatus& c) { scale_.set_calibration(c); });
    motion_sub_ = create_subscription<msg::Motion>(
        "scan/motion", 10, [this](const msg::Motion& m) { last_count_ = m.encoder_count; });
    scan_sub_ = create_subscription<ScanStatus>(
        "scan/status", 10, [this](const ScanStatus& s) { follow_scan(s); });
    imu_sub_ = create_subscription<msg::ImuSample>("imu/corrected", data, [this](const msg::ImuSample& m) {
      imu_history_.push_back(m);
      while (m.device_time_ns - imu_history_.front().device_time_ns > kHistoryNs) imu_history_.pop_front();
    });
    pose_sub_ = create_subscription<msg::DevicePose>("odometry/laser_pose", data, [this](const msg::DevicePose& p) {
      if (!pose_history_.empty() && p.device_time_ns <= pose_history_.back().device_time_ns) pose_history_.clear();
      pose_history_.push_back(p);
      while (p.device_time_ns - pose_history_.front().device_time_ns > kHistoryNs) pose_history_.pop_front();
    });
    trigger_sub_ = create_subscription<msg::TriggerRecord>("nano/trigger", data, [this](const msg::TriggerRecord& t) {
      if (active_ && hardware_) pairer_.add_trigger(to_trigger(t), now_ns());
    });
    encoder_sub_ = create_subscription<msg::EncoderSample>("nano/encoder", data, [this](const msg::EncoderSample& e) {
      if (active_ && !hardware_) add_software_reference(e);
    });
    raw_sub_ = create_subscription<msg::RawProfile>("laser/raw", rclcpp::QoS(200), [this](msg::RawProfile::ConstSharedPtr p) {
      if (!active_) return;
      Raw r;
      r.stamp_ns = rclcpp::Time(p->header.stamp).nanoseconds();
      r.profile_counter = p->profile_counter;
      r.shutter_open_s = p->shutter_open_s;
      r.msg = p;
      if (hardware_) pairer_.add_profile(r);
      else software_.add_profile(r);
    });
    timer_ = create_wall_timer(std::chrono::milliseconds(2), [this] { step(); });
  }

 private:
  static int64_t now_ns() {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
               std::chrono::system_clock::now().time_since_epoch()).count();
  }
  static Trigger to_trigger(const msg::TriggerRecord& t) {
    Trigger out;
    out.device_ns = t.device_time_ns;
    out.index = t.index;
    out.count = t.count;
    out.direction = t.direction;
    out.flags = t.flags;
    return out;
  }

  // ---- software sync input ----
  // Each encoder sample is a reference with two times: Nano time (device_ns) and Pi time
  // (to_host). The pairer interpolates the count at each profile's Pi time.
  void add_software_reference(const msg::EncoderSample& e) {
    Trigger ref;
    ref.device_ns = e.device_time_ns;
    ref.count = e.count;
    ref.direction = static_cast<int8_t>(e.count > last_ref_count_ ? 1 : e.count < last_ref_count_ ? -1 : 0);
    last_ref_count_ = e.count;
    software_.add_record(ref, to_host(e.device_time_ns));
  }

  // ---- every 2 ms: completed pairs -> frames ----
  void step() {
    const int64_t now = now_ns();
    if (active_) {
      for (const Pair& p : hardware_ ? pairer_.take(now) : software_.take(now)) ready_.push_back(p);
    }
    // A frame goes out once the IMU sample after it has arrived (so the IMU is interpolated at
    // that instant), or after kImuWaitNs without it (imu_valid false).
    while (!ready_.empty()) {
      const Pair& pair = ready_.front();
      const bool imu_ready = !imu_history_.empty() &&
                             imu_history_.back().device_time_ns >= pair.trigger.device_ns;
      if ((!imu_ready || !pose_ready(pair.trigger.device_ns)) && now - pair.arrival_ns < kImuWaitNs) break;
      publish(pair);
      ready_.pop_front();
    }
    if (now - last_stats_ns_ >= 100000000) publish_stats(now);
    if (tag_.stop_pending && tag_.scan_id.empty() && now - tag_.stop_ns > kIdentityWaitNs) {
      RCLCPP_ERROR(get_logger(), "scan identity never arrived: %zu tagged frames dropped, no stream close",
                   tag_.buffer.size());
      tag_ = Tag();                                  // the UI fails this scan (missing stream close)
    }
  }

  // ---- scan state ----
  // start: reset everything, remember the count at the start (position 0) and the encoder
  // scale; resume: new trigger numbering, so the hardware pairer locks again; stop: flush.
  void follow_scan(const ScanStatus& status) {
    const uint8_t state = status.state;
    if (state == scan_state_) return;
    const bool was_on = scan_state_ == ScanStatus::SCANNING || scan_state_ == ScanStatus::PAUSED;
    if (state == ScanStatus::SCANNING && !was_on) {
      begin_tag(status.header.stamp);
      ready_.clear();
      pairer_.reset();
      software_.reset();
      start_count_ = last_count_;
      metres_per_count_ = scale_.metres_per_count();
      sign_ = scale_.direction_sign();
      frames_ = 0;
      last_position_m_ = 0;
      active_ = true;
    } else if (state == ScanStatus::SCANNING) {
      pairer_.reset();
      software_.reset();
    } else if (state != ScanStatus::PAUSED && was_on) {
      for (const Pair& pair : ready_) publish(pair);
      ready_.clear();
      active_ = false;
      end_tag(state);                                 // after the last frame of this scan
    }
    scan_state_ = state;
  }

  // ---- one frame ----
  // position_m = sign * (count - start_count) * metres_per_count
  // IMU and pose are interpolated at the trigger's Nano time (same clock: exact).
  void publish(const Pair& pair) {
    auto f = std::make_unique<msg::ScanFrame>();
    f->header.stamp = rclcpp::Time(to_host(pair.trigger.device_ns));
    f->header.frame_id = "laser";
    f->index = frames_;  // unique within the scan (trigger numbers restart after a pause)
    f->encoder_count = pair.trigger.count;
    f->position_m = sign_ * (pair.trigger.count - start_count_) * metres_per_count_;
    f->direction = static_cast<int8_t>(sign_ * pair.trigger.direction);
    f->imu_valid = imu_at(pair.trigger.device_ns, *f);
    f->flags = static_cast<uint8_t>((pair.trigger.flags & 1) | (pair.timing_mismatch ? 2 : 0));
    f->x_m = pair.profile.msg->x_m;
    f->z_m = pair.profile.msg->z_m;
    f->valid = pair.profile.msg->valid;
    last_position_m_ = static_cast<float>(f->position_m);
    ++frames_;
    Pose p;
    if (pose_at(pair.trigger.device_ns, p)) {  // the laser pose at the same instant, same stamp
      geometry_msgs::msg::PoseStamped out;
      out.header.stamp = f->header.stamp;
      out.header.frame_id = "odom";
      out.pose.position.x = p.position_m[0];
      out.pose.position.y = p.position_m[1];
      out.pose.position.z = p.position_m[2];
      out.pose.orientation.w = p.orientation[0];
      out.pose.orientation.x = p.orientation[1];
      out.pose.orientation.y = p.orientation[2];
      out.pose.orientation.z = p.orientation[3];
      pose_pub_->publish(out);
    }
    publish_tagged(*f);
    frame_pub_->publish(std::move(f));
  }

  // ---- scan identity (operator UI) ----
  // Tag: the identity state of the current scan. `start` is the stamp of the scan/status message
  // that started it; the controller's ScanSessionState for that transition carries the same stamp.
  struct Tag {
    builtin_interfaces::msg::Time start;
    bool started = false, stop_pending = false, overflow = false;
    std::string scan_id;
    std::deque<msg::ScanFrame> buffer;   // frames waiting for the scan_id
    uint32_t published = 0;              // tagged frames published (index 0 .. published-1)
    uint32_t dropped = 0;
    uint8_t stop_state = ScanStatus::IDLE;
    int64_t stop_ns = 0;
  };

  static bool same(const builtin_interfaces::msg::Time& a, const builtin_interfaces::msg::Time& b) {
    return a.sec == b.sec && a.nanosec == b.nanosec;
  }

  void begin_tag(const builtin_interfaces::msg::Time& start) {
    if (tag_.stop_pending)
      RCLCPP_ERROR(get_logger(), "previous scan's identity never arrived: its tagged frames are dropped");
    tag_ = Tag();
    tag_.start = start;
    tag_.started = true;
    for (const auto& s : sessions_)                  // the identity may have arrived first
      if (s.state == msg::ScanSessionState::SCANNING && same(s.status_stamp, start)) tag_.scan_id = s.scan_id;
  }

  void on_session(const msg::ScanSessionState& m) {
    sessions_.push_back(m);
    while (sessions_.size() > 100) sessions_.pop_front();
    if (!tag_.started || !tag_.scan_id.empty()) return;
    if (m.state != msg::ScanSessionState::SCANNING || !same(m.status_stamp, tag_.start)) return;
    tag_.scan_id = m.scan_id;
    for (const auto& f : tag_.buffer) send_tagged(f);
    tag_.buffer.clear();
    if (tag_.stop_pending) close_tag();
  }

  void publish_tagged(const msg::ScanFrame& f) {
    if (!tag_.started) return;
    if (!tag_.scan_id.empty()) {
      send_tagged(f);
    } else if (tag_.buffer.size() < kTagBuffer) {
      tag_.buffer.push_back(f);
    } else {
      if (!tag_.overflow) RCLCPP_ERROR(get_logger(), "scan identity late: tagged frame buffer full");
      tag_.overflow = true;
      ++tag_.dropped;
    }
  }

  void send_tagged(const msg::ScanFrame& f) {
    if (tag_.overflow) { ++tag_.dropped; return; }   // never a gap inside a tagged stream
    msg::TaggedScanFrame t;
    t.scan_id = tag_.scan_id;
    t.frame = f;
    tagged_pub_->publish(t);
    ++tag_.published;
  }

  void end_tag(uint8_t stop_state) {
    if (!tag_.started) return;
    tag_.stop_pending = true;
    tag_.stop_state = stop_state;
    tag_.stop_ns = now_ns();
    if (!tag_.scan_id.empty()) close_tag();          // else: on_session closes it when the identity comes
  }

  void close_tag() {
    msg::ScanStreamClosed c;
    c.header.stamp = this->now();
    c.scan_id = tag_.scan_id;
    c.frame_count = tag_.published;
    c.stop_state = tag_.stop_state;
    if (tag_.overflow)
      c.error = "scan identity arrived late: " + std::to_string(tag_.dropped) + " frames not tagged";
    closed_pub_->publish(c);
    tag_ = Tag();
  }

  // ---- interpolation helpers (Nano time) ----
  // value(t) = a + k * (b - a), k = (t - t_a) / (t_b - t_a), a and b the samples around t;
  // poses: position linear, orientation slerp (pose.hpp).
  /// IMU linearly interpolated at Nano time t; false if no samples bracket t.
  bool imu_at(int64_t t, msg::ScanFrame& f) const {
    for (size_t i = 1; i < imu_history_.size(); ++i) {
      const auto& a = imu_history_[i - 1];
      const auto& b = imu_history_[i];
      if (a.device_time_ns > t || b.device_time_ns < t) continue;
      const double span = static_cast<double>(b.device_time_ns - a.device_time_ns);
      const double k = span > 0 ? (t - a.device_time_ns) / span : 0.0;
      for (int j = 0; j < 3; ++j) {
        f.accel_mps2[j] = static_cast<float>(a.accel_mps2[j] + k * (b.accel_mps2[j] - a.accel_mps2[j]));
        f.gyro_radps[j] = static_cast<float>(a.gyro_radps[j] + k * (b.gyro_radps[j] - a.gyro_radps[j]));
      }
      return true;
    }
    return false;
  }

  /// Wait for the pose after t, unless no poses come (extrinsics not calibrated).
  bool pose_ready(int64_t t) const {
    return pose_history_.empty() || pose_history_.back().device_time_ns >= t ||
           t - pose_history_.back().device_time_ns > 200000000;
  }

  static Pose to_pose(const msg::DevicePose& d) {
    Pose p;
    p.position_m = {d.pose.position.x, d.pose.position.y, d.pose.position.z};
    p.orientation = {d.pose.orientation.w, d.pose.orientation.x, d.pose.orientation.y, d.pose.orientation.z};
    return p;
  }
  bool pose_at(int64_t t, Pose& out) const {
    for (size_t i = 1; i < pose_history_.size(); ++i) {
      const auto& a = pose_history_[i - 1];
      const auto& b = pose_history_[i];
      if (a.device_time_ns > t || b.device_time_ns < t) continue;
      const double span = static_cast<double>(b.device_time_ns - a.device_time_ns);
      out = interpolate(to_pose(a), to_pose(b), span > 0 ? (t - a.device_time_ns) / span : 0.0);
      return true;
    }
    return false;
  }

  // pi = host_ns + (nano - device_ns) * (1 + drift), from the latest nano/clock.
  int64_t to_host(int64_t device_ns) const {
    if (!have_clock_) return now_ns();
    return clock_.host_ns + static_cast<int64_t>((device_ns - clock_.device_ns) * (1.0 + clock_.drift));
  }

  void publish_stats(int64_t now) {
    last_stats_ns_ = now;
    msg::SyncStats s;
    s.header.stamp = this->now();
    s.hardware_trigger = hardware_;
    s.locked = hardware_ ? pairer_.locked() : software_.stats().paired > 0;
    s.frames = frames_;
    s.missing_profiles = pairer_.stats().missing_profiles;
    s.missing_triggers = pairer_.stats().missing_triggers;
    s.timing_mismatches = pairer_.stats().timing_mismatches;
    s.unplaced = software_.stats().unplaced;
    s.last_position_m = last_position_m_;
    stats_pub_->publish(s);
  }

  EncoderScale scale_;
  bool hardware_ = false, active_ = false, have_clock_ = false;
  FramePairer pairer_;
  SoftwarePairer software_;
  std::deque<Pair> ready_;
  std::deque<msg::ImuSample> imu_history_;
  std::deque<msg::DevicePose> pose_history_;
  msg::ClockMap clock_;
  uint8_t scan_state_ = ScanStatus::IDLE;
  double metres_per_count_ = 0;
  int sign_ = 1;
  int32_t start_count_ = 0, last_count_ = 0, last_ref_count_ = 0;
  uint32_t frames_ = 0;
  float last_position_m_ = 0;
  int64_t last_stats_ns_ = 0;
  rclcpp::Publisher<msg::ScanFrame>::SharedPtr frame_pub_;
  rclcpp::Publisher<geometry_msgs::msg::PoseStamped>::SharedPtr pose_pub_;
  rclcpp::Publisher<msg::SyncStats>::SharedPtr stats_pub_;
  rclcpp::Publisher<msg::TaggedScanFrame>::SharedPtr tagged_pub_;
  rclcpp::Publisher<msg::ScanStreamClosed>::SharedPtr closed_pub_;
  rclcpp::Subscription<msg::ScanSessionState>::SharedPtr session_sub_;
  std::deque<msg::ScanSessionState> sessions_;     // recent identities (transitions and snapshots)
  Tag tag_;
  rclcpp::Subscription<msg::ClockMap>::SharedPtr clock_sub_;
  rclcpp::Subscription<msg::CalibrationStatus>::SharedPtr calibration_sub_;
  rclcpp::Subscription<msg::Motion>::SharedPtr motion_sub_;
  rclcpp::Subscription<ScanStatus>::SharedPtr scan_sub_;
  rclcpp::Subscription<msg::ImuSample>::SharedPtr imu_sub_;
  rclcpp::Subscription<msg::DevicePose>::SharedPtr pose_sub_;
  rclcpp::Subscription<msg::TriggerRecord>::SharedPtr trigger_sub_;
  rclcpp::Subscription<msg::EncoderSample>::SharedPtr encoder_sub_;
  rclcpp::Subscription<msg::RawProfile>::SharedPtr raw_sub_;
  rclcpp::TimerBase::SharedPtr timer_;
};

}  // namespace laser_hhrp

int main(int argc, char** argv) {
  rclcpp::init(argc, argv);
  rclcpp::spin(std::make_shared<laser_hhrp::SynchronizerNode>());
  rclcpp::shutdown();
  return 0;
}
