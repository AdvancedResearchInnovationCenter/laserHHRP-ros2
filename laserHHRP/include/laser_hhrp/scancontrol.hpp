// Micro-Epsilon scanCONTROL 29xx (LLT2900-10/BL) through the scanCONTROL Linux SDK (libllt).
//
// connect(): finds/opens the scanner, sets resolution, exposure, trigger (In1 from the Nano, or
// internal = free running) and starts the transfer. Profiles arrive on the SDK's thread and are
// queued; read() takes them in order. Without the SDK installed (LASER_HHRP_HAS_SDK not defined)
// connect() always fails with a clear message.
#pragma once

#include <cstdint>
#include <memory>
#include <string>
#include <vector>

namespace laser_hhrp {

struct ScanControlSettings {
  std::string interface;           // scanner IP / name as listed by the SDK; "" = first found
  std::string config_file;         // Configuration Tools .sc1 loaded first, the rest goes on top; "" = none
  bool config_ignore_calibration = true;  // config_file: keep the scanner's own calibration
  uint32_t resolution = 0;         // points per profile (highest supported <= this); 0 = keep the sensor's
  uint32_t exposure_time_us = 0;   // multiples of 10 us; 0 = keep the sensor's (and its automatic bit)
  bool exposure_automatic = false;  // with exposure_time_us > 0: scanner's automatic exposure
  double software_rate_hz = 0;     // free running: idle = 1/rate - exposure (when idle_time_us is 0)
  uint32_t idle_time_us = 0;       // fixed idle; 0 = automatic (software sync: from the rate; hardware: 900)
  bool external_trigger = false;    // true: one profile per trigger on In1 (hardware sync)
  bool inputs_24v = false;          // input level: false = TTL 5 V (the Nano)
  bool input_pulldown = true;       // pull-down on the inputs: an unplugged cable never triggers
  uint32_t digital_in_mode = 2;     // IO register, bits 4..7: 2 = In1 is the trigger input only
  uint32_t trigger_mode = 0;        // trigger register, bits 16..19: 0 = edge
  bool trigger_input_digital = true;   // trigger source: digital input In1
  bool trigger_polarity_high = true;   // trigger on the rising edge
  uint32_t queue_size = 256;        // profiles buffered between the SDK thread and read()
};

struct LaserProfile {
  std::vector<float> x_m, z_m;
  std::vector<bool> valid;
  uint32_t profile_counter = 0;  // scanner counter (24 bit, wraps)
  double shutter_open_s = 0;     // scanner clock (wraps every 128 s)
  double shutter_close_s = 0;
  int64_t arrival_ns = 0;        // Pi system time when the SDK delivered it
  uint32_t lost_profiles = 0;    // dropped since connect (queue full or gaps in the counter)
};

class ScanControl {
 public:
  ScanControl();
  ~ScanControl();
  ScanControl(const ScanControl&) = delete;
  ScanControl& operator=(const ScanControl&) = delete;

  /// On success `message` describes the active setup (file, exposure, idle, resolution); else the error.
  bool connect(const ScanControlSettings& settings, std::string& message);
  void disconnect();
  bool connected() const;
  /// Next profile (non-blocking). False if none waiting; `error` set if the scanner was lost.
  bool read(LaserProfile& profile, std::string& error);

 private:
  struct Impl;
  std::unique_ptr<Impl> impl_;
};

}  // namespace laser_hhrp
