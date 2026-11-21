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
    can_reboot = False

    def calibrate(self) -> tuple[bool, str]:
        return False, "Calibration is not available here"

    def power_off(self) -> tuple[bool, str]:
        return False, "Turning off is not enabled on this device"

    def reboot(self) -> tuple[bool, str]:
        return False, "Restarting is not enabled on this device"


def run_power_off(command: list[str] | None, what: str = "turn off") -> tuple[bool, str]:
    """The configured command (e.g. `systemctl poweroff`). It returns once the shutdown is queued; the
    system then stops the backend, the core and the detector with SIGTERM."""
    if not command:
        return NoDevice().power_off() if what == "turn off" else NoDevice().reboot()
    try:
        p = subprocess.run(command, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"Could not {what}: {e}"
    if p.returncode != 0:
        detail = (p.stderr or p.stdout).strip().splitlines()
        return False, f"Could not {what}: " + (detail[-1] if detail else f"exit code {p.returncode}")
    return True, "Turning off" if what == "turn off" else "Restarting"


class RosDevice:
    """Gyro calibration through calibration_node's `calibration/start` (std_srvs/Trigger), called on the ROS
    bridge's node (its executor thread delivers the reply); power off through `power_off_command`."""
    can_calibrate = True

    def __init__(self, node_getter, power_off_command: list[str] | None = None, timeout_s: float = 5.0,
                 reboot_command: list[str] | None = None):
        self.node_getter = node_getter
        self.power_off_command = list(power_off_command) if power_off_command else None
        self.reboot_command = list(reboot_command) if reboot_command else None
        self.timeout_s = timeout_s
        self._client = None
        self._lock = threading.Lock()

    @property
    def can_power_off(self) -> bool:
        return bool(self.power_off_command)

    @property
    def can_reboot(self) -> bool:
        return bool(self.reboot_command)

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

    def reboot(self) -> tuple[bool, str]:
        return run_power_off(self.reboot_command, "restart")


# ---- service menu (developers): PIN, network information, kiosk requests ----
class ServiceError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class ServiceAccess:
    """The service menu's PIN: 4-8 digits in a file (read at every check, so it can be changed over SSH).
    After `max_tries` wrong PINs in a row nothing is accepted for `lock_s`."""

    def __init__(self, pin_file, max_tries: int = 5, lock_s: float = 60.0, monotonic=None):
        import time as _time
        self.pin_file = pin_file
        self.max_tries, self.lock_s = max_tries, lock_s
        self.monotonic = monotonic or _time.monotonic
        self.wrong, self.locked_until = 0, 0.0
        self._lock = threading.Lock()

    def _pin(self) -> str | None:
        try:
            with open(self.pin_file, encoding="utf-8") as f:
                pin = f.read().strip()
        except OSError:
            return None
        return pin if pin.isdigit() and 4 <= len(pin) <= 8 else None

    @property
    def available(self) -> bool:
        return self._pin() is not None

    def check(self, pin) -> None:
        import hmac
        with self._lock:
            now = self.monotonic()
            if now < self.locked_until:
                raise ServiceError(429, "locked", f"Too many wrong PINs: wait {self.locked_until - now:.0f} s")
            expected = self._pin()
            if expected is None:
                raise ServiceError(409, "unavailable", "The service menu is not set up on this device")
            if not isinstance(pin, str) or not hmac.compare_digest(pin.encode(), expected.encode()):
                self.wrong += 1
                if self.wrong >= self.max_tries:
                    self.wrong, self.locked_until = 0, now + self.lock_s
                raise ServiceError(403, "wrong_pin", "Wrong PIN")
            self.wrong = 0


class KioskControl:
    """Requests to the kiosk session (deploy/kiosk/kiosk-session.sh), which watches `<dir>/request`:
    `desktop` stops the scanner software and opens the Ubuntu desktop until logout; `restart` restarts the
    core and the backend."""
    KINDS = ("desktop", "restart")

    def __init__(self, directory):
        from pathlib import Path
        self.dir = Path(directory)

    def request(self, kind: str) -> tuple[bool, str]:
        import os
        if kind not in self.KINDS:
            return False, f"unknown request {kind}"
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self.dir / "request.tmp"
            tmp.write_text(kind + "\n")
            os.replace(tmp, self.dir / "request")
        except OSError as e:
            return False, f"Could not ask the kiosk session: {e.strerror or e}"
        return True, kind


def network_info() -> list[dict]:
    """Network devices with state, connection (Wi-Fi name) and IPv4 addresses, for the service menu."""
    devices = []
    try:
        out = subprocess.run(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status"],
                             capture_output=True, text=True, timeout=5).stdout
        for line in out.splitlines():
            parts = line.replace("\\:", "\0").split(":")
            if len(parts) != 4:
                continue
            dev, kind, state, conn = (p.replace("\0", ":") for p in parts)
            if kind in ("wifi", "ethernet"):
                devices.append({"device": dev, "type": kind, "state": state, "connection": conn, "ipv4": []})
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show"], capture_output=True, text=True, timeout=5).stdout
        for line in out.splitlines():
            f = line.split()
            if len(f) >= 4 and f[2] == "inet":
                d = next((x for x in devices if x["device"] == f[1]), None)
                if d is None and f[1] != "lo":
                    d = {"device": f[1], "type": "", "state": "", "connection": "", "ipv4": []}
                    devices.append(d)
                if d is not None:
                    d["ipv4"].append(f[3].split("/")[0])
    except (OSError, subprocess.TimeoutExpired):
        pass
    return devices


class Service:
    """What the service menu needs: the PIN, the kiosk session and the network view."""

    def __init__(self, access: ServiceAccess, control: KioskControl, network=network_info):
        self.access, self.control, self.network = access, control, network

    @property
    def available(self) -> bool:
        return self.access.available
