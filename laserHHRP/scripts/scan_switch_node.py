#!/usr/bin/env python3
"""Scan switch and status LEDs on the Pi 5 GPIO header.

    Button: GPIO25 (pin 22) -> GND (pin 20), internal pull-up, no resistor.
            press = scan/start, release = scan/stop.
    LEDs (GPIO -> 220 ohm -> LED -> GND) follow scan/status, not the button, so a refused start
    (not calibrated, devices not ready) leaves yellow on:
        yellow  not scanning
        green   scanning
        red     blinks while scanning faster than speed.warning_fraction x the maximum speed

Started by hhrp.launch.py; scan_switch:=false on a machine without the GPIO header.
    python3 scan_switch_node.py --self-test     # checks the LED logic, no GPIO or ROS graph needed
"""

from __future__ import annotations

import glob
import signal
import sys

import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from std_srvs.srv import Trigger

from laser_hhrp.msg import ScanStatus


HEADER_LABEL = "pinctrl-rp1"  # the Pi 5 40-pin header; its /dev/gpiochipN number depends on the kernel (4, 15, ...)


def header_chip(chips) -> int | None:
    """Lowest chip number labelled as the Pi 5 header, from (number, label) pairs; None if there is none."""
    return min((number for number, label in chips if label == HEADER_LABEL), default=None)


def readable_chips() -> list[tuple[int, str]]:
    """(number, label) of each /dev/gpiochipN this user can open; root-only chips are skipped."""
    import lgpio
    chips = []
    for path in glob.glob("/dev/gpiochip*"):
        number = path[len("/dev/gpiochip"):]
        if not number.isdigit():
            continue
        try:
            handle = lgpio.gpiochip_open(int(number))
        except lgpio.error:
            continue
        try:
            chips.append((int(number), lgpio.gpio_get_chip_info(handle)[3]))
        finally:
            lgpio.gpiochip_close(handle)
    return sorted(chips)


def leds_for(scanning: bool, speed_mps: float, max_speed_mps: float,
             warning_fraction: float) -> tuple[bool, bool, bool]:
    """(green, yellow, red blinking) for the scan state."""
    too_fast = scanning and max_speed_mps > 0 and abs(speed_mps) > warning_fraction * max_speed_mps
    return scanning, not scanning, too_fast


class ScanSwitch(Node):
    STABLE_POLLS = 3  # debounce: the button must read the same 3 polls in a row (60 ms)

    def __init__(self) -> None:
        super().__init__("scan_switch")
        from gpiozero import LED, Button
        import lgpio
        from gpiozero.pins.lgpio import LGPIOFactory, LGPIOPin
        from gpiozero.pins.local import LocalPiFactory

        def param(name, default):
            return self.declare_parameter(name, default).value

        chip = param("gpio.chip", -1)
        if chip < 0:
            chips = readable_chips()
            chip = header_chip(chips)
            if chip is None:
                raise RuntimeError(f"no GPIO chip labelled {HEADER_LABEL} among the readable ones "
                                   f"{chips}; set gpio.chip in hhrp.yaml")
        self.get_logger().info(f"GPIO header: /dev/gpiochip{chip}")

        class ChipFactory(LGPIOFactory):
            # gpiozero 2.0.1's LGPIOFactory ignores its chip argument and opens chip 4 on a Pi 5,
            # which does not exist under the RT kernel; this is its __init__ with the chip passed in.
            # shortcut: copies gpiozero internals, recheck when python3-gpiozero is upgraded.
            def __init__(self, chip):
                LocalPiFactory.__init__(self)
                self._handle = lgpio.gpiochip_open(chip)
                self._chip = chip
                self.pin_class = LGPIOPin

        pins = ChipFactory(chip)  # pins are BCM numbers (RP1 line offsets)
        self.green = LED(param("gpio.green", 22), pin_factory=pins)
        self.yellow = LED(param("gpio.yellow", 27), pin_factory=pins)
        self.red = LED(param("gpio.red", 17), pin_factory=pins)
        self.button = Button(param("gpio.switch", 25), pull_up=True, pin_factory=pins)
        self.warning_fraction = param("speed.warning_fraction", 0.8)

        self.pressed = False
        self.last_raw = False
        self.stable = 0
        self.red_blinking = False
        self.triggers = {name: self.create_client(Trigger, name) for name in ("scan/start", "scan/stop")}
        self.create_subscription(ScanStatus, "scan/status", self.on_status, 10)
        # The button is polled in a ROS timer (not gpiozero callbacks) so service calls stay on
        # the executor thread.
        self.create_timer(0.02, self.poll_button)
        self.show(False, True, False)

    def poll_button(self) -> None:
        raw = self.button.is_pressed
        self.stable = self.stable + 1 if raw == self.last_raw else 0
        self.last_raw = raw
        if self.stable < self.STABLE_POLLS or raw == self.pressed:
            return
        name = "scan/start" if raw else "scan/stop"
        if not self.triggers[name].service_is_ready():
            # Retried on the next poll while the button stays put (e.g. pressed during startup).
            self.get_logger().warning(f"{name}: waiting for scan_controller_node", throttle_duration_sec=2.0)
            return
        self.pressed = raw
        self.call(name)

    def call(self, name: str) -> None:
        def done(future) -> None:
            result = future.result()
            # Two call sites: rclpy refuses one call site logging at two severities (ValueError).
            if result.success:
                self.get_logger().info(f"{name}: {result.message}")
            else:
                self.get_logger().warning(f"{name}: {result.message}")

        self.triggers[name].call_async(Trigger.Request()).add_done_callback(done)

    def on_status(self, s: ScanStatus) -> None:
        self.show(*leds_for(s.state == ScanStatus.SCANNING, s.speed_mps, s.max_speed_mps,
                            self.warning_fraction))

    def show(self, green: bool, yellow: bool, red_blinking: bool) -> None:
        self.green.value = green
        self.yellow.value = yellow
        if red_blinking != self.red_blinking:  # restarting blink() every status would reset its phase
            if red_blinking:
                self.red.blink(on_time=0.25, off_time=0.25)
            else:
                self.red.off()
            self.red_blinking = red_blinking

    def close(self) -> None:
        for device in (self.green, self.yellow, self.red, self.button):
            device.close()  # LEDs off, pins released


def self_test() -> int:
    assert leds_for(False, 0.0, 0.011, 0.8) == (False, True, False)    # idle: yellow
    assert leds_for(True, 0.005, 0.011, 0.8) == (True, False, False)   # scanning: green
    assert leds_for(True, 0.010, 0.011, 0.8) == (True, False, True)    # too fast: green + red
    assert leds_for(True, -0.010, 0.011, 0.8) == (True, False, True)   # backwards counts too
    assert leds_for(False, 0.010, 0.011, 0.8) == (False, True, False)  # stopped: no red
    assert leds_for(True, 0.010, 0.0, 0.8) == (True, False, False)     # no maximum known yet
    pi5_rt = [(11, "gpio-brcmstb@107d517c00"), (15, "pinctrl-rp1"), (16, "ftdi-cbus")]
    assert header_chip(pi5_rt) == 15                                    # new Pi, RT kernel
    assert header_chip([(0, "gpio-brcmstb@107d508500"), (4, "pinctrl-rp1")]) == 4  # old Pi
    assert header_chip([(0, "pinctrl-bcm2711")]) is None               # not a Pi 5
    print("scan_switch_node self-test: OK")
    return 0


def main() -> int:
    if "--self-test" in sys.argv:
        return self_test()
    rclpy.init()
    node = ScanSwitch()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        # ros2 launch forwards a second SIGINT; it must not cut the LED/pin cleanup short.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        node.close()
        node.destroy_node()
        rclpy.try_shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
