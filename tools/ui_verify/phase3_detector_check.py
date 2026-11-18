#!/usr/bin/env python3
"""Phase 3: the real hole_detector node in tagged mode, fed the synthetic test plate as tagged frames.

Scenarios (one detector process, back to back): normal scan; stream close arriving before the frames (with
redelivered frames); zero-frame scan; scan with missing frames; a late frame after finalization.
Checks: tagged mode reports exactly what the legacy (untagged) node reports for the same frames; results numbered
0..n-1 per scan; targets = countersinks; exactly one DetectionFinalized per scan; UUID folder with summary.csv
(also for zero results). Run by phase3_detector_check.sh; exit code = FAILs.

Frames are fed at the laser's real profile rate (CHECK_RATE, default 120/s). Fed at 400/s the rivet detector
falls behind and frames are lost: legacy mode then reports different features without notice, tagged mode fails
the scan with "missing frames" (the intended behaviour). The yardstick is equality with the legacy node.

hole_detector runs here with max_roundness 0.10 (default 0.08), in both modes: the plate's 3 mm hole sits at the
default limit, so whether it is accepted depends on where the 0.2 s cycles happen to cut the scan, with or without
tagging (reproduced offline, without ROS). This check compares the two modes; it must not hinge on that hole.
For the same reason a feature's reported position moves by about 0.1 mm between runs: positions are compared
within POSITION_TOL_MM, and the feature type must match exactly.
"""

import csv
import importlib.util
import os
import subprocess
import sys
import threading
import time
import uuid
from collections import defaultdict
from pathlib import Path

import numpy as np
import rclpy
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy

import rclpy.executors

from laser_hhrp.msg import (DetectionFinalized, InspectionResult, ScanFrame, ScanStreamClosed, TaggedInspectionResult,
                            TaggedScanFrame)

REPO = Path(__file__).resolve().parents[2]
OUT = Path(os.environ["CHECK_OUT"])
PKG = os.environ.get("CHECK_DETECTOR", "hole_detector")       # or rivet_detector
MODE = {"hole_detector": "csk_depth", "rivet_detector": "rivet_flushness"}[PKG]
TARGET = {"hole_detector": "countersink", "rivet_detector": "rivet"}[PKG]
RATE = float(os.environ.get("CHECK_RATE", "120"))   # frames/s: the laser's real profile rate
FAILS = []
RELIABLE = QoSProfile(depth=50, reliability=ReliabilityPolicy.RELIABLE)
LATCHED = QoSProfile(depth=50, reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.TRANSIENT_LOCAL)


def check(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}{('  -- ' + str(detail)) if detail else ''}", flush=True)
    if not ok:
        FAILS.append(name)
    return ok


# see the module docstring
EXTRA_PARAMS = ["-p", "max_roundness:=0.10"] if PKG == "hole_detector" else []


POSITION_TOL_MM = 0.5


def covered(wanted, got):
    """Every (y_mm, type) of `wanted` has its own entry in `got`: same type, y within the tolerance."""
    left = list(got)
    for y, kind in wanted:
        match = next((g for g in left if g[1] == kind and abs(g[0] - y) <= POSITION_TOL_MM), None)
        if match is None:
            return False
        left.remove(match)
    return True


def plate_profiles():
    """The detector tests' synthetic plate (7 holes, 3 countersunk) as profiles: one per scan position."""
    test = {"hole_detector": "hole_detector/test/test_detector.py", "rivet_detector": "rivet_detector/test/test_rivets.py"}[PKG]
    spec = importlib.util.spec_from_file_location("td", REPO / test)
    sys.path.insert(0, str((REPO / test).parent))
    td = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(td)
    pts = td.synthetic_plate() if PKG == "hole_detector" else td.synthetic_panel()
    ys = np.unique(pts[:, 1])
    order = np.argsort(pts[:, 1], kind="stable")
    pts = pts[order]
    bounds = np.searchsorted(pts[:, 1], ys)
    return [(float(y), pts[a:b, 0].astype(np.float32), pts[a:b, 2].astype(np.float32))
            for y, a, b in zip(ys, bounds, list(bounds[1:]) + [len(pts)])], (td.PLATE if PKG == "hole_detector" else [])


class Probe:
    def __init__(self):
        rclpy.init()
        n = self.node = rclpy.create_node("phase3_detector_probe")
        self.lock = threading.Lock()
        self.results = defaultdict(list)
        self.final = defaultdict(list)
        n.create_subscription(TaggedInspectionResult, "/inspection/result_tagged", self.on_result,
                              QoSProfile(depth=200, reliability=ReliabilityPolicy.RELIABLE))
        n.create_subscription(DetectionFinalized, "/inspection/detection_finalized", self.on_final, LATCHED)
        self.pub_frame = n.create_publisher(TaggedScanFrame, "/scan/frame_tagged",
                                            QoSProfile(depth=400, reliability=ReliabilityPolicy.RELIABLE))
        self.pub_close = n.create_publisher(ScanStreamClosed, "/scan/stream_closed", LATCHED)
        threading.Thread(target=rclpy.spin, args=(n,), daemon=True).start()

    def on_result(self, m):
        with self.lock:
            self.results[m.scan_id].append(m)

    def on_final(self, m):
        with self.lock:
            self.final[m.scan_id].append(m)

    def frame(self, scan_id, index, profile, rate=None):
        y, x, z = profile
        f = ScanFrame(index=index, position_m=y, x_m=x.tolist(), z_m=z.tolist(), valid=[True] * len(x))
        t = self.node.get_clock().now().to_msg()
        f.header.stamp, f.header.frame_id = t, "laser"
        self.pub_frame.publish(TaggedScanFrame(scan_id=scan_id, frame=f))
        time.sleep(1.0 / (rate or RATE))

    def close(self, scan_id, count, error=""):
        c = ScanStreamClosed(scan_id=scan_id, frame_count=count, stop_state=0, error=error)
        c.header.stamp = self.node.get_clock().now().to_msg()
        self.pub_close.publish(c)

    def wait_final(self, scan_id, timeout):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self.lock:
                if self.final[scan_id]:
                    return self.final[scan_id][0]
            time.sleep(0.05)
        return None

    def connected(self, timeout=30):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.pub_frame.get_subscription_count() and self.pub_close.get_subscription_count():
                return True
            time.sleep(0.1)
        return False


def legacy_positions(profiles):
    """Holes (y in mm, kind) the legacy node reports for the same frames (a next scan's index 0 ends it)."""
    exe = Path(os.environ["HHRP_WS"]) / f"install/{PKG}/lib/{PKG}/{PKG}"
    params = Path(os.environ["HHRP_WS"]) / f"install/{PKG}/share/{PKG}/config/params.yaml"
    det = subprocess.Popen([str(exe), "--ros-args", "--params-file", str(params), "-p", f"out_dir:={OUT}/legacy",
                            *EXTRA_PARAMS],
                           stdout=open(OUT / "legacy_detector.log", "w"), stderr=subprocess.STDOUT)
    node = rclpy.create_node("phase3_legacy_probe")
    found = []
    node.create_subscription(InspectionResult, "/inspection/result",
                             lambda m: found.append((round(m.position_m[1] * 1000, 1), m.feature_type)), 50)
    pub = node.create_publisher(ScanFrame, "/scan/frame", QoSProfile(depth=400, reliability=ReliabilityPolicy.RELIABLE))
    ex = rclpy.executors.SingleThreadedExecutor()
    ex.add_node(node)
    threading.Thread(target=ex.spin, daemon=True).start()
    end = time.monotonic() + 30
    while not pub.get_subscription_count() and time.monotonic() < end:
        time.sleep(0.1)

    def feed(items):
        for i, (y, x, z) in enumerate(items):
            f = ScanFrame(index=i, position_m=y, x_m=x.tolist(), z_m=z.tolist(), valid=[True] * len(x))
            f.header.stamp = node.get_clock().now().to_msg()
            pub.publish(f)
            time.sleep(1 / RATE)
    feed(profiles)
    time.sleep(2)
    feed(profiles[:5])
    time.sleep(3)
    det.send_signal(2)
    det.wait(timeout=15)
    ex.shutdown()
    node.destroy_node()
    return sorted(found)


def main():
    profiles, plate = plate_profiles()
    exe = Path(os.environ["HHRP_WS"]) / f"install/{PKG}/lib/{PKG}/{PKG}"
    params = Path(os.environ["HHRP_WS"]) / f"install/{PKG}/share/{PKG}/config/params.yaml"
    det = subprocess.Popen([str(exe), "--ros-args", "--params-file", str(params), "-p", f"out_dir:={OUT}/det",
                            "-p", "tagged:=true", "-p", "final_wait_s:=3.0", *EXTRA_PARAMS],
                           stdout=open(OUT / "detector.log", "w"), stderr=subprocess.STDOUT)
    p = Probe()
    legacy = legacy_positions(profiles)
    print(f"legacy node on the same frames: {legacy}", flush=True)
    try:
        check("detector subscribed to the tagged topics", p.connected())

        # ---- A: normal scan ----
        a = str(uuid.uuid4())
        for i, prof in enumerate(profiles):
            p.frame(a, i, prof)
        p.close(a, len(profiles))
        fa = p.wait_final(a, 60)
        ra = sorted(p.results[a], key=lambda m: m.result_seq)
        check("A finalized successfully, once", fa is not None and fa.success and len(p.final[a]) == 1,
              fa and (fa.success, fa.error))
        tagged_a = sorted((round(m.result.position_m[1] * 1000, 1), m.result.feature_type) for m in ra)
        check("A: tagged mode reports the same features as the legacy node",
              legacy and len(tagged_a) == len(legacy) and covered(legacy, tagged_a), f"tagged {tagged_a}")
        check("A: result_seq 0..n-1, feature ids 1..n",
              [m.result_seq for m in ra] == list(range(len(ra))) and [m.feature_id for m in ra] == list(
                  range(1, len(ra) + 1)), [(m.result_seq, m.feature_id) for m in ra])
        n_csk = sum(1 for _, kind in legacy if kind == TARGET)
        check(f"A: finalization counts = results, {n_csk} {TARGET} targets, all frames",
              fa is not None and fa.emitted_result_count == len(ra) and fa.detected_target_count == n_csk
              and fa.frame_count == len(profiles) and fa.detector_mode == MODE,
              fa and (fa.emitted_result_count, fa.detected_target_count, fa.frame_count))
        check("A: every result tagged with A's id and mode", all(m.scan_id == a and m.detector_mode == MODE
                                                                for m in ra))
        rows = list(csv.reader((OUT / "det" / a / "summary.csv").open())) if (OUT / "det" / a / "summary.csv").exists() else []
        check("A: UUID folder with one summary row per hole", len(rows) == len(ra) + 1, len(rows))

        # ---- B: close first, then frames (half the plate), some redelivered ----
        b = str(uuid.uuid4())
        half = profiles[: len(profiles) // 2]
        p.close(b, len(half))
        time.sleep(0.3)
        for i, prof in enumerate(half):
            p.frame(b, i, prof)
            if i % 50 == 0 and i:
                p.frame(b, i - 1, half[i - 1])          # redelivered: ignored
        fb = p.wait_final(b, 60)
        rb = p.results[b]
        radius = {round(y * 1000, 1): d / 2 for _, y, d, _ in plate}
        end_mm = half[-1][0] * 1000
        inside = [h for h in legacy if h[0] + radius.get(h[0], 0.015) * 1000 + 10 < end_mm]
        check("B (close before frames, duplicates): finalized once, success", fb is not None and fb.success
              and len(p.final[b]) == 1, fb and fb.error)
        tagged_b = sorted((round(m.result.position_m[1] * 1000, 1), m.result.feature_type) for m in rb)
        check("B: the legacy holes well inside the scanned half, none beyond the scanned length",
              covered(inside, tagged_b) and all(y <= end_mm for y, _ in tagged_b), f"{tagged_b} vs {inside}")
        check("B: counts consistent", fb is not None and fb.emitted_result_count == len(rb)
              and fb.frame_count == len(half))

        # ---- C: zero-frame scan ----
        c = str(uuid.uuid4())
        p.close(c, 0)
        fc = p.wait_final(c, 15)
        check("C (zero frames): finalized success with 0 results", fc is not None and fc.success
              and fc.emitted_result_count == 0 and not p.results[c])
        check("C: UUID folder exists with an empty summary", (OUT / "det" / c / "summary.csv").exists()
              and len(list(csv.reader((OUT / "det" / c / "summary.csv").open()))) == 1)

        # ---- D: missing frames ----
        d = str(uuid.uuid4())
        part = profiles[:300]
        for i, prof in enumerate(part):
            if i not in (100, 101):
                p.frame(d, i, prof)
        p.close(d, len(part))
        fd = p.wait_final(d, 20)
        check("D (2 frames missing): finalized as FAILED with the reason", fd is not None and not fd.success
              and "missing frames: 2 of 300" in fd.error, fd and fd.error)

        # ---- E: late frame after finalization ----
        p.frame(a, 5, profiles[5])
        p.close(a, len(profiles))
        time.sleep(2.0)
        check("E: a late frame / close for a finalized scan does not reopen it", len(p.final[a]) == 1)

        # ---- F: stream closed with an error ----
        e = str(uuid.uuid4())
        for i, prof in enumerate(profiles[:50]):
            p.frame(e, i, prof)
        p.close(e, 50, error="scan identity arrived late: 3 frames not tagged")
        fe = p.wait_final(e, 15)
        check("F (stream error): finalized as FAILED, error passed on", fe is not None and not fe.success
              and "identity arrived late" in fe.error, fe and fe.error)
    finally:
        det.send_signal(2)
        try:
            det.wait(timeout=15)
        except subprocess.TimeoutExpired:
            det.kill()
    check("detector exits cleanly on SIGINT", det.returncode == 0, det.returncode)
    print(f"\n{'ALL PASSED' if not FAILS else f'{len(FAILS)} FAILED: ' + ', '.join(FAILS)}", flush=True)
    return len(FAILS)


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    os._exit(code)
