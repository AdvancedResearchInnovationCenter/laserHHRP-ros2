#!/usr/bin/env python3
"""Replay a bag recorded on the device (`ros2 bag record -a` while the stack ran) into the real stack, in place
of the two drivers: everything from the synchronizer on (odometry, point cloud, scan controller, recorder,
detector, measurement, operator UI backend) runs as on the device, on the recorded laser profiles, encoder, IMU
and trigger samples. Run by replay_stack.sh, which starts the stack without the drivers.

- Published: laser/raw, laser/status, nano/encoder, nano/imu, nano/trigger, nano/clock, nano/status, at the
  recorded pace. Pi-time stamps (headers, ClockMap.host_ns) move to now; Nano and scanner clock values stay, so
  every pairing in the synchronizer is the recorded one.
- The scan switch: scan/start and scan/stop at the recorded transitions of scan/status.
- --ui URL: an operator UI backend drives the inspection. Before each scan the replay waits (holding the stream:
  the stamps then move on by the hold, and laser/nano status keep coming) until the backend is Ready, sets the
  next expected count, and after each scan until its analysis has ended, then accepts it (Results screen).

    python3 raw_replay.py BAG [--ui http://127.0.0.1:8099 --mode csk_depth --counts 15,20] [--out result.json]

Exit code 0 when every scan was started and stopped (and, with --ui, analysed); the per-scan results are
printed and written to --out.
"""

import argparse
import json
import queue
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import rclpy
import rosbag2_py
from rclpy.executors import SingleThreadedExecutor
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from rclpy.serialization import deserialize_message
from rosidl_runtime_py.utilities import get_message
from std_srvs.srv import Trigger

RAW = {  # topic: publisher depth (as the drivers), transient local
    "/laser/raw": (200, False), "/laser/status": (10, False), "/nano/encoder": (500, False),
    "/nano/imu": (500, False), "/nano/trigger": (500, False), "/nano/clock": (10, True), "/nano/status": (10, False),
}
HEARTBEAT = ("/laser/status", "/nano/status", "/nano/clock")     # repeated while the stream is held


def open_reader(bag: Path, topics):
    reader = rosbag2_py.SequentialReader()
    try:
        reader.open(rosbag2_py.StorageOptions(uri=str(bag), storage_id="mcap"), rosbag2_py.ConverterOptions("cdr", "cdr"))
    except Exception:       # a Jazzy bag on Humble: its metadata is too new, the mcap file itself reads fine
        reader = rosbag2_py.SequentialReader()
        reader.open(rosbag2_py.StorageOptions(uri=str(next(bag.glob("*.mcap"))), storage_id="mcap"),
                    rosbag2_py.ConverterOptions("cdr", "cdr"))
    types = {t.name: t.type for t in reader.get_all_topics_and_types()}
    reader.set_filter(rosbag2_py.StorageFilter(topics=[t for t in topics if t in types]))
    return reader, types


def transitions(bag: Path):
    """[(record time ns, 'start' | 'stop')] from the recorded scan/status (SCANNING = 1)."""
    reader, types = open_reader(bag, ["/scan/status"])
    cls, out, state = get_message(types["/scan/status"]), [], 0
    while reader.has_next():
        _, data, t = reader.read_next()
        s = 1 if deserialize_message(data, cls).state == 1 else 0
        if s != state:
            out.append((t, "start" if s else "stop"))
            state = s
    if state:                                    # recording ended while scanning
        out.append((None, "stop"))
    return out


class Api:
    def __init__(self, base):
        self.base = base.rstrip("/") + "/api/v1"

    def call(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            return 0, {}

    def state(self):
        return self.call("GET", "/state")[1]


class Replay:
    def __init__(self, args):
        self.args = args
        rclpy.init()
        self.node = rclpy.create_node("raw_replay")
        self.reader, self.types = open_reader(args.bag, list(RAW))
        self.cls = {t: get_message(self.types[t]) for t in RAW if t in self.types}
        self.pubs = {}
        for topic, (depth, latched) in RAW.items():
            if topic in self.cls:
                qos = QoSProfile(depth=depth, reliability=ReliabilityPolicy.RELIABLE,
                                 durability=DurabilityPolicy.TRANSIENT_LOCAL if latched else DurabilityPolicy.VOLATILE)
                self.pubs[topic] = self.node.create_publisher(self.cls[topic], topic[1:], qos)
        self.srv = {n: self.node.create_client(Trigger, n) for n in ("scan/start", "scan/stop")}
        self.executor = SingleThreadedExecutor()
        self.executor.add_node(self.node)
        self.spinner = threading.Thread(target=self.executor.spin, daemon=True)
        self.spinner.start()
        self.last = {}            # topic: last message (heartbeat while held)
        self.shift = None         # ns added to Pi-time stamps
        self.api = Api(args.ui) if args.ui else None
        self.results = []

    # ---- stamps ----
    def restamp(self, topic, msg):
        if hasattr(msg, "header"):
            ns = msg.header.stamp.sec * 1_000_000_000 + msg.header.stamp.nanosec + self.shift
            msg.header.stamp.sec, msg.header.stamp.nanosec = divmod(ns, 1_000_000_000)
        if topic == "/nano/clock":
            msg.host_ns += self.shift
        return msg

    def hold(self, until, what, timeout):
        """Hold the stream until until() is true: status keeps coming (now-stamped), the stamps move on."""
        t0, beat, deadline = time.monotonic(), 0.0, time.monotonic() + timeout
        while not until():
            if time.monotonic() > deadline:
                why = ""
                if self.api:
                    st = self.api.state()
                    why = " (screen %s, not ready: %s, detector %s)" % (st.get("screen"), st.get("not_ready_reasons"),
                                                                        st.get("detector"))
                raise TimeoutError("waited %.0f s for: %s%s" % (timeout, what, why))
            if time.monotonic() - beat > 0.5:
                beat = time.monotonic()
                for topic in HEARTBEAT:
                    if topic in self.last:
                        msg = deserialize_message(self.last[topic][0], self.cls[topic])
                        self.shift_for_heartbeat(topic, msg, self.last[topic][1])
            time.sleep(0.05)
        self.shift += int((time.monotonic() - t0) * 1e9)

    def shift_for_heartbeat(self, topic, msg, rec_t):
        now = time.time_ns()
        if hasattr(msg, "header"):
            msg.header.stamp.sec, msg.header.stamp.nanosec = divmod(now, 1_000_000_000)
        if topic == "/nano/clock":     # the same map, moved to now: host_ns of its device_ns instant
            msg.host_ns += now - (rec_t + self.shift)
        self.pubs[topic].publish(msg)

    # ---- the switch ----
    def press(self, name):
        c = self.srv[name]
        if not c.wait_for_service(timeout_sec=20):
            raise RuntimeError(name + ": no service (is scan_controller running?)")
        fut = c.call_async(Trigger.Request())
        end = time.monotonic() + 10
        while not fut.done() and time.monotonic() < end:
            time.sleep(0.005)
        r = fut.result()
        print("%s: %s %s" % (name, "ok" if r and r.success else "FAILED", r.message if r else "(no answer)"), flush=True)
        return bool(r and r.success)

    # ---- operator UI ----
    def settle(self, s):
        """Results screen: accept a completed analysis, keep a failed one (as an operator would, to go on)."""
        scan = s.get("current_scan") or {}
        if s.get("screen") == "results" and scan.get("analysis") == "complete" and not scan.get("decision"):
            self.api.call("POST", "/scans/%s/decision" % scan["scan_id"], {"decision": "accept"})
        elif s.get("screen") == "analysis_failed" and not scan.get("decision"):
            self.api.call("POST", "/scans/%s/failure-action" % scan["scan_id"], {"action": "keep_incomplete"})

    def ui_ready(self, count):
        s = self.api.state()
        if s.get("screen") in ("results", "analysis_failed"):
            self.settle(s)
            return False
        if s.get("screen") not in ("ready", "not_ready"):
            return False
        insp = s.get("inspection") or {}
        if count is not None and insp.get("next_expected_count") != count:
            self.api.call("PATCH", "/inspections/%s/next-expected-count" % insp["uuid"], {"count": count})
            return False
        return s.get("screen") == "ready"

    def ui_analysed(self, recorded):
        s = self.api.state()
        scan = s.get("current_scan") or {}
        return scan.get("analysis") not in (None, "pending") and scan.get("scan_id") not in recorded

    def ui_setup(self):
        api = self.api
        self.hold(lambda: bool(api.state()), "backend up", 60)
        if api.state().get("screen") == "identify":
            api.call("POST", "/operator", {"operator_id": self.args.operator})
        if not (api.state().get("inspection") or {}).get("uuid"):
            st, body = api.call("POST", "/inspections", {"mode": self.args.mode})
            if st != 200:
                raise RuntimeError("create inspection: %s %s" % (st, body))

    def run(self):
        marks = transitions(self.args.bag)
        counts = [int(c) for c in self.args.counts.split(",")] if self.args.counts else []
        print("%s: %d scan(s) in the recording" % (self.args.bag.name, sum(1 for _, k in marks if k == "start")), flush=True)
        t_rec0 = wall0 = None
        scan_no, recorded = 0, []
        ahead = queue.Queue(maxsize=20000)        # ~10 s of messages read ahead: big chunks never stall the pace

        def read():
            while self.reader.has_next():
                ahead.put(self.reader.read_next())
            ahead.put(None)
        threading.Thread(target=read, daemon=True).start()
        self.late_max = 0.0
        while True:
            item = ahead.get()
            if item is None:
                break
            topic, data, t = item
            if t_rec0 is None:
                t_rec0, wall0 = t, time.monotonic_ns()
                self.shift = time.time_ns() - t
                if self.api:   # the stream starts held: status and clock map first, then the backend's setup
                    self.last[topic] = (data, t)
                    self.ui_setup()
            while marks and marks[0][0] is not None and marks[0][0] <= t:
                _, kind = marks.pop(0)
                if kind == "start":
                    if self.api:
                        count = counts[scan_no] if scan_no < len(counts) else None
                        before = time.monotonic_ns()
                        self.hold(lambda: self.ui_ready(count), "operator UI Ready", self.args.wait)
                        wall0 += time.monotonic_ns() - before
                    self.press("scan/start")
                    scan_no += 1
                else:
                    self.press("scan/stop")
                    if self.api:
                        before = time.monotonic_ns()
                        time.sleep(0.5)
                        self.hold(lambda: self.ui_analysed(recorded), "analysis of scan %d" % scan_no, self.args.wait)
                        scan = self.api.state()["current_scan"]
                        recorded.append(scan["scan_id"])
                        self.results.append(self.api.call("GET", "/scans/%s" % scan["scan_id"])[1])
                        print("scan %d: analysis %s" % (scan_no, scan.get("analysis")), flush=True)
                        wall0 += time.monotonic_ns() - before
            # the recorded pace
            due = wall0 + (t - t_rec0) / self.args.rate
            delay = (due - time.monotonic_ns()) / 1e9
            if delay > 0:
                time.sleep(delay)
            elif -delay > self.late_max:
                self.late_max = -delay
                if self.late_max > 0.2:
                    print("replay late by %.2f s (the stack sees a gap)" % self.late_max, flush=True)
            msg = self.restamp(topic, deserialize_message(data, self.cls[topic]))
            self.pubs[topic].publish(msg)
            if topic in HEARTBEAT:
                self.last[topic] = (data, t)
        if marks and marks[0][1] == "stop":
            self.press("scan/stop")
        if self.api:
            self.hold(lambda: self.ui_analysed(recorded) or len(recorded) >= scan_no, "last analysis", self.args.wait)
            s = self.api.state()
            scan = s.get("current_scan")
            if scan and scan["scan_id"] not in recorded:
                self.results.append(self.api.call("GET", "/scans/%s" % scan["scan_id"])[1])
            self.settle(s)
        return scan_no


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bag", type=Path)
    ap.add_argument("--rate", type=float, default=1.0, help="replay speed (1 = as recorded)")
    ap.add_argument("--ui", default=None, help="operator UI backend URL: drive an inspection through it")
    ap.add_argument("--mode", default="csk_depth", help="inspection mode (csk_depth | rivet_flushness)")
    ap.add_argument("--counts", default="", help="expected feature count per scan, comma separated")
    ap.add_argument("--operator", default="REPLAY-1")
    ap.add_argument("--wait", type=float, default=300, help="s to wait for Ready / an analysis")
    ap.add_argument("--out", type=Path, default=None, help="per-scan results (JSON)")
    args = ap.parse_args(argv)
    r = Replay(args)
    try:
        scans = r.run()
    finally:
        if args.out:
            args.out.write_text(json.dumps(r.results, indent=1))
        r.executor.shutdown()
        r.spinner.join(timeout=5)
        r.node.destroy_node()
        rclpy.shutdown()
    print("replayed %d scan(s), at most %.2f s late" % (scans, r.late_max))
    return 0


if __name__ == "__main__":
    sys.exit(main())
