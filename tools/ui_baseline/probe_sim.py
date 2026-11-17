#!/usr/bin/env python3
"""Operator UI Phase 0 baseline probe (simulation): calibrate, run scans, log status transitions, frames and
inspection results relative to the stop call, so late (post-release) detections become visible.

Usage: probe.py SCAN_S LISTEN_S [QUICK_S]
  scan 1: SCAN_S seconds, then listen LISTEN_S
  scan 2: SCAN_S seconds, stop, immediately scan 3 for QUICK_S (rapid re-press), stop, listen LISTEN_S
"""
import json
import sys
import time

import rclpy
from std_srvs.srv import Trigger

from laser_hhrp.msg import InspectionResult, ScanFrame, ScanStatus, StorageStatus

STATES = {0: "IDLE", 1: "SCANNING", 2: "PAUSED", 3: "ERROR"}
T0 = time.monotonic()


def t():
    return f"{time.monotonic() - T0:7.2f}"


def main():
    scan_s, listen_s = float(sys.argv[1]), float(sys.argv[2])
    quick_s = float(sys.argv[3]) if len(sys.argv) > 3 else 0.15
    rclpy.init()
    node = rclpy.create_node("phase0_probe")
    st = {"state": None, "cal": None, "frames": 0, "first": None, "last": None, "dir": ""}

    def on_status(m):
        if m.state != st["state"]:
            print(f"{t()} STATUS {STATES.get(st['state'])} -> {STATES[m.state]} "
                  f"stamp={m.header.stamp.sec}.{m.header.stamp.nanosec:09d} msg='{m.message}'", flush=True)
            st["state"] = m.state
        st["cal"] = m.calibration

    def on_frame(m):
        if m.index == 0:
            if st["frames"]:
                print(f"{t()} FRAMES previous scan: {st['frames']} frames, idx {st['first']}..{st['last']}", flush=True)
            st["frames"], st["first"] = 0, 0
        st["frames"] += 1
        st["last"] = m.index

    def on_result(m):
        d = json.loads(m.detail or "{}")
        print(f"{t()} RESULT {m.feature_type} id={d.get('id')} partial={d.get('partial')} "
              f"pos_mm=({m.position_m[0]*1e3:.1f},{m.position_m[1]*1e3:.1f}) "
              f"csk={m.countersink_depth_m} flush={m.flushness_m} passed={m.passed} state={STATES.get(st['state'])}",
              flush=True)

    def on_storage(m):
        if m.session_dir != st["dir"]:
            print(f"{t()} STORAGE recording={m.recording} dir={m.session_dir}", flush=True)
            st["dir"] = m.session_dir

    node.create_subscription(ScanStatus, "scan/status", on_status, 10)
    node.create_subscription(ScanFrame, "scan/frame", on_frame, 100)
    node.create_subscription(InspectionResult, "inspection/result", on_result, 50)
    node.create_subscription(StorageStatus, "scan/storage", on_storage, 10)
    clients = {n: node.create_client(Trigger, n) for n in ("calibration/start", "scan/start", "scan/stop")}

    def spin(seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            rclpy.spin_once(node, timeout_sec=0.01)

    def call(name):
        c = clients[name]
        if not c.wait_for_service(timeout_sec=20):
            print(f"{t()} SERVICE {name} unavailable", flush=True)
            return False
        fut = c.call_async(Trigger.Request())
        rclpy.spin_until_future_complete(node, fut, timeout_sec=10)
        r = fut.result()
        print(f"{t()} CALL {name}: success={r.success} '{r.message}'", flush=True)
        return r.success

    spin(3)
    call("scan/start")  # expected: refused before calibration
    call("calibration/start")
    end = time.monotonic() + 30
    while st["cal"] != 2 and time.monotonic() < end:
        spin(0.2)
    print(f"{t()} calibration state={st['cal']}", flush=True)
    spin(1)

    print(f"{t()} === SCAN 1 ({scan_s}s) ===", flush=True)
    call("scan/start"); spin(scan_s); call("scan/stop")
    print(f"{t()} --- released; listening {listen_s}s ---", flush=True)
    spin(listen_s)
    print(f"{t()} FRAMES scan so far: {st['frames']} frames, idx {st['first']}..{st['last']}", flush=True)

    print(f"{t()} === SCAN 2 ({scan_s}s) then rapid SCAN 3 ({quick_s}s) ===", flush=True)
    call("scan/start"); spin(scan_s); call("scan/stop")
    call("scan/start"); spin(quick_s); call("scan/stop")
    print(f"{t()} --- released; listening {listen_s}s ---", flush=True)
    spin(listen_s)
    print(f"{t()} FRAMES scan so far: {st['frames']} frames, idx {st['first']}..{st['last']}", flush=True)
    node.destroy_node()
    rclpy.shutdown()


if __name__ == "__main__":
    main()
