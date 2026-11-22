// inspection_node (layer 5): the measurement algorithms, run once per scan after detection.
//
// Input   inspection/detection_finalized (laser_hhrp/DetectionFinalized) from the tagged detector: the scan
//         folder (output_dir) holds summary.csv plus one PCD per feature, all on disk before it is sent.
// Output  inspection/measurement (laser_hhrp/MeasurementFinalized): exactly one per successful detection, with
//         every target feature (CSK Depth: countersinks; Rivet Flushness: rivets) measured or a reason code;
//         plus <scan folder>/measurement.csv with the same values (for offline repeatability checks).
//
// Algorithms (git submodules in third_party/, contract docs/operator_ui/MEASUREMENT_INTEGRATION.md):
//   CSK depth        laser_csk_inspection/csk_depth   csk::measure_countersink_depth
//   rivet flushness  rivet_flushness                  rivet_flushness::measure_rivet_flushness
// Both are pure and thread-safe: one scan's features run on inspection.workers threads. Scans are measured
// one at a time, in arrival order, on a worker thread (the ROS callbacks never wait for it).
#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <deque>
#include <fstream>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include <rclcpp/rclcpp.hpp>

#include "laser_hhrp/measurement_input.hpp"
#include "laser_hhrp/msg/detection_finalized.hpp"
#include "laser_hhrp/msg/measured_feature.hpp"
#include "laser_hhrp/msg/measurement_finalized.hpp"

#ifdef LASER_HHRP_HAS_CSK_DEPTH
#include "csk_depth.h"
#endif
#ifdef LASER_HHRP_HAS_RIVET_FLUSHNESS
#include "rivet_flushness/measure.hpp"
#endif

namespace laser_hhrp {

namespace {
double seconds_since(std::chrono::steady_clock::time_point t0) {
  return std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
}
}  // namespace

class InspectionNode : public rclcpp::Node {
 public:
  InspectionNode() : Node("inspection") {
    workers_ = static_cast<int>(declare_parameter("inspection.workers", 4));
    csk_invert_z_ = declare_parameter("inspection.csk_invert_z", false);
    rivet_invert_z_ = declare_parameter("inspection.rivet_invert_z", false);
    rivet_check_orientation_ = declare_parameter("inspection.rivet_check_orientation", true);
    csk_min_m_ = declare_parameter("inspection.csk_depth_min_m", 0.000947);
    csk_max_m_ = declare_parameter("inspection.csk_depth_max_m", 0.001147);
    flush_min_m_ = declare_parameter("inspection.flushness_min_m", -0.00015);
    flush_max_m_ = declare_parameter("inspection.flushness_max_m", 0.00005);
    csk_angle_deg_ = declare_parameter("inspection.csk_angle_deg", 130.0);
    csk_bore_mm_ = declare_parameter("inspection.csk_bore_diameter_mm", 4.25);
    write_csv_ = declare_parameter("inspection.write_csv", true);
    workers_ = std::clamp(workers_, 1, 16);

    // the detectors publish transient-local; volatile here: a restarted node does not re-measure old scans
    auto reliable = rclcpp::QoS(50).reliable();
    pub_ = create_publisher<msg::MeasurementFinalized>("inspection/measurement",
                                                       rclcpp::QoS(50).reliable().transient_local());
    sub_ = create_subscription<msg::DetectionFinalized>(
        "inspection/detection_finalized", reliable, [this](msg::DetectionFinalized::SharedPtr m) { enqueue(m); });
    worker_ = std::thread([this] { run(); });
    RCLCPP_INFO(get_logger(), "measurement ready: CSK depth %s, rivet flushness %s, %d workers, invert_z csk %s rivet %s",
                kHaveCsk ? "built" : "NOT BUILT", kHaveRivet ? "built" : "NOT BUILT", workers_,
                csk_invert_z_ ? "true" : "false", rivet_invert_z_ ? "true" : "false");
  }

  ~InspectionNode() override {
    {
      std::lock_guard<std::mutex> lock(mutex_);
      stop_ = true;
    }
    cv_.notify_all();
    if (worker_.joinable()) worker_.join();
  }

 private:
#ifdef LASER_HHRP_HAS_CSK_DEPTH
  static constexpr bool kHaveCsk = true;
#else
  static constexpr bool kHaveCsk = false;
#endif
#ifdef LASER_HHRP_HAS_RIVET_FLUSHNESS
  static constexpr bool kHaveRivet = true;
#else
  static constexpr bool kHaveRivet = false;
#endif

  struct Result {
    bool measured = false;
    double value_m = 0;
    std::string reason, note;
    double elapsed_s = 0;
    std::size_t points = 0;
  };

  void enqueue(const msg::DetectionFinalized::SharedPtr& m) {
    if (!m->success) {
      RCLCPP_INFO(get_logger(), "scan %s: detection failed, nothing to measure", m->scan_id.c_str());
      return;
    }
    std::lock_guard<std::mutex> lock(mutex_);
    // a redelivered finalization (same scan) is measured once
    if (std::find(seen_.begin(), seen_.end(), m->scan_id) != seen_.end()) return;
    seen_.push_back(m->scan_id);
    if (seen_.size() > 200) seen_.pop_front();
    queue_.push_back(m);
    cv_.notify_one();
  }

  void run() {
    for (;;) {
      msg::DetectionFinalized::SharedPtr job;
      {
        std::unique_lock<std::mutex> lock(mutex_);
        cv_.wait(lock, [this] { return stop_ || !queue_.empty(); });
        if (stop_) return;
        job = queue_.front();
        queue_.pop_front();
      }
      try {
        measure_scan(*job);
      } catch (const std::exception& e) {  // never lose the scan's answer
        RCLCPP_ERROR(get_logger(), "scan %s: measurement crashed: %s", job->scan_id.c_str(), e.what());
        msg::MeasurementFinalized out;
        out.scan_id = job->scan_id;
        out.detector_mode = job->detector_mode;
        out.success = false;
        out.error = std::string("measurement error: ") + e.what();
        out.header.stamp = now();
        pub_->publish(out);
      }
    }
  }

  Result measure_one(MeasureKind kind, const std::string& folder, const SummaryRow& row) const {
    Result r;
    auto t0 = std::chrono::steady_clock::now();
    if (row.partial) {  // the contract: partly scanned features never reach the algorithm
      r.reason = "partial";
      return r;
    }
    std::vector<float> xyz;
    std::string error;
    if (!read_pcd_xyz(folder + "/" + feature_pcd_name(kind, row.id), xyz, error)) {
      r.reason = "failed";
      r.note = error;
      return r;
    }
    r.points = xyz.size() / 3;
    if (kind == MeasureKind::CskDepth) {
#ifdef LASER_HHRP_HAS_CSK_DEPTH
      csk::Params p;
      p.csk_angle_deg = csk_angle_deg_;
      p.bore_diameter_mm = csk_bore_mm_;
      csk::Measurement m = csk::measure_countersink_depth(xyz.data(), r.points, row.centre_m, row.diameter_m,
                                                          csk_invert_z_, p);
      r.measured = m.measured;
      r.value_m = m.value_m;
      r.reason = m.reason;
      r.note = m.message;
#else
      r.reason = "failed";
      r.note = "CSK depth algorithm not built (third_party/laser_csk_inspection missing)";
#endif
    } else {
#ifdef LASER_HHRP_HAS_RIVET_FLUSHNESS
      rivet_flushness::Params p;
      p.check_orientation = rivet_check_orientation_;
      std::string note;
      p.log = [&note](int, const std::string& text) { note = text; };
      rivet_flushness::Measurement m = rivet_flushness::measure_rivet_flushness(
          xyz.data(), r.points, row.centre_m, row.diameter_m, rivet_invert_z_, p);
      r.measured = m.measured;
      r.value_m = m.value_m;
      r.reason = m.reason;
      r.note = note;
#else
      r.reason = "failed";
      r.note = "rivet flushness algorithm not built (third_party/rivet_flushness missing)";
#endif
    }
    if (r.measured && !std::isfinite(r.value_m)) {
      r.measured = false;
      r.reason = "failed";
      r.note = "non-finite value";
    }
    if (!r.measured && r.reason.empty()) r.reason = "failed";
    if (r.measured) r.reason.clear();
    r.elapsed_s = seconds_since(t0);
    return r;
  }

  void measure_scan(const msg::DetectionFinalized& job) {
    auto t0 = std::chrono::steady_clock::now();
    msg::MeasurementFinalized out;
    out.scan_id = job.scan_id;
    out.detector_mode = job.detector_mode;
    MeasureKind kind{};
    std::vector<SummaryRow> rows;
    std::string error;
    if (!measure_kind(job.detector_mode, kind)) {
      error = "unknown detector mode '" + job.detector_mode + "'";
    } else if (job.output_dir.empty()) {
      error = "the detector sent no scan folder (rebuild hole_detector / rivet_detector)";
    } else {
      std::ifstream f(job.output_dir + "/summary.csv");
      if (!f) error = "cannot read " + job.output_dir + "/summary.csv";
      else parse_summary(f, kind, rows, error);
    }
    if (!error.empty()) {
      RCLCPP_ERROR(get_logger(), "scan %s: %s", job.scan_id.c_str(), error.c_str());
      out.success = false;
      out.error = error;
      out.header.stamp = now();
      pub_->publish(out);
      return;
    }
    out.quantity = quantity_of(kind);
    out.lower_limit_m = kind == MeasureKind::CskDepth ? csk_min_m_ : flush_min_m_;
    out.upper_limit_m = kind == MeasureKind::CskDepth ? csk_max_m_ : flush_max_m_;

    std::vector<SummaryRow> targets;
    for (const auto& r : rows)
      if (r.target) targets.push_back(r);
    std::vector<Result> results(targets.size());
    std::atomic<std::size_t> next{0};
    auto work = [&] {
      for (std::size_t i = next++; i < targets.size(); i = next++) results[i] = measure_one(kind, job.output_dir, targets[i]);
    };
    std::vector<std::thread> pool;
    const std::size_t n_threads = std::min<std::size_t>(static_cast<std::size_t>(workers_), targets.size());
    for (std::size_t t = 1; t < n_threads; ++t) pool.emplace_back(work);
    work();
    for (auto& t : pool) t.join();

    int measured = 0;
    for (std::size_t i = 0; i < targets.size(); ++i) {
      const auto& r = results[i];
      msg::MeasuredFeature f;
      f.feature_id = targets[i].id;
      f.measured = r.measured;
      f.value_m = r.measured ? r.value_m : 0.0;
      f.reason = r.reason;
      f.elapsed_s = r.elapsed_s;
      out.features.push_back(f);
      measured += r.measured;
      if (r.measured) {
        RCLCPP_INFO(get_logger(), "scan %s %s %u: %.1f um (%zu points, %.0f ms)", job.scan_id.c_str(), out.quantity.c_str(),
                    f.feature_id, r.value_m * 1e6, r.points, r.elapsed_s * 1e3);
      } else {
        RCLCPP_INFO(get_logger(), "scan %s %s %u: not measured, %s%s%s (%zu points, %.0f ms)", job.scan_id.c_str(),
                    out.quantity.c_str(), f.feature_id, r.reason.c_str(), r.note.empty() ? "" : ": ", r.note.c_str(),
                    r.points, r.elapsed_s * 1e3);
      }
    }
    out.success = true;
    out.elapsed_s = seconds_since(t0);
    if (write_csv_) write_csv(job.output_dir, out, results);
    out.header.stamp = now();
    pub_->publish(out);
    RCLCPP_INFO(get_logger(), "scan %s: %d of %zu %s measured in %.2f s", job.scan_id.c_str(), measured,
                targets.size(), kind == MeasureKind::CskDepth ? "countersinks" : "rivets", out.elapsed_s);
  }

  // measurement.csv next to summary.csv: id, value_um, in_limits, reason, note. Written before the message, so a
  // scan's file is complete when the UI shows the result. A write failure is logged, never fatal.
  void write_csv(const std::string& folder, const msg::MeasurementFinalized& out, const std::vector<Result>& results) {
    const std::string path = folder + "/measurement.csv", tmp = path + ".tmp";
    {
      std::ofstream f(tmp);
      f << "id," << out.quantity << "_um,in_limits,reason,points,ms,note\n";
      for (std::size_t i = 0; i < out.features.size(); ++i) {
        const auto& m = out.features[i];
        std::string note = results[i].note;
        std::replace(note.begin(), note.end(), ',', ';');
        std::replace(note.begin(), note.end(), '\n', ' ');
        char value[32] = "";
        if (m.measured) std::snprintf(value, sizeof value, "%.2f", m.value_m * 1e6);
        f << m.feature_id << ',' << value << ','
          << (m.measured ? (within_limits(m.value_m, out.lower_limit_m, out.upper_limit_m) ? "yes" : "no") : "")
          << ',' << m.reason << ',' << results[i].points << ',' << static_cast<int>(results[i].elapsed_s * 1e3 + 0.5)
          << ',' << note << '\n';
      }
      if (!f) {
        RCLCPP_WARN(get_logger(), "cannot write %s", tmp.c_str());
        return;
      }
    }
    if (std::rename(tmp.c_str(), path.c_str()) != 0) RCLCPP_WARN(get_logger(), "cannot write %s", path.c_str());
  }

  int workers_ = 4;
  bool csk_invert_z_ = false, rivet_invert_z_ = false, rivet_check_orientation_ = true, write_csv_ = true;
  double csk_min_m_ = 0, csk_max_m_ = 0, flush_min_m_ = 0, flush_max_m_ = 0, csk_angle_deg_ = 130, csk_bore_mm_ = 4.25;
  rclcpp::Publisher<msg::MeasurementFinalized>::SharedPtr pub_;
  rclcpp::Subscription<msg::DetectionFinalized>::SharedPtr sub_;
  std::mutex mutex_;
  std::condition_variable cv_;
  std::deque<msg::DetectionFinalized::SharedPtr> queue_;
  std::deque<std::string> seen_;
  bool stop_ = false;
  std::thread worker_;
};

}  // namespace laser_hhrp

int main(int argc, char** argv) {
  rclcpp::init(argc, argv);
  rclcpp::spin(std::make_shared<laser_hhrp::InspectionNode>());
  rclcpp::shutdown();
  return 0;
}
