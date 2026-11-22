// Unit tests of the plain logic in include/laser_hhrp (no ROS graph needed).
#include <gtest/gtest.h>

#include <cmath>
#include <cstdio>
#include <fstream>
#include <sstream>

#include "laser_hhrp/calibration_data.hpp"
#include "laser_hhrp/clock_sync.hpp"
#include "laser_hhrp/frame_pairer.hpp"
#include "laser_hhrp/gyro_calibrator.hpp"
#include "laser_hhrp/measurement_input.hpp"
#include "laser_hhrp/monitor_rules.hpp"
#include "laser_hhrp/odometry.hpp"
#include "laser_hhrp/scanner_clock.hpp"
#include "laser_hhrp/serial_protocol.hpp"
#include "laser_hhrp/software_pairer.hpp"

using namespace laser_hhrp;

namespace {
// Irregular hand-held intervals (ms) between triggers.
const double kIntervalsMs[] = {9, 11, 8.5, 12, 10, 9.5, 13, 8, 10.5, 11.5, 9, 12.5, 10, 8.8, 11.2};

Trigger trigger(uint32_t index, int64_t t_ns) {
  Trigger t;
  t.index = index;
  t.device_ns = t_ns;
  t.count = static_cast<int32_t>(4 * (index + 1));
  t.direction = 1;
  return t;
}
Raw profile(uint32_t counter, double shutter_s, int64_t pi_ns) {
  Raw r;
  r.profile_counter = counter;
  r.shutter_open_s = shutter_s;
  r.stamp_ns = pi_ns;
  return r;
}
}  // namespace

TEST(FramePairer, PairsByNumberAndCountsALostProfile) {
  FramePairer pairer;
  int64_t t = 1000000000;
  const uint32_t first_counter = 5000;  // the scanner's counter does not start at 0
  for (uint32_t k = 0; k < 15; ++k) {
    t += static_cast<int64_t>(kIntervalsMs[k] * 1e6);
    pairer.add_trigger(trigger(k, t), t);
    if (k != 10) pairer.add_profile(profile(first_counter + k, 20.0 + t * 1e-9, t + 3000000));
  }
  ASSERT_TRUE(pairer.locked());
  auto pairs = pairer.take(t + 10000000);
  EXPECT_EQ(pairs.size(), 14u);
  for (const auto& p : pairs) {
    EXPECT_EQ(p.profile.profile_counter - first_counter, p.trigger.index);
    EXPECT_FALSE(p.timing_mismatch);
  }
  pairer.take(t + 2000000000);  // the trigger without a profile gives up
  EXPECT_EQ(pairer.stats().missing_profiles, 1u);
}

TEST(SoftwarePairer, InterpolatesTheCountAtTheProfileTime) {
  SoftwarePairer pairer(0.0);
  for (int i = 0; i < 3; ++i) {
    Trigger ref;
    ref.device_ns = i * 10000000;
    ref.count = i * 100;
    pairer.add_record(ref, 1000000000 + i * 10000000);  // Pi time = Nano time + 1 s
  }
  pairer.add_profile(profile(0, 0, 1000000000 + 5000000));  // halfway between the first two
  auto pairs = pairer.take(1000000000 + 30000000);
  ASSERT_EQ(pairs.size(), 1u);
  EXPECT_EQ(pairs[0].trigger.count, 50);
  EXPECT_EQ(pairs[0].trigger.device_ns, 5000000);
}

TEST(ClockSync, FindsOffsetAndDrift) {
  ClockSync clock;
  const double drift = 20e-6;
  for (uint32_t i = 1; i <= 20; ++i) {
    const int64_t host_send = 5000000000LL + i * 500000000LL;
    const int64_t device = static_cast<int64_t>((host_send + 500000 - 5000000000LL) / (1 + drift));
    clock.sent(i, host_send);
    ASSERT_TRUE(clock.reply(i, host_send, device, device, host_send + 1000000));
  }
  EXPECT_NEAR(clock.drift(), drift, 1e-7);
  const int64_t device = 3000000000LL;
  EXPECT_NEAR(static_cast<double>(clock.to_host(device)), 5000000000.0 + device * (1 + drift), 2000.0);
}

TEST(ScannerClock, RemovesArrivalJitter) {
  ScannerClock clock;
  int64_t mapped = 0;
  for (int i = 0; i < 100; ++i) {
    const double scanner_s = 10.0 + i * 0.008;
    const int64_t arrival = 1000000000000LL + static_cast<int64_t>(i * 8e6) + (i % 7) * 300000 + 2000000;
    mapped = clock.map(scanner_s, scanner_s, arrival);
  }
  // Pi time = scanner time + the smallest delay (2 ms), whatever the jitter.
  EXPECT_NEAR(static_cast<double>(mapped), 1000000000000.0 + 99 * 8e6 + 2e6, 1000.0);
}

TEST(SerialProtocol, EncodeThenRead) {
  protocol::TriggerPacket t{123456789ULL, 42, -17, -1, 1};
  const auto bytes = protocol::encode(protocol::PKT_TRIGGER, t);
  protocol::PacketReader reader;
  reader.push(0x00);  // noise before the packet is skipped
  bool complete = false;
  for (uint8_t b : bytes) complete = reader.push(b);
  ASSERT_TRUE(complete);
  protocol::TriggerPacket out{};
  ASSERT_TRUE(reader.payload(out));
  EXPECT_EQ(out.time_us, 123456789ULL);
  EXPECT_EQ(out.index, 42u);
  EXPECT_EQ(out.count, -17);
  EXPECT_EQ(out.direction, -1);
  auto bad = bytes;
  bad[6] ^= 0xFF;  // corrupted payload
  for (uint8_t b : bad) complete = reader.push(b);
  EXPECT_FALSE(complete);
  EXPECT_EQ(reader.errors(), 1u);
}

TEST(GyroCalibrator, AveragesTheBiasWhileStill) {
  GyroCalibrator cal;
  cal.start(0);
  int64_t t = 0;
  for (int i = 0; i < 1000 && cal.state() == GyroCalibrator::State::collecting; ++i) {
    t += 4000000;
    cal.imu({0.01 + (i % 2 ? 1e-3 : -1e-3), -0.02, 0.005}, {0, 0, 9.81}, t);
  }
  ASSERT_EQ(cal.state(), GyroCalibrator::State::done);
  EXPECT_NEAR(cal.bias()[0], 0.01, 1e-4);
  EXPECT_NEAR(cal.bias()[1], -0.02, 1e-6);
}

TEST(CalibrationFile, SaveAndLoad) {
  CalibrationData c;
  c.gyro_calibrated = true;
  c.gyro_bias_radps = {0.1, 0.2, 0.3};
  c.extrinsics.calibrated = true;
  c.extrinsics.metres_per_count = 2.4e-5;
  c.extrinsics.encoder_direction_sign = -1;
  const std::string path = testing::TempDir() + "/calibration_test.yaml";
  ASSERT_TRUE(calibration_file::save(path, c));
  CalibrationData loaded;
  std::string error;
  ASSERT_TRUE(calibration_file::load(path, loaded, error)) << error;
  EXPECT_TRUE(loaded.gyro_calibrated);
  EXPECT_DOUBLE_EQ(loaded.gyro_bias_radps[2], 0.3);
  EXPECT_DOUBLE_EQ(loaded.extrinsics.metres_per_count, 2.4e-5);
  EXPECT_EQ(loaded.extrinsics.encoder_direction_sign, -1);
  std::remove(path.c_str());
}

TEST(MonitorRules, SpeedAndDevices) {
  MonitorInput in;
  in.scanning = true;
  in.max_speed_mps = 0.1;
  in.speed_mps = 0.09;
  auto f = evaluate(in);
  ASSERT_EQ(f.size(), 1u);
  EXPECT_EQ(f[0].severity, Severity::WARNING);  // close to the maximum
  in.laser_ok = false;
  f = evaluate(in);
  EXPECT_EQ(f[0].severity, Severity::FATAL);
}

// ---- measurement inputs (inspection_node) --------------------------------------------------------

TEST(MonitorRules, SoftwareSyncSaysLinesNotSkippedProfiles) {
  MonitorInput in;
  in.scanning = true;
  in.max_speed_mps = 0.06;
  in.speed_mps = 0.07;
  in.software_sync = true;   // the laser runs free: too fast spreads the lines, nothing is skipped
  auto f = evaluate(in);
  ASSERT_EQ(f.size(), 1u);
  EXPECT_EQ(f[0].message, "scan speed too high: scan lines too far apart");
  in.software_sync = false;
  EXPECT_EQ(evaluate(in)[0].message, "scan speed too high: profiles are skipped");
}

TEST(MonitorRules, VibrationOnlyWhileScanning) {
  MonitorInput in;
  in.imu_ok = true;
  in.rotation_radps = 1.8;          // the device carried between scans
  EXPECT_TRUE(evaluate(in).empty());
  in.scanning = true;
  auto f = evaluate(in);
  ASSERT_EQ(f.size(), 1u);
  EXPECT_EQ(f[0].message, "excess vibration");
}

TEST(MeasurementInput, HoleSummaryTargetsAreTheCountersinks) {
  std::istringstream in(
      "id,cx_mm,cy_mm,cz_mm,top_diam_mm,fit_rms_mm,ring_coverage,countersunk,partial,scanned_share,dist_to_prev_mm\n"
      "1,0.459,-13.512,63.207,8.864,0.047,1.00,True,False,1.00,\n"
      "2,1.122,-34.186,63.169,4.542,0.151,1.00,False,False,1.00,20.685\n"
      "3,1.5,-54.0,63.1,8.9,0.05,0.80,True,True,0.60,20.1\r\n");
  std::vector<SummaryRow> rows;
  std::string error;
  ASSERT_TRUE(parse_summary(in, MeasureKind::CskDepth, rows, error)) << error;
  ASSERT_EQ(rows.size(), 3u);
  EXPECT_EQ(rows[0].id, 1u);
  EXPECT_NEAR(rows[0].centre_m[1], -0.013512, 1e-12);
  EXPECT_NEAR(rows[0].diameter_m, 0.008864, 1e-12);
  EXPECT_TRUE(rows[0].target);
  EXPECT_FALSE(rows[1].target);   // plain hole: not measured
  EXPECT_TRUE(rows[2].target);
  EXPECT_TRUE(rows[2].partial);
}

TEST(MeasurementInput, RivetSummaryEveryRivetIsATarget) {
  std::istringstream in(
      "id,cx_mm,cy_mm,cz_mm,head_diam_mm,head_height_mm,fit_rms_mm,edge_coverage,partial,scanned_share,dist_to_nearest_mm\n"
      "7,-4.28,8.99,64.75,8.81,0.02,0.01,0.95,False,1.00,20.0\n");
  std::vector<SummaryRow> rows;
  std::string error;
  ASSERT_TRUE(parse_summary(in, MeasureKind::RivetFlushness, rows, error)) << error;
  ASSERT_EQ(rows.size(), 1u);
  EXPECT_EQ(rows[0].id, 7u);
  EXPECT_TRUE(rows[0].target);
  EXPECT_NEAR(rows[0].diameter_m, 0.00881, 1e-12);
  EXPECT_EQ(feature_pcd_name(MeasureKind::RivetFlushness, 7), "rivet_07.pcd");
  EXPECT_EQ(feature_pcd_name(MeasureKind::CskDepth, 123), "hole_123.pcd");
}

TEST(MeasurementInput, BadSummariesAreRefusedNotGuessed) {
  std::vector<SummaryRow> rows;
  std::string error;
  std::istringstream missing("id,cx_mm,cy_mm,cz_mm,top_diam_mm,partial\n1,0,0,0,8,False\n");  // no countersunk
  EXPECT_FALSE(parse_summary(missing, MeasureKind::CskDepth, rows, error));
  EXPECT_NE(error.find("countersunk"), std::string::npos);
  std::istringstream bad("id,cx_mm,cy_mm,cz_mm,head_diam_mm,partial\n1,0,x,0,8,False\n");
  EXPECT_FALSE(parse_summary(bad, MeasureKind::RivetFlushness, rows, error));
  std::istringstream twice("id,cx_mm,cy_mm,cz_mm,head_diam_mm,partial\n1,0,0,0,8,False\n1,0,0,0,8,False\n");
  EXPECT_FALSE(parse_summary(twice, MeasureKind::RivetFlushness, rows, error));
  std::istringstream empty("");
  EXPECT_FALSE(parse_summary(empty, MeasureKind::RivetFlushness, rows, error));
  std::istringstream header_only("id,cx_mm,cy_mm,cz_mm,head_diam_mm,partial\n");
  EXPECT_TRUE(parse_summary(header_only, MeasureKind::RivetFlushness, rows, error));  // a scan with no rivet
  EXPECT_TRUE(rows.empty());
}

TEST(MeasurementInput, PcdBinaryRoundTripAndTruncation) {
  const std::string path = ::testing::TempDir() + "measurement_input_test.pcd";
  const float pts[6] = {0.001f, 0.002f, 0.063f, -0.004f, 0.005f, 0.0625f};
  {
    std::ofstream f(path, std::ios::binary);
    f << "# .PCD v0.7\nVERSION 0.7\nFIELDS x y z\nSIZE 4 4 4\nTYPE F F F\nCOUNT 1 1 1\nWIDTH 2\nHEIGHT 1\n"
         "VIEWPOINT 0 0 0 1 0 0 0\nPOINTS 2\nDATA binary\n";
    f.write(reinterpret_cast<const char*>(pts), sizeof pts);
  }
  std::vector<float> xyz;
  std::string error;
  ASSERT_TRUE(read_pcd_xyz(path, xyz, error)) << error;
  ASSERT_EQ(xyz.size(), 6u);
  for (int i = 0; i < 6; ++i) EXPECT_EQ(xyz[i], pts[i]);
  {
    std::ofstream f(path, std::ios::binary);
    f << "VERSION 0.7\nFIELDS x y z\nSIZE 4 4 4\nTYPE F F F\nCOUNT 1 1 1\nPOINTS 3\nDATA binary\n";
    f.write(reinterpret_cast<const char*>(pts), sizeof pts);   // one point short
  }
  EXPECT_FALSE(read_pcd_xyz(path, xyz, error));
  {
    std::ofstream f(path, std::ios::binary);
    f << "VERSION 0.7\nFIELDS x y z rgb\nSIZE 4 4 4 4\nTYPE F F F F\nCOUNT 1 1 1 1\nPOINTS 0\nDATA binary\n";
  }
  EXPECT_FALSE(read_pcd_xyz(path, xyz, error));
  EXPECT_FALSE(read_pcd_xyz(path + ".missing", xyz, error));
  std::remove(path.c_str());
}

TEST(MeasurementInput, LimitsAreInclusive) {
  EXPECT_TRUE(within_limits(0.000947, 0.000947, 0.001147));
  EXPECT_TRUE(within_limits(0.001147, 0.000947, 0.001147));
  EXPECT_FALSE(within_limits(0.0011471, 0.000947, 0.001147));
  EXPECT_TRUE(within_limits(-0.00015, -0.00015, 0.00005));
  EXPECT_FALSE(within_limits(0.00006, -0.00015, 0.00005));
  EXPECT_FALSE(within_limits(std::nan(""), -1, 1));
}

namespace {
// Level IMU (base = IMU), 240 Hz samples, encoder 5.33e-5 m per count.
Extrinsics level_extrinsics() {
  Extrinsics e;
  e.calibrated = true;
  e.metres_per_count = 5.33063582e-05;
  return e;
}
// Rolls at speed_mps for seconds with a forward accelerometer error of accel_error (gravity leak, bias);
// imu_moves: the IMU feels the roll's start (false: roller spun, device at rest). Returns slip changes.
int roll(Odometry& o, double seconds, double speed_mps, double accel_error, int64_t& t, double& count) {
  int changes = 0;
  bool was = o.pose().slipping;
  for (int i = 0; i < static_cast<int>(seconds * 240); ++i) {
    t += 4166667;
    count += speed_mps / 240 / 5.33063582e-05;
    o.encoder(static_cast<int32_t>(std::lround(count)), t);
    o.imu({0, 0, 0.001}, {accel_error, 0, 9.80665}, t);
    changes += o.pose().slipping != was;
    was = o.pose().slipping;
  }
  return changes;
}
}  // namespace

TEST(Odometry, RollerSpunWithoutMotionIsASlip) {
  Odometry o;
  o.configure(level_extrinsics());
  int64_t t = 0;
  double count = 0;
  roll(o, 1.0, 0.0, 0.0, t, count);                    // at rest
  // The roller turns at 50 mm/s within one sample while the IMU feels nothing: not a roll.
  for (int i = 0; i < 24; ++i) {
    t += 4166667;
    count += 0.05 / 240 / 5.33063582e-05;
    o.encoder(static_cast<int32_t>(std::lround(count)), t);
    o.imu({0, 0, 0}, {0, 0, 9.80665}, t);
  }
  EXPECT_TRUE(o.pose().slipping);
  EXPECT_LT(o.pose().distance_m, 0.002);                 // the counts added no distance
}

TEST(Odometry, DriftedSpeedIsReanchoredNotARunaway) {
  // The filter's speed knocked far off the encoder's (an IMU shock: 3 m/s^2 for 0.2 s) while the roll goes on at
  // 20 mm/s and the hand keeps the gyro busy (no rest update). Every encoder sample then looks like slip: before
  // max_slip_s the filter never came back and its speed moved the pose (2026-11-22: km off on 0.5 m rolls).
  auto run = [](double max_slip_s, double& distance) {
    OdometrySettings s;
    s.max_slip_s = max_slip_s;
    Odometry o(s);
    o.configure(level_extrinsics());
    int64_t t = 0;
    double count = 0;
    for (int i = 0; i < 240 * 10; ++i) {
      t += 4166667;
      count += 0.02 / 240 / 5.33063582e-05;
      const double shock = (i >= 240 && i < 288) ? 3.0 : 0.0;
      o.encoder(static_cast<int32_t>(std::lround(count)), t);
      o.imu({0.0, 0.0, 0.2}, {shock, 0, 9.80665}, t);
    }
    distance = o.pose().distance_m;
    return o.pose();
  };
  double d_new = 0, d_old = 0;
  const OdometryPose fixed = run(1.0, d_new);
  EXPECT_GE(fixed.reanchors, 1);
  EXPECT_FALSE(fixed.slipping);
  EXPECT_NEAR(d_new, 0.2, 0.03);                             // 10 s at 20 mm/s
  const OdometryPose old = run(0.0, d_old);
  EXPECT_TRUE(old.slipping);                                 // the failure this guards against
  EXPECT_GT(std::fabs(d_old - 0.2), 1.0);
}
