// Inputs of the measurement algorithms (inspection_node): one detector scan folder, as written by
// hole_detector / rivet_detector in tagged mode. Plain C++, no ROS (unit-tested in test/test_logic.cpp).
//
//   <scan folder>/summary.csv   one row per feature: id, cx_mm, cy_mm, cz_mm, top_diam_mm | head_diam_mm,
//                               countersunk (holes only), partial, ...
//   <scan folder>/hole_NN.pcd   (or rivet_NN.pcd, NN = id, at least two digits) binary float32 x y z, metres
//
// Contract: docs/operator_ui/MEASUREMENT_INTEGRATION.md.
#pragma once

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <istream>
#include <map>
#include <sstream>
#include <string>
#include <vector>

namespace laser_hhrp {

enum class MeasureKind { CskDepth, RivetFlushness };

// "csk_depth" / "rivet_flushness" (the detectors' detector_mode); false for anything else.
inline bool measure_kind(const std::string& mode, MeasureKind& kind) {
  if (mode == "csk_depth") kind = MeasureKind::CskDepth;
  else if (mode == "rivet_flushness") kind = MeasureKind::RivetFlushness;
  else return false;
  return true;
}

inline const char* quantity_of(MeasureKind k) { return k == MeasureKind::CskDepth ? "csk_depth" : "flushness"; }

// The PCD of feature `id` in a scan folder.
inline std::string feature_pcd_name(MeasureKind k, std::uint32_t id) {
  char name[32];
  std::snprintf(name, sizeof name, "%s_%02u.pcd", k == MeasureKind::CskDepth ? "hole" : "rivet", id);
  return name;
}

struct SummaryRow {
  std::uint32_t id = 0;
  double centre_m[3] = {0, 0, 0};
  double diameter_m = 0;
  bool partial = false;
  bool target = false;  // CSK Depth: countersunk; Rivet Flushness: every rivet
};

namespace detail {
inline std::vector<std::string> split_csv(const std::string& line) {
  std::vector<std::string> out;
  std::string cell;
  std::stringstream ss(line);
  while (std::getline(ss, cell, ',')) {
    while (!cell.empty() && (cell.back() == '\r' || cell.back() == ' ')) cell.pop_back();
    while (!cell.empty() && cell.front() == ' ') cell.erase(cell.begin());
    out.push_back(cell);
  }
  if (!line.empty() && line.back() == ',') out.emplace_back();
  return out;
}
inline bool to_double(const std::string& s, double& v) {
  if (s.empty()) return false;
  char* end = nullptr;
  v = std::strtod(s.c_str(), &end);
  return end && *end == '\0' && std::isfinite(v);
}
inline bool to_bool(const std::string& s, bool& v) {
  if (s == "True" || s == "true" || s == "1") v = true;
  else if (s == "False" || s == "false" || s == "0") v = false;
  else return false;
  return true;
}
}  // namespace detail

// Parse summary.csv. Returns false with `error` on a missing column or a bad cell (the scan cannot be
// measured reliably then); rows come back in file order.
inline bool parse_summary(std::istream& in, MeasureKind kind, std::vector<SummaryRow>& rows, std::string& error) {
  rows.clear();
  std::string line;
  if (!std::getline(in, line)) {
    error = "summary.csv is empty";
    return false;
  }
  const auto header = detail::split_csv(line);
  std::map<std::string, std::size_t> col;
  for (std::size_t i = 0; i < header.size(); ++i) col[header[i]] = i;
  const char* diam = kind == MeasureKind::CskDepth ? "top_diam_mm" : "head_diam_mm";
  std::vector<std::string> need = {"id", "cx_mm", "cy_mm", "cz_mm", diam, "partial"};
  if (kind == MeasureKind::CskDepth) need.push_back("countersunk");
  for (const auto& n : need) {
    if (!col.count(n)) {
      error = "summary.csv has no column " + n;
      return false;
    }
  }
  int line_no = 1;
  while (std::getline(in, line)) {
    ++line_no;
    if (line.empty() || line == "\r") continue;
    const auto cells = detail::split_csv(line);
    auto cell = [&](const std::string& n) -> std::string {
      std::size_t i = col.at(n);
      return i < cells.size() ? cells[i] : std::string();
    };
    SummaryRow r;
    double id = 0, mm[3], d = 0;
    bool ok = detail::to_double(cell("id"), id) && id >= 0 && id == std::floor(id) && id < 1e9 &&
              detail::to_double(cell("cx_mm"), mm[0]) && detail::to_double(cell("cy_mm"), mm[1]) &&
              detail::to_double(cell("cz_mm"), mm[2]) && detail::to_double(cell(diam), d) &&
              detail::to_bool(cell("partial"), r.partial);
    bool countersunk = true;
    if (ok && kind == MeasureKind::CskDepth) ok = detail::to_bool(cell("countersunk"), countersunk);
    if (!ok) {
      error = "summary.csv line " + std::to_string(line_no) + " cannot be read";
      return false;
    }
    r.id = static_cast<std::uint32_t>(id);
    for (int k = 0; k < 3; ++k) r.centre_m[k] = mm[k] / 1000.0;
    r.diameter_m = d / 1000.0;
    r.target = countersunk;
    for (const auto& other : rows) {
      if (other.id == r.id) {
        error = "summary.csv lists feature " + std::to_string(r.id) + " twice";
        return false;
      }
    }
    rows.push_back(r);
  }
  return true;
}

// Read a PCD holding x y z float32 (binary, or ascii), the detectors' format. Other layouts are refused.
inline bool read_pcd_xyz(const std::string& path, std::vector<float>& xyz, std::string& error) {
  xyz.clear();
  std::ifstream f(path, std::ios::binary);
  if (!f) {
    error = "cannot open " + path;
    return false;
  }
  std::string line, fields, size, type, data;
  long points = -1;
  while (std::getline(f, line)) {
    if (!line.empty() && line.back() == '\r') line.pop_back();
    if (line.empty() || line[0] == '#') continue;
    std::istringstream ls(line);
    std::string key;
    ls >> key;
    std::string rest;
    std::getline(ls, rest);
    if (!rest.empty() && rest[0] == ' ') rest.erase(0, 1);
    if (key == "FIELDS") fields = rest;
    else if (key == "SIZE") size = rest;
    else if (key == "TYPE") type = rest;
    else if (key == "POINTS") points = std::atol(rest.c_str());
    else if (key == "DATA") {
      data = rest;
      break;
    }
  }
  if (fields != "x y z" || size != "4 4 4" || type != "F F F" || points < 0) {
    error = path + ": not an x y z float32 PCD";
    return false;
  }
  xyz.resize(static_cast<std::size_t>(points) * 3);
  if (data == "binary") {
    f.read(reinterpret_cast<char*>(xyz.data()), static_cast<std::streamsize>(xyz.size() * sizeof(float)));
    if (f.gcount() != static_cast<std::streamsize>(xyz.size() * sizeof(float))) {
      error = path + ": truncated";
      xyz.clear();
      return false;
    }
  } else if (data == "ascii") {
    for (auto& v : xyz) {
      if (!(f >> v)) {
        error = path + ": truncated";
        xyz.clear();
        return false;
      }
    }
  } else {
    error = path + ": unsupported PCD data " + data;
    return false;
  }
  return true;
}

// The GO range is inclusive at both ends.
inline bool within_limits(double value, double lower, double upper) { return value >= lower && value <= upper; }

// Did the device rock while its lines crossed the feature? The cloud's y is the encoder's travel, so a roller that
// stops while the device tips (operator pausing on a feature, 2026-11-22 holes2 hole 5: 0.1 s at one encoder count,
// skin 0.7 / 0.3 mm lower either side) stacks tilted lines at one y. Per 0.25 mm of y, a line z = a x + c is fitted to
// the skin (outside the feature's radius + 1 mm, both sides, x spread >= 6 mm); a bin's jump is |c - median c of the
// bins within 3 mm|. Only bins crossing the feature (|y - cy| <= radius + 1 mm) count: a stop out on the skin leaves
// the feature itself intact. Real rows: every good feature <= 0.031 mm, the rocked hole 0.27-0.29 mm.
struct LineJump {
  bool judged = false;    // enough skin lines to tell (else: no verdict, the algorithm decides)
  double jump_m = 0;      // the largest jump over the feature
  double at_y_m = 0;      // where (cloud y)
};

inline LineJump line_jump(const std::vector<float>& xyz, const double centre_m[3], double diameter_m) {
  constexpr double kBin = 0.00025, kWindow = 0.003, kMargin = 0.001, kMinSpread = 0.006;
  constexpr std::size_t kMinPoints = 30, kMinBins = 8;
  struct Sums {
    double n = 0, x = 0, xx = 0, z = 0, xz = 0, lo = 1e9, hi = -1e9;
  };
  std::map<long, Sums> bins;
  const double skin_r = diameter_m / 2 + kMargin;
  for (std::size_t i = 0; i + 2 < xyz.size(); i += 3) {
    const double x = xyz[i] - centre_m[0], y = xyz[i + 1] - centre_m[1], z = xyz[i + 2];
    if (!std::isfinite(x) || !std::isfinite(y) || !std::isfinite(z) || std::hypot(x, y) <= skin_r) continue;
    Sums& s = bins[static_cast<long>(std::floor(y / kBin))];
    s.n += 1, s.x += x, s.xx += x * x, s.z += z, s.xz += x * z;
    s.lo = std::min(s.lo, x), s.hi = std::max(s.hi, x);
  }
  std::vector<double> ys, cs;
  for (const auto& [k, s] : bins) {
    if (s.n < kMinPoints || s.hi - s.lo < kMinSpread) continue;
    const double det = s.n * s.xx - s.x * s.x;
    if (det <= 0) continue;
    const double a = (s.n * s.xz - s.x * s.z) / det;
    ys.push_back((k + 0.5) * kBin);
    cs.push_back((s.z - a * s.x) / s.n);  // the line's height at the feature's x
  }
  LineJump out;
  if (ys.size() < kMinBins) return out;
  out.judged = true;
  std::vector<double> near;
  for (std::size_t i = 0; i < ys.size(); ++i) {
    if (std::fabs(ys[i]) > skin_r) continue;
    near.clear();
    for (std::size_t j = 0; j < ys.size(); ++j)
      if (std::fabs(ys[j] - ys[i]) <= kWindow) near.push_back(cs[j]);
    std::nth_element(near.begin(), near.begin() + near.size() / 2, near.end());
    const double jump = std::fabs(cs[i] - near[near.size() / 2]);
    if (jump > out.jump_m) out.jump_m = jump, out.at_y_m = ys[i] + centre_m[1];
  }
  return out;
}

}  // namespace laser_hhrp
