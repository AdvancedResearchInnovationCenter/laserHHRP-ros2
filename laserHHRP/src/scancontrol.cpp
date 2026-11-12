#include "laser_hhrp/scancontrol.hpp"

#if !defined(LASER_HHRP_HAS_SDK)

// Built without the scanCONTROL SDK: the laser is reported unavailable.
namespace laser_hhrp {
struct ScanControl::Impl {};
ScanControl::ScanControl() = default;
ScanControl::~ScanControl() = default;
// ---- connect: find, open, configure, start the transfer ----
// Steps are numbered below; any failure closes the connection and returns the reason.
bool ScanControl::connect(const ScanControlSettings&, std::string& error) {
  error = "built without the scanCONTROL SDK (install it, then rebuild)";
  return false;
}
void ScanControl::disconnect() {}
bool ScanControl::connected() const { return false; }
bool ScanControl::read(LaserProfile&, std::string&) { return false; }
}  // namespace laser_hhrp

#else

#include <llt.h>

#include <algorithm>
#include <chrono>
#include <deque>
#include <mutex>

namespace laser_hhrp {

namespace {
// Register bits (scanCONTROL SDK LLTDataTypes.h, 29xx manual).
constexpr uint32_t kTrigInputDigIn = 1u << 21;
constexpr uint32_t kTrigPolarityHigh = 1u << 24;
constexpr uint32_t kTrigExtActive = 1u << 25;
constexpr uint32_t kTriggerMask = kTrigExtActive | kTrigPolarityHigh | kTrigInputDigIn | (0xFu << 16);
constexpr uint32_t kIoLevel24V = 1u << 11;
constexpr uint32_t kIoPullDown = 1u << 8;          // an unplugged cable never triggers
constexpr uint32_t kIoMask = kIoLevel24V | kIoPullDown | (0xFu << 4) | 0xFu;
constexpr uint32_t kExposureAutomatic = 1u << 24;
constexpr uint32_t kTimeMask = 0x00FFFFFFu;
// Only the fields we own are changed (read-modify-write): the registers hold further mode bits
// that must keep the scanner's values (writing them as 0 slowed a 2900 to ~18 profiles/s).
uint32_t merge(uint32_t current, uint32_t mask, uint32_t value) {
  return (current & ~mask) | (value & mask);
}

constexpr guint32 kTimestampBytes = 16;  // the last 16 bytes of every profile
// Partial profile: x and z of the first stripe only (8 bytes per point, ~10 kB at 1280 points).
// The full profile (~82 kB) loses frames on the Pi's Ethernet at a few hundred Hz.
constexpr guint32 kPointBytes = 8;

int64_t now_ns() {
  return std::chrono::duration_cast<std::chrono::nanoseconds>(
             std::chrono::system_clock::now().time_since_epoch())
      .count();
}
}  // namespace

struct ScanControl::Impl {
  CInterfaceLLT* llt = nullptr;
  TScannerType type = StandardType;
  guint32 resolution = 0;
  TPartialProfile partial{};
  std::vector<double> x, z;

  std::mutex mutex;  // guards everything below (SDK thread vs read())
  struct Raw {
    std::vector<guint8> data;
    int64_t arrival_ns = 0;
  };
  std::deque<Raw> queue;
  size_t queue_size = 256;
  size_t profile_bytes = 0;
  uint32_t lost = 0;
  bool have_counter = false;
  uint32_t last_counter = 0;
  bool control_lost = false;

  // Called by the SDK for every profile: stamp it at once and queue it.
  static void on_profile(const void* data, size_t size, gpointer user) {
    const int64_t arrival = now_ns();
    auto* self = static_cast<Impl*>(user);
    std::lock_guard<std::mutex> lock(self->mutex);
    if (size != self->profile_bytes) return;  // incomplete: shows as a counter gap
    if (self->queue.size() >= self->queue_size) {
      self->queue.pop_front();
      ++self->lost;
    }
    Raw raw;
    raw.arrival_ns = arrival;
    raw.data.assign(static_cast<const guint8*>(data), static_cast<const guint8*>(data) + size);
    self->queue.push_back(std::move(raw));
  }
  static void on_control_lost(gpointer user) {
    auto* self = static_cast<Impl*>(user);
    std::lock_guard<std::mutex> lock(self->mutex);
    self->control_lost = true;
  }
  void close() {
    if (llt == nullptr) return;
    llt->TransferProfiles(NORMAL_TRANSFER, false);
    llt->Disconnect();
    delete llt;
    llt = nullptr;
  }
};

ScanControl::ScanControl() : impl_(new Impl) {}
ScanControl::~ScanControl() { disconnect(); }
bool ScanControl::connected() const { return impl_->llt != nullptr; }
void ScanControl::disconnect() { impl_->close(); }

bool ScanControl::connect(const ScanControlSettings& s, std::string& message) {
  Impl& d = *impl_;
  d.close();
  auto fail = [&](const std::string& what) {
    message = what;
    d.close();
    return false;
  };

  // 1. Find and open the scanner.
  std::string interface = s.interface;
  if (interface.empty()) {
    std::vector<char*> names(16, nullptr);
    const gint32 n = CInterfaceLLT::GetDeviceInterfaces(names.data(), 16);
    if (n <= 0 || names[0] == nullptr) return fail("no scanCONTROL found");
    interface = names[0];
  }
  d.llt = new CInterfaceLLT();
  if (d.llt->SetDeviceInterface(interface.c_str()) < GENERAL_FUNCTION_OK)
    return fail("SetDeviceInterface failed");
  if (d.llt->Connect() < GENERAL_FUNCTION_OK) return fail("connect failed");
  if (d.llt->GetLLTType(&d.type) < GENERAL_FUNCTION_OK ||
      !(d.type >= scanCONTROL29xx_25 && d.type <= scanCONTROL29xx_xxx))
    return fail("not a scanCONTROL 29xx");

  // 2. The Configuration Tools file first (the user's setup); steps 3-4 go on top of it.
  // A run that died may have left the transfer on; when it is off this returns an error, ignored.
  d.llt->TransferProfiles(NORMAL_TRANSFER, false);
  const std::string file = s.config_file.substr(s.config_file.find_last_of('/') + 1);
  if (!s.config_file.empty()) {
    const gint32 rc = d.llt->ImportLLTConfig(s.config_file.c_str(), s.config_ignore_calibration);
    if (rc < GENERAL_FUNCTION_OK) return fail("loading " + s.config_file + " failed (SDK " + std::to_string(rc) + ")");
  }

  // 3. Resolution: the sensor's (file's), or the highest supported one not above the requested one.
  d.resolution = 0;
  if (s.resolution == 0) {
    d.llt->GetResolution(&d.resolution);
  } else {
    std::vector<guint32> resolutions(16, 0);
    const gint32 nres = d.llt->GetResolutions(resolutions.data(), 16);
    for (gint32 i = 0; i < nres; ++i)
      if (resolutions[i] <= s.resolution && resolutions[i] > d.resolution) d.resolution = resolutions[i];
    if (d.resolution == 0 && nres > 0) d.resolution = resolutions[nres - 1];
  }
  if (d.resolution == 0) return fail("no resolution");

  // 4. Exposure (unless kept), idle time, IO (In1 as trigger input) and trigger mode.
  auto update = [&d](guint32 address, uint32_t mask, uint32_t value) {
    guint32 current = 0;
    if (d.llt->GetFeature(address, &current) < GENERAL_FUNCTION_OK) return false;
    return d.llt->SetFeature(address, merge(current, mask, value)) >= GENERAL_FUNCTION_OK;
  };
  if (s.exposure_time_us > 0 &&
      !update(FEATURE_FUNCTION_EXPOSURE_TIME, kTimeMask | kExposureAutomatic,
              s.exposure_time_us / 10 | (s.exposure_automatic ? kExposureAutomatic : 0u)))
    return fail("configuration failed (exposure)");
  guint32 exposure = 0;
  if (d.llt->GetFeature(FEATURE_FUNCTION_EXPOSURE_TIME, &exposure) < GENERAL_FUNCTION_OK)
    return fail("configuration failed (reading exposure)");
  const uint32_t exposure_us = (exposure & kTimeMask) * 10;
  if (s.software_rate_hz > 0 && (exposure & kExposureAutomatic))
    return fail("automatic exposure is on: software sync needs a fixed exposure (set laser.exposure_time_us)");
  // Free running: period = exposure + idle. Hardware sync: exposure + idle must fit the shortest
  // trigger period (a file's long idle would make the scanner skip pulses).
  const uint32_t idle_us = s.idle_time_us > 0 ? s.idle_time_us
      : s.software_rate_hz > 0 ? static_cast<uint32_t>(std::max(10.0, 1e6 / s.software_rate_hz - exposure_us))
      : 900u;
  const uint32_t io = (s.inputs_24v ? kIoLevel24V : 0u) | (s.input_pulldown ? kIoPullDown : 0u) |
                      ((s.digital_in_mode & 0xFu) << 4);
  const uint32_t trigger_value =
      s.external_trigger ? (kTrigExtActive | ((s.trigger_mode & 0xFu) << 16) |
                            (s.trigger_input_digital ? kTrigInputDigIn : 0u) |
                            (s.trigger_polarity_high ? kTrigPolarityHigh : 0u))
                         : 0u;
  const uint32_t trigger_mask = s.external_trigger ? kTriggerMask : kTrigExtActive;
  if (d.llt->SetResolution(d.resolution) < GENERAL_FUNCTION_OK ||
      d.llt->SetProfileConfig(PARTIAL_PROFILE) < GENERAL_FUNCTION_OK ||
      !update(FEATURE_FUNCTION_IDLE_TIME, kTimeMask, idle_us / 10) ||
      !update(FEATURE_FUNCTION_RS422_INTERFACE_FUNCTION, kIoMask, io) ||
      !update(FEATURE_FUNCTION_TRIGGER, trigger_mask, trigger_value))
    return fail("configuration failed");
  d.partial.nStartPoint = 0;
  d.partial.nStartPointData = 0;
  d.partial.nPointCount = d.resolution;
  d.partial.nPointDataWidth = kPointBytes;
  if (d.llt->SetPartialProfile(&d.partial) < GENERAL_FUNCTION_OK)
    return fail("partial profile setup failed");

  // 5. Callbacks and transfer.
  d.x.assign(d.resolution, 0.0);
  d.z.assign(d.resolution, 0.0);
  {
    std::lock_guard<std::mutex> lock(d.mutex);
    d.queue.clear();
    d.queue_size = s.queue_size == 0 ? 1 : s.queue_size;
    d.profile_bytes = d.resolution * kPointBytes;
    d.lost = 0;
    d.have_counter = false;
    d.control_lost = false;
  }
  if (d.llt->RegisterBufferCallback(reinterpret_cast<gpointer>(&Impl::on_profile), &d) <
          GENERAL_FUNCTION_OK ||
      d.llt->RegisterControlLostCallback(reinterpret_cast<gpointer>(&Impl::on_control_lost), &d) <
          GENERAL_FUNCTION_OK)
    return fail("callback registration failed");
  if (d.llt->TransferProfiles(NORMAL_TRANSFER, true) < GENERAL_FUNCTION_OK)
    return fail("transfer start failed");
  message = "setup " + (file.empty() ? std::string("sensor's own (no .sc1)") : file + " loaded") +
          "; exposure " + std::to_string(exposure_us) + " us, idle " + std::to_string(idle_us / 10 * 10) +
          " us, resolution " + std::to_string(d.resolution);
  return true;
}

// ---- read: one queued profile -> points (mm -> m), shutter times, counter ----
// The last 16 bytes of a profile are the scanner timestamp (shutter open/close, counter).
bool ScanControl::read(LaserProfile& profile, std::string& error) {
  Impl& d = *impl_;
  if (d.llt == nullptr) return false;
  Impl::Raw raw;
  {
    std::lock_guard<std::mutex> lock(d.mutex);
    if (d.control_lost) {
      error = "connection to the scanner lost";
      return false;
    }
    if (d.queue.empty()) return false;
    raw = std::move(d.queue.front());
    d.queue.pop_front();
  }
  const gint32 converted = CInterfaceLLT::ConvertPartProfile2Values(
      raw.data.data(), static_cast<guint32>(raw.data.size()), &d.partial, d.type, 0, nullptr,
      nullptr, nullptr, d.x.data(), d.z.data(), nullptr, nullptr);
  if ((converted & (CONVERT_X | CONVERT_Z)) != (CONVERT_X | CONVERT_Z)) {
    error = "profile conversion failed";
    return false;
  }
  double opened = 0, closed = 0;
  guint32 counter = 0;
  gushort enc_or_digin = 0;
  CInterfaceLLT::Timestamp2TimeAndCount(raw.data.data() + raw.data.size() - kTimestampBytes,
                                        &opened, &closed, &counter, &enc_or_digin);
  std::lock_guard<std::mutex> lock(d.mutex);
  if (d.have_counter) {
    const uint32_t gap = (counter - d.last_counter) & 0xFFFFFFu;
    if (gap > 1) d.lost += gap - 1;
  }
  d.have_counter = true;
  d.last_counter = counter;

  // The timestamp takes the place of the last two points. The SDK gives mm; z == 0: no reflection.
  const guint32 points = d.resolution - kTimestampBytes / kPointBytes;
  profile.x_m.resize(points);
  profile.z_m.resize(points);
  profile.valid.resize(points);
  for (guint32 i = 0; i < points; ++i) {
    profile.x_m[i] = static_cast<float>(d.x[i] * 1e-3);
    profile.z_m[i] = static_cast<float>(d.z[i] * 1e-3);
    profile.valid[i] = d.z[i] != 0.0;
  }
  profile.profile_counter = counter;
  profile.shutter_open_s = opened;
  profile.shutter_close_s = closed;
  profile.arrival_ns = raw.arrival_ns;
  profile.lost_profiles = d.lost;
  return true;
}

}  // namespace laser_hhrp

#endif
