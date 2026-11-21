#!/usr/bin/env python3
"""Replay an old bag's /scan/frame through the operator UI's tagged path (plan section 5, legacy bag adapter).

Old bags carry the original ScanFrame only: no scan identity and no stream-close marker. This adapter
reads the bag (never modifies it), splits it into scans at every index 0, gives each scan a deterministic UUID
(uuid5 of the bag path and scan number), publishes the frames as /scan/frame_tagged and, after each scan's last
frame, a /scan/stream_closed with the number of frames published. It does not invent a switch-release time:
the close marks only "no more frames in the bag for this scan". Each published copy is stamped with the replay
time: tagged detectors ignore frames exposed before they started (earlier scans from the synchronizer's history).

    python3 legacy_bag_adapter.py <bag> [--rate 120] [--wait-final 60]

With a tagged detector running (e.g. hole_detector with tagged:=true), it prints each scan's
DetectionFinalized; exit code 0 only if every scan was finalized successfully.
"""

import argparse
import sys
import threading
import time
import uuid
from pathlib import Path

import rclpy
import rosbag2_py
from rclpy.executors import SingleThreadedExecutor
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from rclpy.serialization import deserialize_message

from laser_hhrp.msg import DetectionFinalized, ScanFrame, ScanStreamClosed, TaggedScanFrame


def bag_scans(bag: Path):
    """Lists of ScanFrame per scan, split where index restarts at 0."""
    meta = rosbag2_py.Info().read_metadata(str(bag), "")
    reader = rosbag2_py.SequentialReader()
    reader.open(rosbag2_py.StorageOptions(uri=str(bag), storage_id=meta.storage_identifier),
                rosbag2_py.ConverterOptions("cdr", "cdr"))
    reader.set_filter(rosbag2_py.StorageFilter(topics=["/scan/frame"]))
    scans, current = [], []
    while reader.has_next():
        _, data, _ = reader.read_next()
        f = deserialize_message(data, ScanFrame)
        if f.index == 0 and current:
            scans.append(current)
            current = []
        current.append(f)
    if current:
        scans.append(current)
    return scans


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bag", type=Path)
    ap.add_argument("--rate", type=float, default=120.0, help="frames/s (the laser's real profile rate)")
    ap.add_argument("--wait-final", type=float, default=60.0, help="s to wait for each DetectionFinalized")
    args = ap.parse_args(argv)
    scans = bag_scans(args.bag)
    if not scans:
        print(f"{args.bag}: no /scan/frame messages")
        return 2
    rclpy.init()
    node = rclpy.create_node("legacy_bag_adapter")
    finals, lock = {}, threading.Lock()

    def on_final(m):
        with lock:
            finals[m.scan_id] = m
    node.create_subscription(DetectionFinalized, "/inspection/detection_finalized", on_final, QoSProfile(
        depth=50, reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.TRANSIENT_LOCAL))
    pub = node.create_publisher(TaggedScanFrame, "/scan/frame_tagged", QoSProfile(     # as the synchronizer
        depth=1200, reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.TRANSIENT_LOCAL))
    close = node.create_publisher(ScanStreamClosed, "/scan/stream_closed", QoSProfile(
        depth=50, reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.TRANSIENT_LOCAL))
    executor = SingleThreadedExecutor()
    executor.add_node(node)
    spinner = threading.Thread(target=executor.spin, daemon=True)
    spinner.start()
    end = time.monotonic() + 30
    while not pub.get_subscription_count() and time.monotonic() < end:
        time.sleep(0.1)
    if not pub.get_subscription_count():
        print("no tagged detector is subscribed to /scan/frame_tagged")
        executor.shutdown()
        return 2
    failed = 0
    for n, frames in enumerate(scans, start=1):
        scan_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{args.bag.resolve()}#scan{n}"))
        for f in frames:
            f.header.stamp = node.get_clock().now().to_msg()   # detectors ignore frames older than themselves
            pub.publish(TaggedScanFrame(scan_id=scan_id, frame=f))
            time.sleep(1.0 / args.rate)
        c = ScanStreamClosed(scan_id=scan_id, frame_count=len(frames), stop_state=0, error="")
        c.header.stamp = node.get_clock().now().to_msg()
        close.publish(c)
        end = time.monotonic() + args.wait_final
        while time.monotonic() < end and scan_id not in finals:
            time.sleep(0.05)
        m = finals.get(scan_id)
        if m is None or not m.success:
            failed += 1
        print(f"scan {n} ({scan_id}): {len(frames)} frames -> " + (
            "no DetectionFinalized" if m is None else
            f"{'ok' if m.success else 'FAILED'}, {m.emitted_result_count} results, {m.detected_target_count} targets"
            + (f": {m.error}" if m.error else "")), flush=True)
    executor.shutdown()                  # stop spinning before the node goes away
    spinner.join(timeout=5)
    node.destroy_node()
    rclpy.try_shutdown()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
