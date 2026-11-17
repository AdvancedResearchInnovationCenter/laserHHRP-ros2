#!/usr/bin/env python3
"""Phase 2 live check against the simulated core and the real backend + detectors.

Run by phase2_ros_check.sh, which starts the core (simulate:=true holes:=false ui:=false) and the backend
(`ros2 run laser_hhrp_ui server`) on an isolated ROS domain. This script plays the operator over HTTP,
plays the physical switch by calling the same scan/start and scan/stop services scan_switch_node calls,
and checks the ROS graph and processes. Each check prints PASS or FAIL; exit code = number of FAILs.

The simulator produces no detections (its rivet is 20 um proud), so the result path is exercised with a
few synthetic InspectionResult messages from this script's own node; they are reported as such.
"""

import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request

import rclpy
from std_srvs.srv import Trigger

from laser_hhrp.msg import InspectionResult, ScanFrame

API = os.environ.get("UI_API", "http://127.0.0.1:8097/api/v1")
DATA = os.environ["UI_DATA"]
FAILS = []


def check(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}{('  -- ' + detail) if detail else ''}", flush=True)
    if not ok:
        FAILS.append(name)
    return ok


def http(method, path, body=None):
    req = urllib.request.Request(API + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")
    except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
        return 0, {"error": str(e)}                      # backend not (yet) reachable


def state():
    return http("GET", "/state")[1]


def wait(pred, timeout, period=0.2):
    end = time.monotonic() + timeout
    last = None
    while time.monotonic() < end:
        last = pred()
        if last:
            return last
        time.sleep(period)
    return last


class Probe:
    def __init__(self):
        rclpy.init()
        self.node = rclpy.create_node("phase2_probe")
        self.frames, self.lock = [], threading.Lock()
        self.node.create_subscription(ScanFrame, "scan/frame", self._frame, 200)
        self.pub = self.node.create_publisher(InspectionResult, "inspection/result", 10)
        self.clients = {n: self.node.create_client(Trigger, n) for n in ("calibration/start", "scan/start", "scan/stop")}
        self.thread = threading.Thread(target=rclpy.spin, args=(self.node,), daemon=True)
        self.thread.start()

    def _frame(self, m):
        with self.lock:
            self.frames.append(time.monotonic())

    def call(self, name):
        c = self.clients[name]
        if not c.wait_for_service(timeout_sec=20):
            return False, "service unavailable"
        fut = c.call_async(Trigger.Request())
        end = time.monotonic() + 10
        while not fut.done() and time.monotonic() < end:
            time.sleep(0.02)
        r = fut.result()
        return (r.success, r.message) if r else (False, "no reply")

    def nodes(self, name):
        return sum(1 for n, _ in self.node.get_node_names_and_namespaces() if n == name)

    def topics(self):
        return {t for t, _ in self.node.get_topic_names_and_types()}

    def publish_rivet(self, fid, y):
        m = InspectionResult(feature_type="rivet", position_m=[0.0, y, 0.0002], flushness_m=-1.0,
                             countersink_depth_m=-1.0, passed=True,
                             detail=json.dumps({"id": fid, "d_head_mm": 5.6, "partial": False}))
        m.header.stamp = self.node.get_clock().now().to_msg()
        self.pub.publish(m)


def detector_procs(name):
    """PIDs of running detector processes (the ROS graph keeps a killed node visible for a while)."""
    pids = []
    for d in os.listdir("/proc"):
        if d.isdigit():
            try:
                cmd = open(f"/proc/{d}/cmdline", "rb").read().split(b"\0")
                state = open(f"/proc/{d}/stat").read().rsplit(")", 1)[-1].split()[0]
            except OSError:
                continue
            if state != "Z" and any(c.endswith(f"/lib/{name}/{name}".encode()) for c in cmd):
                pids.append(int(d))
    return pids


def detector_pid():
    try:
        return int(open(os.path.join(DATA, "logs", "detector.pid")).read().split()[0])
    except (OSError, ValueError):
        return None


def main():
    p = Probe()
    ok = wait(lambda: http("GET", "/health")[0] == 200, 30)
    h = http("GET", "/health")[1] if ok else {}
    if not check("backend up with live ROS source", h.get("source") == "ros", str(h)):
        print("backend not reachable: see backend.log", flush=True)
        return 1
    check("operator identified", http("POST", "/operator", {"operator_id": "ENG-1042"})[0] == 200)

    # ---- one detector per inspection: rivets ----
    code, insp = http("POST", "/inspections", {"mode": "rivet_flushness", "label": "P2-RIVETS"})
    check("rivet inspection created", code == 200, str(insp))
    st = wait(lambda: (s := state())["detector"]["ready"] and s, 60)
    check("rivet_detector ready (process + ROS subscriptions)", bool(st), str(state()["detector"]))
    wait(lambda: p.nodes("rivet_detector") == 1, 10)      # this probe's graph view can lag the backend's
    check("exactly one rivet_detector node, no hole_detector",
          (p.nodes("rivet_detector"), p.nodes("hole_detector")) == (1, 0),
          f"rivet={p.nodes('rivet_detector')} hole={p.nodes('hole_detector')}")
    check("/rivets/markers (developer RViz topic) still published", "/rivets/markers" in p.topics())
    http("PATCH", f"/inspections/{insp['uuid']}/next-expected-count", {"count": 3})
    s = state()
    check("not ready until calibrated", "Calibration required" in s["not_ready_reasons"], str(s["not_ready_reasons"]))
    check("calibration via the existing service", p.call("calibration/start")[0])
    st = wait(lambda: (s := state())["screen"] == "ready" and s, 20)
    check("READY after calibration", bool(st), str(state()["not_ready_reasons"]))

    # ---- physical scan (service calls = what the GPIO switch node does) ----
    n0 = len(p.frames)
    t0 = time.monotonic()
    check("scan/start accepted by the controller", p.call("scan/start")[0])
    st = wait(lambda: (s := state())["screen"] == "scanning" and s, 5)
    check("UI shows SCANNING from confirmed scan/status", bool(st))
    p.publish_rivet(1, 0.015)
    p.publish_rivet(2, 0.040)
    time.sleep(2.5)
    check("scan/stop accepted", p.call("scan/stop")[0])
    t1 = time.monotonic()
    time.sleep(0.5)
    rate = (len(p.frames) - n0) / (t1 - t0)
    check("frame rate unaffected by UI + detector (sim 120 Hz)", rate >= 110, f"{rate:.0f} frames/s")
    p.publish_rivet(3, 0.065)                                    # after release
    st = wait(lambda: (s := state())["screen"] == "ready" and (s["preview"] or {}).get("detected_target_count") == 3 and s, 5)
    pv = (st or state())["preview"] or {}
    check("preview: 3 synthetic rivets, last after release, not measured",
          bool(st) and [f["after_release"] for f in pv["features"]] == [False, False, True]
          and all(f["measurement"]["status"] == "not_measured" for f in pv["features"]), json.dumps(pv)[:200])
    check("preview notice shown", bool(state()["preview_notice"]))
    import sqlite3
    db = sqlite3.connect(os.path.join(DATA, "inspections.db"))
    check("nothing recorded to history in preview",
          db.execute("SELECT COUNT(*) FROM scans").fetchone()[0] == 0
          and db.execute("SELECT COUNT(*) FROM features").fetchone()[0] == 0)
    check("no acceptance possible", http("GET", f"/inspections/{insp['uuid']}/scans")[1] == [])

    # ---- mode change: rivets -> CSK, never both ----
    rivet_pid = detector_pid()
    check("end inspection", http("POST", f"/inspections/{insp['uuid']}/end")[0] == 200)
    gone = wait(lambda: p.nodes("rivet_detector") == 0 and rivet_pid and not os.path.exists(f"/proc/{rivet_pid}"), 30)
    check("rivet_detector stopped (node and process gone)", bool(gone))
    code, insp2 = http("POST", "/inspections", {"mode": "csk_depth", "label": "P2-CSK"})
    st = wait(lambda: (s := state())["detector"]["ready"] and s["detector"]["mode"] == "csk_depth" and s, 60)
    check("hole_detector ready for CSK", bool(st), str(state()["detector"]))
    wait(lambda: p.nodes("hole_detector") == 1 and p.nodes("rivet_detector") == 0, 10)
    check("exactly one hole_detector, no rivet_detector",
          (p.nodes("hole_detector"), p.nodes("rivet_detector")) == (1, 0))
    check("/holes/markers still published", "/holes/markers" in p.topics())
    out = os.path.join(DATA, "detector-output", insp2["uuid"])
    check("detector output under the UI data root per inspection", os.path.isdir(out), out)

    # ---- crash and explicit recovery ----
    pid = detector_pid()
    os.kill(pid, signal.SIGKILL)
    st = wait(lambda: (s := state())["screen"] == "not_ready" and "exited with code" in " ".join(s["not_ready_reasons"]) and s, 10)
    check("crash detected: NOT READY with exit reason", bool(st), str(state()["not_ready_reasons"]))
    time.sleep(3)
    check("no silent auto-restart (no detector process)", detector_procs("hole_detector") == [],
          str(detector_procs("hole_detector")))
    check("explicit restart accepted", http("POST", "/detector/restart")[0] == 200)
    st = wait(lambda: (s := state())["detector"]["ready"] and s, 60)
    check("ready again after restart, new process", bool(st) and detector_pid() not in (None, pid))

    # ---- foreign detector (e.g. core launched with holes:=true) ----
    foreign = subprocess.Popen(["ros2", "run", "hole_detector", "hole_detector"], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, start_new_session=True)
    st = wait(lambda: (s := state())["screen"] == "not_ready" and "2 hole_detector nodes" in " ".join(s["not_ready_reasons"]) and s, 30)
    check("duplicate detector node detected: NOT READY", bool(st), str(state()["not_ready_reasons"]))
    os.killpg(foreign.pid, signal.SIGINT)
    foreign.wait(timeout=20)
    st = wait(lambda: (s := state())["screen"] == "ready" and s, 30)
    check("ready again after the foreign node left", bool(st), str(state()["not_ready_reasons"]))

    print(f"\n{'ALL PASSED' if not FAILS else f'{len(FAILS)} FAILED: ' + ', '.join(FAILS)}", flush=True)
    return len(FAILS)


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    os._exit(code)          # skip rclpy teardown while the spin thread runs (it can block)
