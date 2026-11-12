# laser_driver_node

Layer 1 (hardware interface). Connects to the Micro-Epsilon scanCONTROL 29xx through the
scanCONTROL SDK, configures its trigger (external In1 for hardware sync, internal free running for
software sync), and publishes every profile stamped with the Pi time of its exposure, computed
from the scanner's own shutter clock. It also publishes a thinned live view for RViz and the
connection state. Timing contract: [synchronization.md](synchronization.md).

Source: `src/laser_driver_node.cpp`, `src/scancontrol.cpp`, `include/laser_hhrp/scancontrol.hpp`,
`include/laser_hhrp/scanner_clock.hpp`, `include/laser_hhrp/profile_cloud.hpp`.

## Interfaces

| Kind | Name | Type / default | Notes |
| --- | --- | --- | --- |
| pub | `laser/raw` | `laser_hhrp/RawProfile` | every profile, QoS depth 200 |
| pub | `laser/profile` | `sensor_msgs/PointCloud2` | every `laser.live_view_every`-th profile, sensor-data QoS |
| pub | `laser/status` | `laser_hhrp/LaserStatus` | 1 Hz: connected + last message |
| param | `laser.interface` | `""` | scanner IP / SDK name; empty = first found |
| param | `laser.config_file` | `""` (hhrp.yaml: `test.sc1`) | scanCONTROL Configuration Tools file loaded at every connect, before the settings below; relative = `share/laser_hhrp/config/`, `~/` = home; a missing file stops the node |
| param | `laser.config_ignore_calibration` | `true` | keep the scanner's own calibration when loading the file |
| param | `laser.resolution` | `0` | 0 = keep the sensor's (the file's); else the highest supported value ≤ this |
| param | `laser.exposure_time_us` | `0` | 0 = keep the sensor's (the file's); else register in units of 10 µs |
| param | `laser.exposure_automatic` | `false` | with `exposure_time_us` > 0: scanner's automatic exposure (refused in software sync) |
| param | `laser.idle_time_us` | `0` | 0 = automatic (software sync: `1e6/rate − exposure`; hardware sync: 900) |
| param | `laser.inputs_24v` | `false` | input level: false = TTL 5 V |
| param | `laser.input_pulldown` | `true` | pull-down on the inputs |
| param | `laser.digital_in_mode` | `2` | I/O register bits 4..7; 2 = In1 trigger input only |
| param | `laser.trigger_mode` | `0` | trigger register bits 16..19 (hardware sync); 0 = edge |
| param | `laser.trigger_input_digital` | `true` | hardware sync: trigger from digital input In1 |
| param | `laser.trigger_polarity_high` | `true` | hardware sync: rising edge |
| param | `laser.queue_size` | `256` | profiles buffered between the SDK thread and the node |
| param | `sync.hardware_trigger` | `false` | true = external trigger on In1 |
| param | `sync.software_rate_hz` | `120.0` | only declared when `sync.hardware_trigger` is false |
| param | `laser.live_view_every` | `10` | live-view thinning |

## Processing

```
startup:
    read parameters into ScanControlSettings
    if software sync: expected_hz = max(1, software_rate_hz)
    connect()
    timers: 1 ms -> poll(), 1 s -> publish_status()

connect():
    scanner_clock.reset()                      (a reconnected scanner may restart its clock)
    ScanControl::connect(settings):
        find interface (or first discovered) -> SetDeviceInterface -> Connect -> check type is 29xx
        transfer off; ImportLLTConfig(config_file, ignore_calibration = true)   (failure = connect failure)
        resolution = the sensor's (0) or highest supported <= requested
        exposure: kept (0) or set (value/10, automatic off); then read back
        idle = laser.idle_time_us, or 0 = automatic: software sync max(10, 1e6/expected_hz − exposure)
               (period = exposure + idle); hardware sync 900 µs (exposure + idle must fit the shortest trigger period)
        read-modify-write registers (only our bit fields change):
            idle (value/10),
            IO: TTL 5 V (or 24 V), pull-down, digital-in mode (In1 = trigger input)
            trigger: external -> ext-active | mode | input | polarity;  internal -> clear ext-active
        partial profile: x and z of stripe 0 (8 bytes / point)
        register profile + control-lost callbacks, start transfer
    success: log + laser/status "connected; setup test.sc1 loaded; exposure … us, idle … us, resolution …"
    on failure: keep the message, warn (throttled 10 s)

SDK thread, on_profile(data):           stamped with arrival = system time first
    drop if size != expected (counter gap shows the loss)
    if queue full (256): drop oldest, lost++
    push {bytes, arrival}

poll():                                  every 1 ms
    if not connected: retry connect() every 2 s, return
    while ScanControl::read(profile):
        convert x, z (mm -> m), valid = (z != 0); last 16 bytes = timestamp
            -> shutter_open, shutter_close, profile_counter; counter gaps -> lost
        stamp = scanner_clock.map(shutter_open, shutter_close, arrival)
        publish RawProfile; every Nth also as PointCloud2 (x, 0, z; invalid dropped)
        count for the rate report
    if read reported an error (control lost / conversion): log, disconnect   (retried after 2 s)
    report_rate()
```

### Formulas

- Exposure midpoint in scanner time: `mid = (open + close)/2` (or `open` if close wrapped);
  wraps of the 128 s clock are counted: `scanner_ns = mid + wraps·128 s`.
- Scanner → Pi (`ScannerClock::map`): `offset_i = arrival_i − scanner_ns_i`; keep the offsets of the
  last 2 s; `stamp = scanner_ns + min(offset)`. A jump back > 2 s (scanner restart) or an offset
  2 s below the minimum resets the window.
- Rate report every 5 s: `received/s = n/T`; `made/s = (counter_span + 1)/T` with
  `counter_span = (last − first) & 0xFFFFFF`; error if software sync and `made < 0.8·expected_hz`
  (a 2900 asked for more than ~140 Hz falls to ~18/s).

## Edge cases

- Built without the SDK: `connect()` always fails with "built without the scanCONTROL SDK"; the
  node keeps running and publishes `connected: false`.
- Scanner unplugged / control lost: disconnect, retry every 2 s.
- Hardware sync with no pulses: "no profiles in 5 s" warning.
- scan_controller_node treats `laser/status` older than 3 s or `connected: false` as a fatal fault.

## Where in the code

`LaserDriverNode::connect/poll/count_rate/report_rate/publish_status`;
`ScanControl::connect/read/disconnect`, `Impl::on_profile/on_control_lost`, `merge()`;
`ScannerClock::map/min_offset`; `to_cloud()`.
