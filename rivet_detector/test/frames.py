"""Frame-shaped bag input and a deterministic replay clock for detector tests."""

from dataclasses import dataclass
import time

import numpy as np


@dataclass
class Replay:
    features: list
    latency: list
    cycle: list


def _reader(bag, topics):
    import rosbag2_py
    reader = rosbag2_py.SequentialReader()
    reader.open(rosbag2_py.StorageOptions(uri=str(bag), storage_id="mcap"),
                rosbag2_py.ConverterOptions("cdr", "cdr"))
    reader.set_filter(rosbag2_py.StorageFilter(topics=topics))
    return reader


def _stamp(msg):
    return msg.header.stamp.sec + msg.header.stamp.nanosec * 1e-9


def bag_frames(bag):
    """Yield ``(stamp, x, position, z, valid, index)`` profiles from a frame or cloud bag (index 0 = a new
    scan; a cloud-only bag is one scan rebuilt from its last /scan/cloud)."""
    from rclpy.serialization import deserialize_message
    from laser_hhrp.msg import ScanFrame
    reader = _reader(bag, ["/scan/frame"])
    frames = []
    while reader.has_next():
        _, data, _ = reader.read_next()
        msg = deserialize_message(data, ScanFrame)
        frames.append((_stamp(msg), np.asarray(msg.x_m), msg.position_m,
                       np.asarray(msg.z_m), np.asarray(msg.valid, dtype=bool), msg.index))
    if frames:
        yield from frames
        return

    from sensor_msgs.msg import PointCloud2
    from sensor_msgs_py import point_cloud2
    reader = _reader(bag, ["/scan/cloud"])
    clouds = []
    while reader.has_next():
        _, data, _ = reader.read_next()
        msg = deserialize_message(data, PointCloud2)
        xyz = point_cloud2.read_points_numpy(msg, field_names=("x", "y", "z"), skip_nans=True)
        clouds.append((_stamp(msg), xyz.astype(np.float64)))
    if not clouds:
        return
    start, (_, xyz), end = clouds[0][0], clouds[-1], clouds[-1][0]
    # One profile per scan position (y), in the order the positions first appear: the scan order (the panel
    # bags run toward -y). A pause gives many profiles at one y: they become one frame.
    _, first, inverse = np.unique(xyz[:, 1], return_index=True, return_inverse=True)
    order = np.argsort(first)
    members = np.argsort(inverse, kind="stable")
    bounds = np.searchsorted(inverse[members], np.arange(len(first) + 1))
    n = len(first)
    for i, k in enumerate(order):
        line = xyz[members[bounds[k]:bounds[k + 1]]]
        stamp = start if n == 1 else start + (end - start) * i / (n - 1)
        yield stamp, line[:, 0], float(line[0, 1]), line[:, 2], np.ones(len(line), dtype=bool), i


def replay(bag, tracker, period=0.2, cost_scale=1.0):
    """Replay profiles in simulated real time: a cycle starts every `period` s of bag time, or when the last
    one ends if it overran (its wall time x cost_scale, e.g. 3 for the Pi). Returns the accepted features, each
    one's latency (publish time - stamp of the frame that completed it) and every cycle's wall time."""
    import importlib
    FrameCloud = importlib.import_module(type(tracker).__module__).FrameCloud
    frames = list(bag_frames(bag))
    starts = [k for k, f in enumerate(frames) if f[5] == 0 and k > 0] + [len(frames)]
    run = Replay([], [], [])
    for lo, hi in zip([0] + starts[:-1], starts):
        tracker.reset()
        scan_run(frames[lo:hi], tracker, FrameCloud(), period, cost_scale, run)
    return run


def scan_run(frames, tracker, cloud, period, cost_scale, run):
    """One scan, as the node runs it: a cycle per tick, and a last one on the whole remaining cloud at the end."""
    next_tick, i, end_cycle, publication, found = frames[0][0], 0, frames[0][0], [], []
    while i < len(frames):
        tick = max(next_tick, frames[i][0], end_cycle)
        while i < len(frames) and frames[i][0] <= tick:
            cloud.add(*frames[i][1:5])
            i += 1
        started = time.perf_counter()
        _, new = tracker.feed(cloud.cloud(), tick)
        elapsed = time.perf_counter() - started
        run.cycle.append(elapsed)
        found.extend(new)
        publication.extend([tick + elapsed * cost_scale] * len(new))
        cloud.trim(tracker)
        end_cycle = tick + elapsed * cost_scale
        next_tick += period
    positions = np.array([f[2] for f in frames])
    stamps = np.array([f[0] for f in frames])
    for feature, published in zip(found, publication):
        # The frame that completes the feature: its far edge + ring + guard scanned.
        limit = tracker.sign * feature.centre[1] + feature.radius + tracker.p["ring"] + tracker.p["guard"]
        eligible = np.flatnonzero(tracker.sign * positions >= limit)
        run.latency.append(published - stamps[eligible[0] if len(eligible) else -1])
    run.features.extend(found)
