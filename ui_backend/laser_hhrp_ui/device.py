"""Device actions the operator takes from the screens: gyro calibration and turning the device off.

Deviation from plan section 3 (the backend only observes ROS; no device control from the web), approved on
2026-11-21 so the device runs without a terminal. Scan start / stop stays on the physical switch only. The
Engine decides whether an action is allowed (never during a scan, never while an analysis is pending); the
classes here only carry it out. Both methods block and run on a worker thread, never on the event loop.
"""

from __future__ import annotations

import logging
import subprocess
import threading

log = logging.getLogger("laser_hhrp_ui.device")
CALIBRATION_SERVICE = "calibration/start"


class NoDevice:
    """Neither action available (no ROS, or API-only runs)."""
    can_calibrate = False
    can_power_off = False

    def calibrate(self) -> tuple[bool, str]:
        return False, "Calibration is not available here"

    def power_off(self) -> tuple[bool, str]:
        return False, "Turning off is not enabled on this device"


def run_power_off(command: list[str] | None) -> tuple[bool, str]:
    """The configured command (e.g. `systemctl poweroff`). It returns once the shutdown is queued; the
    system then stops the backend, the core and the detector with SIGTERM."""
    if not command:
        return NoDevice().power_off()
    try:
        p = subprocess.run(command, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"Could not turn off: {e}"
    if p.returncode != 0:
        detail = (p.stderr or p.stdout).strip().splitlines()
        return False, "Could not turn off: " + (detail[-1] if detail else f"exit code {p.returncode}")
    return True, "Turning off"


class RosDevice:
    """Gyro calibration through calibration_node's `calibration/start` (std_srvs/Trigger), called on the ROS
    bridge's node (its executor thread delivers the reply); power off through `power_off_command`."""
    can_calibrate = True

    def __init__(self, node_getter, power_off_command: list[str] | None = None, timeout_s: float = 5.0):
        self.node_getter = node_getter
        self.power_off_command = list(power_off_command) if power_off_command else None
        self.timeout_s = timeout_s
        self._client = None
        self._lock = threading.Lock()

    @property
    def can_power_off(self) -> bool:
        return bool(self.power_off_command)

    def calibrate(self) -> tuple[bool, str]:
        node = self.node_getter()
        if node is None:
            return False, "ROS is not running"
        from std_srvs.srv import Trigger
        with self._lock:
            if self._client is None:
                self._client = node.create_client(Trigger, CALIBRATION_SERVICE)
            client = self._client
        if not client.wait_for_service(timeout_sec=2.0):
            return False, "Calibration is not running (calibration_node)"
        done = threading.Event()
        future = client.call_async(Trigger.Request())
        future.add_done_callback(lambda _f: done.set())
        if not done.wait(self.timeout_s):
            future.cancel()
            return False, "Calibration did not answer"
        reply = future.result()
        return bool(reply.success), str(reply.message)

    def power_off(self) -> tuple[bool, str]:
        return run_power_off(self.power_off_command)
