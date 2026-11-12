// nano_driver_node (layer 1: hardware interface): the Arduino Nano over USB serial.
//
// Publishes (every message carries the Nano time and the Pi time)
//   nano/encoder   laser_hhrp/EncoderSample   240 Hz
//   nano/trigger   laser_hhrp/TriggerRecord   one per laser trigger pulse
//   nano/imu       laser_hhrp/ImuSample       240 Hz (raw, not calibrated)
//   imu/data       sensor_msgs/Imu            the same samples, for RViz / other tools
//   nano/status    laser_hhrp/NanoStatus      2 Hz
//   nano/clock     laser_hhrp/ClockMap        Nano clock -> Pi clock, after each clock exchange
// Subscribes
//   nano/command   laser_hhrp/NanoCommand     start / stop triggering, zero (scan controller)
// The port is reopened every 2 s if the Nano is unplugged.
//
// Threads: a reader thread (SCHED_FIFO nano.rt_priority, when allowed) only moves bytes from the
// port into a queue, so the 4 KB tty buffer never fills (it did when the 1 ms ROS timer ran late
// under load: the FTDI then overran and packets were lost -> false "Nano link lost"). The ROS
// timer parses the queue and publishes, as before.
#include <fcntl.h>
#include <linux/serial.h>
#include <poll.h>
#include <pthread.h>
#include <sched.h>
#include <sys/ioctl.h>
#include <termios.h>
#include <unistd.h>

#include <atomic>
#include <cstring>
#include <deque>
#include <mutex>
#include <thread>

#include <rclcpp/rclcpp.hpp>
#include <sensor_msgs/msg/imu.hpp>

#include "laser_hhrp/clock_sync.hpp"
#include "laser_hhrp/msg/clock_map.hpp"
#include "laser_hhrp/msg/encoder_sample.hpp"
#include "laser_hhrp/msg/imu_sample.hpp"
#include "laser_hhrp/msg/nano_command.hpp"
#include "laser_hhrp/msg/nano_status.hpp"
#include "laser_hhrp/msg/trigger_record.hpp"
#include "laser_hhrp/serial_protocol.hpp"

namespace laser_hhrp {

namespace p = protocol;

/// Opens a serial port in raw mode at `baud` (1000000 for the Nano). -1 on failure.
int open_serial(const std::string& port, int baud) {
  const int fd = ::open(port.c_str(), O_RDWR | O_NOCTTY | O_NONBLOCK);
  if (fd < 0) return -1;
  termios tty{};
  tcgetattr(fd, &tty);
  cfmakeraw(&tty);
  const speed_t speed = baud == 1000000 ? B1000000 : baud == 500000 ? B500000 : B115200;
  cfsetispeed(&tty, speed);
  cfsetospeed(&tty, speed);
  tty.c_cflag |= CLOCAL | CREAD;
  tcsetattr(fd, TCSANOW, &tty);
  // USB-serial adapters (FTDI) hold bytes back up to 16 ms by default: samples come in bursts and
  // the clock mapping jumps. Low latency = 1 ms (no root needed).
  serial_struct ss{};
  if (ioctl(fd, TIOCGSERIAL, &ss) == 0) {
    ss.flags |= ASYNC_LOW_LATENCY;
    ioctl(fd, TIOCSSERIAL, &ss);
  }
  tcflush(fd, TCIOFLUSH);
  return fd;
}

class NanoDriverNode : public rclcpp::Node {
 public:
  // ---- setup: parameters, topics, the 1 kHz poll timer ----
  NanoDriverNode() : Node("nano_driver") {
    port_name_ = declare_parameter("nano.port", "/dev/ttyUSB0");
    baud_ = declare_parameter("nano.baud", 1000000);

    const auto data_qos = rclcpp::QoS(500);
    encoder_pub_ = create_publisher<msg::EncoderSample>("nano/encoder", data_qos);
    trigger_pub_ = create_publisher<msg::TriggerRecord>("nano/trigger", data_qos);
    imu_pub_ = create_publisher<msg::ImuSample>("nano/imu", data_qos);
    ros_imu_pub_ = create_publisher<sensor_msgs::msg::Imu>("imu/data", rclcpp::SensorDataQoS());
    status_pub_ = create_publisher<msg::NanoStatus>("nano/status", 10);
    clock_pub_ = create_publisher<msg::ClockMap>("nano/clock", rclcpp::QoS(10).transient_local());
    command_sub_ = create_subscription<msg::NanoCommand>(
        "nano/command", 10, [this](const msg::NanoCommand& c) { send_command(c); });

    timer_ = create_wall_timer(std::chrono::milliseconds(1), [this] { poll(); });

    reader_thread_ = std::thread([this] { read_loop(); });
    const int priority = static_cast<int>(declare_parameter("nano.rt_priority", 45));
    if (priority > 0) {
      sched_param sp{};
      sp.sched_priority = priority;
      const int err = pthread_setschedparam(reader_thread_.native_handle(), SCHED_FIFO, &sp);
      if (err)
        RCLCPP_WARN(get_logger(), "serial reader at normal priority (SCHED_FIFO %d refused: %s; "
                    "allow it with rtprio in /etc/security/limits.d, then log in again)",
                    priority, std::strerror(err));
      else
        RCLCPP_INFO(get_logger(), "serial reader at SCHED_FIFO %d", priority);
    }
  }

  ~NanoDriverNode() override {
    msg::NanoCommand stop;
    stop.command = msg::NanoCommand::STOP;
    send_command(stop);
    stop_ = true;
    reader_thread_.join();  // the reader closes the port on its way out
  }

 private:
  struct Chunk {
    uint32_t port_open;  // which opening of the port it came from (opens_ at read time)
    int64_t arrival_ns;
    std::vector<uint8_t> bytes;
  };
  static constexpr size_t kMaxQueuedBytes = 1 << 20;  // ~90 s of Nano data

  static int64_t now_ns() {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
               std::chrono::system_clock::now().time_since_epoch()).count();
  }

  // ---- reader thread: port -> queue, nothing else (no ROS calls, no logging) ----
  void read_loop() {
    uint8_t buf[4096];
    int64_t last_open_ns = -10000000000LL;
    while (!stop_) {
      int fd;
      {
        std::lock_guard<std::mutex> lock(fd_mutex_);
        fd = fd_;
      }
      if (fd < 0) {  // (re)open every 2 s
        const int64_t now = now_ns();
        if (now - last_open_ns < 2000000000LL) {
          std::this_thread::sleep_for(std::chrono::milliseconds(20));
          continue;
        }
        last_open_ns = now;
        const int opened = open_serial(port_name_, static_cast<int>(baud_));
        if (opened < 0) {
          ++open_failures_;
          continue;
        }
        std::lock_guard<std::mutex> lock(fd_mutex_);
        fd_ = opened;
        ++opens_;
        continue;
      }
      pollfd pfd{fd, POLLIN, 0};
      if (::poll(&pfd, 1, 5) <= 0) continue;  // timeout (checks stop_) or EINTR
      ssize_t n;
      while ((n = ::read(fd, buf, sizeof(buf))) > 0) enqueue(buf, static_cast<size_t>(n));
      if (n == 0 || (n < 0 && errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR)) {
        std::lock_guard<std::mutex> lock(fd_mutex_);  // unplugged
        ::close(fd_);
        fd_ = -1;
        ++closes_;
      }
    }
    std::lock_guard<std::mutex> lock(fd_mutex_);
    if (fd_ >= 0) ::close(fd_);
    fd_ = -1;
  }

  void enqueue(const uint8_t* data, size_t n) {
    const int64_t arrival = now_ns();
    std::lock_guard<std::mutex> lock(queue_mutex_);
    if (queued_bytes_ + n > kMaxQueuedBytes) {  // the ROS side stopped: drop rather than grow
      dropped_bytes_ += n;
      return;
    }
    // shortcut: allocates per chunk in the RT thread; a preallocated ring if latency ever shows it.
    queue_.push_back({opens_, arrival, std::vector<uint8_t>(data, data + n)});
    queued_bytes_ += n;
  }

  // ---- main loop (ROS timer, every 1 ms) ----
  // 1. log port changes, 2. handle the queued bytes, 3. send a clock request when due,
  // 4. warn if the Nano has been silent for 0.5 s, 5. every 5 s, report lost data.
  void poll() {
    const int64_t now = now_ns();
    const uint32_t opens = opens_, closes = closes_, failures = open_failures_;
    if (closes != seen_closes_) {
      seen_closes_ = closes;
      RCLCPP_ERROR(get_logger(), "%s: Nano disconnected", port_name_.c_str());
      port_open_ = false;
    }
    if (opens != seen_opens_) {
      seen_opens_ = opens;
      RCLCPP_INFO(get_logger(), "%s opened, waiting for the Nano", port_name_.c_str());
      clock_.reset();
      reader_ = p::PacketReader();  // a packet cut by the reopen must not join the new bytes
      last_bad_ = 0;
      last_heard_ns_ = now;
      port_open_ = true;
    }
    if (failures != seen_failures_) {
      seen_failures_ = failures;
      RCLCPP_WARN_THROTTLE(get_logger(), *get_clock(), 10000, "%s: serial port did not open",
                           port_name_.c_str());
    }
    // PacketReader finds 0xAA 0x55, checks the length and the checksum; push() returns true when
    // one packet is complete.
    std::deque<Chunk> chunks;
    {
      std::lock_guard<std::mutex> lock(queue_mutex_);
      chunks.swap(queue_);
      queued_bytes_ = 0;
    }
    for (const Chunk& c : chunks) {
      if (c.port_open != seen_opens_) continue;  // read before a reopen: old connection
      arrival_ns_ = c.arrival_ns;
      for (uint8_t b : c.bytes)
        if (reader_.push(b)) handle_packet();
    }
    if (!port_open_) return;
    sync_clock(now);
    if (now - last_heard_ns_ > 500000000LL)
      RCLCPP_WARN_THROTTLE(get_logger(), *get_clock(), 5000, "Nano silent");
    report_losses(now);
  }

  /// Every 5 s, if anything was lost: bad packets (checksum/length), bytes dropped by the queue,
  /// and FTDI overruns (the chip's buffer overflowed: bytes lost before they reached the Pi).
  void report_losses(int64_t now) {
    if (now - last_loss_report_ns_ < 5000000000LL) return;
    last_loss_report_ns_ = now;
    int overruns = -1;
    {
      std::lock_guard<std::mutex> lock(fd_mutex_);
      serial_icounter_struct ic{};
      if (fd_ >= 0 && ioctl(fd_, TIOCGICOUNT, &ic) == 0) overruns = ic.overrun;
    }
    const uint32_t bad = reader_.errors();
    const uint64_t dropped = dropped_bytes_;
    const bool overran = last_overruns_ >= 0 && overruns > last_overruns_;  // first read = baseline
    if (bad != last_bad_ || dropped != last_dropped_ || overran)
      RCLCPP_WARN(get_logger(), "serial data lost in the last 5 s: %u bad packets, %d FTDI overruns, "
                  "%lu bytes dropped", bad - last_bad_, overran ? overruns - last_overruns_ : 0,
                  static_cast<unsigned long>(dropped - last_dropped_));
    last_bad_ = bad;
    last_dropped_ = dropped;
    if (overruns >= 0) last_overruns_ = overruns;
  }

  /// Writes one whole packet (commands and clock requests, from the ROS thread).
  void write_packet(const std::vector<uint8_t>& bytes) {
    std::lock_guard<std::mutex> lock(fd_mutex_);
    if (fd_ < 0) return;
    size_t done = 0;
    int retries = 0;
    while (done < bytes.size()) {
      const ssize_t w = ::write(fd_, bytes.data() + done, bytes.size() - done);
      if (w > 0) {
        done += static_cast<size_t>(w);
      } else if (w < 0 && errno == EINTR) {
        continue;
      } else if (w < 0 && (errno == EAGAIN || errno == EWOULDBLOCK) && ++retries < 20) {
        std::this_thread::sleep_for(std::chrono::microseconds(200));
      } else {
        RCLCPP_WARN(get_logger(), "write to the Nano failed");
        return;
      }
    }
  }

  // ---- Pi -> Nano ----
  // A NanoCommand from the scan controller becomes one COMMAND packet.
  void send_command(const msg::NanoCommand& c) {
    p::CommandPacket packet{c.command, c.counts_per_trigger, c.imu_period_ms, c.min_trigger_period_us};
    write_packet(p::encode(p::PKT_COMMAND, packet));
  }

  /// Clock exchanges: 10 Hz until the mapping has a few points, then 2 Hz to follow the drift.
  void sync_clock(int64_t now) {
    const int64_t period = clock_.samples() < 8 ? 100000000 : 500000000;
    if (now - last_sync_ns_ < period) return;
    last_sync_ns_ = now;
    p::TimeRequestPacket r{++sync_sequence_, now_ns()};
    clock_.sent(r.sequence, r.host_send_ns);
    write_packet(p::encode(p::PKT_TIME_REQUEST, r));
  }

  /// Pi time of a Nano time (arrival time until the first clock exchange).
  rclcpp::Time pi_time(int64_t device_ns) const {
    return rclcpp::Time(clock_.valid() ? clock_.to_host(device_ns) : arrival_ns_);
  }

  // ---- Nano -> Pi: one complete packet ----
  // Every packet carries the Nano time in us. We keep it (device_time_ns, used for pairing and
  // IMU interpolation) and add the Pi time (header.stamp) through the clock mapping.
  void handle_packet() {
    last_heard_ns_ = arrival_ns_;
    switch (reader_.type()) {
      case p::PKT_ENCODER: {
        p::EncoderPacket e;
        if (!reader_.payload(e)) return;
        msg::EncoderSample m;
        m.device_time_ns = static_cast<int64_t>(e.time_us) * 1000;
        m.header.stamp = pi_time(m.device_time_ns);
        m.count = e.count;
        m.raw = e.raw;
        m.flags = e.flags;
        encoder_pub_->publish(m);
        break;
      }
      case p::PKT_TRIGGER: {
        p::TriggerPacket t;
        if (!reader_.payload(t)) return;
        msg::TriggerRecord m;
        m.device_time_ns = static_cast<int64_t>(t.time_us) * 1000;
        m.header.stamp = pi_time(m.device_time_ns);
        m.index = t.index;
        m.count = t.count;
        m.direction = t.direction;
        m.flags = t.flags;
        trigger_pub_->publish(m);
        break;
      }
      case p::PKT_IMU: {
        p::ImuPacket i;
        if (!reader_.payload(i)) return;
        msg::ImuSample m;
        m.device_time_ns = static_cast<int64_t>(i.time_us) * 1000;
        m.header.stamp = pi_time(m.device_time_ns);
        m.header.frame_id = "imu";
        // Raw MPU6050 counts -> SI: accel x 9.80665/16384 (m/s^2), gyro x (pi/180)/131 (rad/s).
        for (int k = 0; k < 3; ++k) {
          m.accel_mps2[k] = static_cast<float>(i.accel[k] * p::ACCEL_MPS2_PER_COUNT);
          m.gyro_radps[k] = static_cast<float>(i.gyro[k] * p::GYRO_RADPS_PER_COUNT);
        }
        imu_pub_->publish(m);
        sensor_msgs::msg::Imu r;
        r.header = m.header;
        r.orientation_covariance[0] = -1;  // no orientation
        r.linear_acceleration.x = m.accel_mps2[0];
        r.linear_acceleration.y = m.accel_mps2[1];
        r.linear_acceleration.z = m.accel_mps2[2];
        r.angular_velocity.x = m.gyro_radps[0];
        r.angular_velocity.y = m.gyro_radps[1];
        r.angular_velocity.z = m.gyro_radps[2];
        ros_imu_pub_->publish(r);
        break;
      }
      case p::PKT_STATUS: {
        p::StatusPacket s;
        if (!reader_.payload(s)) return;
        msg::NanoStatus m;
        m.header.stamp = pi_time(static_cast<int64_t>(s.time_us) * 1000);
        m.encoder_ok = s.encoder_ok;
        m.imu_ok = s.imu_ok;
        m.triggering = s.triggering;
        m.triggers = s.triggers;
        m.overspeed = s.overspeed;
        status_pub_->publish(m);
        break;
      }
      // Clock exchange answer: add the point to the Nano -> Pi fit (clock_sync.hpp) and publish
      // the new mapping for the other nodes: pi = host_ns + (nano - device_ns) * (1 + drift).
      case p::PKT_TIME_REPLY: {
        p::TimeReplyPacket r;
        if (!reader_.payload(r)) return;
        const int64_t receive_ns = static_cast<int64_t>(r.receive_us) * 1000;
        const int64_t send_ns = static_cast<int64_t>(r.send_us) * 1000;
        if (!clock_.reply(r.sequence, r.host_send_ns, receive_ns, send_ns, arrival_ns_) ||
            !clock_.valid())
          return;
        msg::ClockMap c;
        c.header.stamp = now();
        c.device_ns = send_ns;
        c.host_ns = clock_.to_host(send_ns);
        c.drift = clock_.drift();
        clock_pub_->publish(c);
        break;
      }
      default:
        break;
    }
  }

  std::string port_name_;
  int64_t baud_ = 1000000;
  std::mutex fd_mutex_;  // fd_: opened/closed by the reader, written by the ROS thread
  int fd_ = -1;
  p::PacketReader reader_;
  ClockSync clock_;
  int64_t last_heard_ns_ = 0, last_sync_ns_ = 0, last_loss_report_ns_ = 0;
  int64_t arrival_ns_ = 0;  // Pi arrival time of the bytes being parsed
  std::thread reader_thread_;
  std::atomic<bool> stop_{false};
  std::atomic<uint32_t> opens_{0}, closes_{0}, open_failures_{0};
  uint32_t seen_opens_ = 0, seen_closes_ = 0, seen_failures_ = 0;
  bool port_open_ = false;
  std::mutex queue_mutex_;
  std::deque<Chunk> queue_;
  size_t queued_bytes_ = 0;
  std::atomic<uint64_t> dropped_bytes_{0};
  uint32_t last_bad_ = 0;
  uint64_t last_dropped_ = 0;
  int last_overruns_ = -1;
  uint32_t sync_sequence_ = 0;
  rclcpp::Publisher<msg::EncoderSample>::SharedPtr encoder_pub_;
  rclcpp::Publisher<msg::TriggerRecord>::SharedPtr trigger_pub_;
  rclcpp::Publisher<msg::ImuSample>::SharedPtr imu_pub_;
  rclcpp::Publisher<sensor_msgs::msg::Imu>::SharedPtr ros_imu_pub_;
  rclcpp::Publisher<msg::NanoStatus>::SharedPtr status_pub_;
  rclcpp::Publisher<msg::ClockMap>::SharedPtr clock_pub_;
  rclcpp::Subscription<msg::NanoCommand>::SharedPtr command_sub_;
  rclcpp::TimerBase::SharedPtr timer_;
};

}  // namespace laser_hhrp

int main(int argc, char** argv) {
  rclcpp::init(argc, argv);
  rclcpp::spin(std::make_shared<laser_hhrp::NanoDriverNode>());
  rclcpp::shutdown();
  return 0;
}
