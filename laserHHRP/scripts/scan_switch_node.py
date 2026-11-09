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

import sys

import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from std_srvs.srv import Trigger

from laser_hhrp.msg import ScanStatus


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
        from gpiozero.pins.lgpio import LGPIOFactory

        def param(name, default):
            return self.declare_parameter(name, default).value

        pins = LGPIOFactory(chip=param("gpio.chip", 4))
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
            log = self.get_logger().info if result.success else self.get_logger().warning
            log(f"{name}: {result.message}")

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
        node.close()
        node.destroy_node()
        rclpy.try_shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
