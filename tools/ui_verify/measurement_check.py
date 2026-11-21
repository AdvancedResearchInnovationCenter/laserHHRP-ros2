#!/usr/bin/env python3
"""The real inspection_node (measurement algorithms), fed the algorithms' own test folders (isolated ROS graph).

Copies of the submodules' test data stand in for the detector's scan folders; this script plays the detector and
publishes DetectionFinalized for them. Checks:
- CSK Depth (laser_csk_inspection csk_depth/tests/data/flap2_13112026_01): every countersink measured, within
  CSK_TOL_UM of its expected_depth.csv; a row marked partial is not measured (reason "partial").
- Rivet Flushness (rivet_flushness/tests/data/panel2_*): each rivet equals expected.csv (value within RIVET_TOL_UM,
  or the same reason code, e.g. scan_distorted). These PLY-derived folders keep the raw z sign, so the node runs
  with inspection.rivet_invert_z:=true here; the detector's own folders need false (hhrp.yaml).
- limits and quantity as in hhrp.yaml; measurement.csv written next to summary.csv with the same values;
- exactly one MeasurementFinalized per scan, also when the finalization is delivered twice;
- a failed detection gets no measurement; an unreadable scan folder gets success = false with the reason.
Prints the measuring time per scan. Run by measurement_check.sh; exit code = number of FAILs.
"""

import csv
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import rclpy
import yaml
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy

from laser_hhrp.msg import DetectionFinalized, MeasurementFinalized

REPO = Path(__file__).resolve().parents[2]
OUT = Path(os.environ["CHECK_OUT"])
WS = Path(os.environ["HHRP_WS"])
CSK_DATA = REPO / "laserHHRP/third_party/laser_csk_inspection/csk_depth/tests/data"
RIVET_DATA = REPO / "laserHHRP/third_party/rivet_flushness/tests/data"
CSK_TOL_UM = float(os.environ.get("CSK_TOL_UM", "2.0"))      # x86 laptop vs ARM Pi floating point
RIVET_TOL_UM = float(os.environ.get("RIVET_TOL_UM", "0.5"))  # the library's own ctest tolerance
FAILS = []
LATCHED = QoSProfile(depth=50, reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.TRANSIENT_LOCAL)


def check(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}{('  -- ' + str(detail)) if detail else ''}", flush=True)
    if not ok:
        FAILS.append(name)
    return ok


def read_csv(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def copy_scan(src: Path, name: str) -> Path:
    dst = OUT / "scans" / name
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("expected*", "dev_reference*", "rivet_flush_out"))
    return dst


class Detector:
    """Plays the tagged detector: publishes DetectionFinalized, collects the node's measurements."""

    def __init__(self):
        rclpy.init()
        self.node = rclpy.create_node("measurement_check")
        self.pub = self.node.create_publisher(DetectionFinalized, "/inspection/detection_finalized", LATCHED)
        self.got = {}
        self.node.create_subscription(MeasurementFinalized, "/inspection/measurement",
                                      lambda m: self.got.setdefault(m.scan_id, []).append(m), LATCHED)

    def spin(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            rclpy.spin_once(self.node, timeout_sec=0.05)

    def wait_for_node(self, timeout=30.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.node.count_subscribers("/inspection/detection_finalized") and \
                    self.node.count_publishers("/inspection/measurement"):
                return True
            self.spin(0.2)
        return False

    def finalize(self, mode, folder, success=True, scan_id=None, repeat=1):
        scan_id = scan_id or str(uuid.uuid4())
        for _ in range(repeat):
            m = DetectionFinalized(scan_id=scan_id, detector_mode=mode, frame_count=100, emitted_result_count=0,
                                   detected_target_count=0, success=success, error="" if success else "test failure",
                                   output_dir=str(folder))
            self.pub.publish(m)
            self.spin(0.1)
        return scan_id

    def result(self, scan_id, timeout=60.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end and scan_id not in self.got:
            self.spin(0.1)
        self.spin(1.0)                                  # a second (duplicate) message would arrive by now
        return self.got.get(scan_id, [])


def csv_matches(folder: Path, msg) -> bool:
    try:
        rows = read_csv(folder / "measurement.csv")
    except OSError:
        return False
    key = f"{msg.quantity}_um"
    if [int(r["id"]) for r in rows] != [f.feature_id for f in msg.features]:
        return False
    for r, f in zip(rows, msg.features):
        if f.measured != (r[key] != "") or r["reason"] != f.reason:
            return False
        if f.measured and abs(float(r[key]) - f.value_m * 1e6) > 0.006:
            return False
    return True


def main():
    params = WS / "install/laser_hhrp/share/laser_hhrp/config/hhrp.yaml"
    conf = yaml.safe_load(params.read_text())["/**"]["ros__parameters"]
    exe = WS / "install/laser_hhrp/lib/laser_hhrp/inspection_node"
    log = open(OUT / "inspection_node.log", "wb")
    proc = subprocess.Popen([str(exe), "--ros-args", "--params-file", str(params),
                             "-p", "inspection.rivet_invert_z:=true"], stdout=log, stderr=subprocess.STDOUT)
    det = Detector()
    try:
        if not check("inspection_node up (subscribes detection_finalized, publishes measurement)", det.wait_for_node()):
            return
        # ---- CSK Depth
        for src in sorted(p for p in CSK_DATA.iterdir() if (p / "expected_depth.csv").is_file()):
            folder = copy_scan(src, "csk_" + src.name)
            expected = {int(r["id"]): float(r["depth_mm"]) * 1000.0 for r in read_csv(src / "expected_depth.csv")}
            targets = [int(r["id"]) for r in read_csv(src / "summary.csv") if r["countersunk"] == "True"]
            sid = det.finalize("csk_depth", folder, repeat=2)       # delivered twice: measured once
            msgs = det.result(sid)
            if not check(f"csk {src.name}: exactly one measurement message", len(msgs) == 1, len(msgs)) or not msgs:
                continue
            m = msgs[0]
            check(f"csk {src.name}: success, quantity csk_depth", m.success and m.quantity == "csk_depth", m.error)
            check(f"csk {src.name}: limits from hhrp.yaml",
                  (m.lower_limit_m, m.upper_limit_m) == (conf["inspection.csk_depth_min_m"], conf["inspection.csk_depth_max_m"]))
            check(f"csk {src.name}: one entry per countersink", [f.feature_id for f in m.features] == targets,
                  [f.feature_id for f in m.features])
            worst = max((abs(f.value_m * 1e6 - expected[f.feature_id]) for f in m.features if f.measured
                         and f.feature_id in expected), default=float("inf"))
            check(f"csk {src.name}: all {len(expected)} measured, within {CSK_TOL_UM} um of expected_depth.csv",
                  all(f.measured for f in m.features if f.feature_id in expected) and worst <= CSK_TOL_UM,
                  f"worst {worst:.2f} um")
            check(f"csk {src.name}: measurement.csv matches the message", csv_matches(folder, m))
            print(f"      csk {src.name}: {len(m.features)} countersinks in {m.elapsed_s:.2f} s "
                  f"(per feature max {max(f.elapsed_s for f in m.features) * 1e3:.0f} ms)", flush=True)
            # one countersink marked partial: never handed to the algorithm
            folder2 = copy_scan(src, "csk_partial_" + src.name)
            rows = read_csv(folder2 / "summary.csv")
            rows[0]["partial"] = "True"
            with open(folder2 / "summary.csv", "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(rows[0]))
                w.writeheader()
                w.writerows(rows)
            msgs = det.result(det.finalize("csk_depth", folder2))
            first = msgs[0].features[0] if msgs and msgs[0].features else None
            check(f"csk {src.name}: a partial countersink is not measured (reason partial)",
                  first is not None and not first.measured and first.reason == "partial",
                  first and (first.measured, first.reason))
        # ---- Rivet Flushness
        for src in sorted(p for p in RIVET_DATA.iterdir() if (p / "expected.csv").is_file()):
            folder = copy_scan(src, "rivet_" + src.name)
            expected = {int(r["id"]): r for r in read_csv(src / "expected.csv")}
            msgs = det.result(det.finalize("rivet_flushness", folder))
            if not check(f"rivet {src.name}: exactly one measurement message", len(msgs) == 1, len(msgs)) or not msgs:
                continue
            m = msgs[0]
            check(f"rivet {src.name}: success, quantity flushness", m.success and m.quantity == "flushness", m.error)
            check(f"rivet {src.name}: limits from hhrp.yaml",
                  (m.lower_limit_m, m.upper_limit_m) == (conf["inspection.flushness_min_m"], conf["inspection.flushness_max_m"]))
            check(f"rivet {src.name}: one entry per rivet", sorted(f.feature_id for f in m.features) == sorted(expected))
            bad = []
            for f in m.features:
                e = expected.get(f.feature_id)
                if e is None:
                    continue
                if e["value_um"]:
                    if not f.measured or abs(f.value_m * 1e6 - float(e["value_um"])) > RIVET_TOL_UM:
                        bad.append((f.feature_id, round(f.value_m * 1e6, 2) if f.measured else f.reason, e["value_um"]))
                elif f.measured or f.reason != e["reason"]:
                    bad.append((f.feature_id, round(f.value_m * 1e6, 2) if f.measured else f.reason, e["reason"]))
            check(f"rivet {src.name}: values within {RIVET_TOL_UM} um / same reasons as expected.csv", not bad, bad)
            check(f"rivet {src.name}: measurement.csv matches the message", csv_matches(folder, m))
            print(f"      rivet {src.name}: {len(m.features)} rivets in {m.elapsed_s:.2f} s "
                  f"(per feature max {max(f.elapsed_s for f in m.features) * 1e3:.0f} ms)", flush=True)
        # ---- failures
        sid = det.finalize("csk_depth", OUT / "scans" / "does-not-exist")
        msgs = det.result(sid, timeout=10)
        check("unreadable scan folder: success false with the reason",
              len(msgs) == 1 and not msgs[0].success and "summary.csv" in msgs[0].error and not msgs[0].features,
              msgs and msgs[0].error)
        sid = det.finalize("csk_depth", OUT / "scans" / "does-not-exist", success=False)
        check("failed detection: no measurement", det.result(sid, timeout=3) == [])
        check("inspection_node still running", proc.poll() is None, proc.poll())
    finally:
        det.node.destroy_node()
        rclpy.try_shutdown()
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


if __name__ == "__main__":
    main()
    print(f"\n{len(FAILS)} FAIL(s)" + (": " + "; ".join(FAILS) if FAILS else ""))
    sys.exit(len(FAILS))
