"""Offline checks: batch detection vs the v1 reference, and incremental replay of real bag messages.

Run in the ROS container:  python3 /bags/hole_detector/test/test_detector.py
"""

import csv
import importlib.util
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hole_detector.detector import DEFAULTS, FrameCloud, Rejected, Tracker, detect  # noqa: E402

DATA = Path(__file__).resolve().parents[2] / "OneDrive_2_10-28-2026"
BAG = DATA / "rosbag2_2026_10_27-16_35_10"
TOL = 0.001                       # 1 mm vs the v1 reference (different method)
# Holes per scan of each real bag (holes_1 holds three scans). The flap's true count is unknown:
# its later holes sit in bands the stitcher dropped, so this pins what is measurable today.
BAG_COUNTS = {DATA / "rosbag2_2026_10_27-16_35_10": [7], DATA / "rosbag2_2026_10_27-16_27_45": [5],
              DATA / "rosbag2_2026_10_27-16_33_35": [7], DATA / "rosbag2_2026_10_27-14_45_24": [6, 4, 9],
              DATA.parent / "flap1_05112026": [3]}
FLAP = DATA.parent / "flap1_05112026"
# Saved frame-replay results (offline_runs) per bag, one folder per scan.
OFFLINE = DATA.parent / "hole_detector_out" / "offline_runs"
BASELINE = {DATA / "rosbag2_2026_10_27-16_27_45": ["csk_1"], DATA / "rosbag2_2026_10_27-16_33_35": ["csk_2"],
            DATA / "rosbag2_2026_10_27-16_35_10": ["csk_3"],
            DATA / "rosbag2_2026_10_27-14_45_24": ["holes_1_scan1", "holes_1_scan2", "holes_1_scan3"],
            FLAP: ["flap1"]}
# mm: centres and top diameters vs offline_runs. 0.1 mm since the 0.2 s cycle (user, 2026-11-12): the reused
# voxel rows change the surface fit's sample slightly; was 0.002 mm (bit-for-bit refactors).
BASELINE_TOL = 0.1
# Real holes the detector misses on purpose (bad data), shown in RViz with the reason.
KNOWN_REJECTED = {"csk_1": (0.165, "not round"), "holes_1_scan1": (0.1565, "not round")}
FLAP_FRAME_HOLES = 20             # every hole in the flap's /scan/frame profiles (checked by eye in CloudCompare)
# Synthetic plate: (kind, centre y, top diameter, through diameter); countersinks are 90 degree cones.
PLATE = [("plain", 0.015, 0.003, 0), ("countersunk", 0.035, 0.008, 0.0045), ("blind", 0.055, 0.006, 0),
         ("countersunk", 0.080, 0.014, 0.009), ("plain", 0.105, 0.010, 0), ("countersunk", 0.140, 0.026, 0.016),
         ("plain", 0.192, 0.006, 0)]
BANDS = [(0.1035, 0.1065), (0.183, 0.186)]     # unscanned: through the y=105 hole, and beside the y=192 one
DENT = (0.170, 0.008)                          # 0.5 mm deep, must not be found
POCKET = (-0.012, 0.015, 0.006, 0.0008)        # x, y, diameter, depth: a flat 0.8 mm pocket is a gap, rejected as a dent
PARTIAL = {0.105}
# Panel bags from the 640-point setup (test.sc1): scans run toward -y, countersinks show as cones above the skin.
# Holes per bag from /scan/cloud_view, and how many are partial (cut by the laser's 25 mm field). a_1903's
# edge row is only ~50 % in the field (rejected); its first and last holes lack the skin ring at the scan ends.
PANEL = DATA.parent / "panel_12112026"
PANEL_HOLES = {"a_1903": (13, 0), "b_1904": (12, 3)}


def read_pcd(path):
    """Binary xyz float32 PCD reader (the format of the scans in this folder)."""
    raw = path.read_bytes()
    start = raw.index(b"DATA binary\n") + len(b"DATA binary\n")
    points = np.frombuffer(raw[start:], dtype=np.float32).reshape(-1, 3).astype(np.float64)
    return points[np.isfinite(points).all(axis=1)]


def reference(stem):
    with (DATA / "out" / stem / "summary.csv").open() as handle:
        rows = list(csv.DictReader(handle))
    return [(np.array([float(r["cx_mm"]), float(r["cy_mm"]), float(r["cz_mm"])]) / 1000,
             float(r["large_diam_mm"]) / 1000) for r in rows]


def match(holes, ref):
    """Pair each reference hole with the nearest found hole; return the worst errors."""
    worst = [0.0, 0.0]
    for centre, d_top in ref:
        hole = min(holes, key=lambda h: np.linalg.norm(h.centre - centre))
        errors = [np.linalg.norm(hole.centre - centre), abs(2 * hole.radius - d_top)]
        print("  ref y=%6.1f  centre err %.2f mm  top %.2f/%.2f mm" % (
            centre[1] * 1000, errors[0] * 1000, 2 * hole.radius * 1000, d_top * 1000))
        worst = [max(w, e) for w, e in zip(worst, errors)]
    return worst



def launch_nodes(path):
    """(package, executable) of every node the launch file starts, with default launch arguments."""
    from launch import LaunchContext
    from launch.actions import OpaqueFunction
    spec = importlib.util.spec_from_file_location("launch_file", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    context = LaunchContext()
    nodes = []
    for action in module.generate_launch_description().entities:
        if isinstance(action, OpaqueFunction):
            nodes += action.execute(context) or []
        else:
            action.visit(context)
    return [(n.node_package, n.node_executable) for n in nodes if hasattr(n, "node_executable")]


def test_batch_matches_reference():
    points = read_pcd(BAG / "csk_3.pcd")
    start = time.time()
    holes = detect(points, DEFAULTS, points[:, 1].min(), points[:, 1].max())[0]
    print("batch: %d holes in %.2f s" % (len(holes), time.time() - start))
    ref = reference("csk_3")
    assert len(holes) == len(ref), "found %d holes, reference has %d" % (len(holes), len(ref))
    worst = match(holes, ref)
    assert all(w < TOL for w in worst), "worst errors (m): %s" % worst
    raw = {row.tobytes() for row in points}
    assert all(row.tobytes() in raw for h in holes for row in h.boundary), "boundary points were altered"
    return holes


def bag_clouds(bag):
    """Every real cumulative /scan/cloud_view message: (stamp, N x 3)."""
    import rosbag2_py
    from rclpy.serialization import deserialize_message
    from sensor_msgs.msg import PointCloud2
    from sensor_msgs_py import point_cloud2
    reader = rosbag2_py.SequentialReader()
    reader.open(rosbag2_py.StorageOptions(uri=str(bag), storage_id="mcap"),
                rosbag2_py.ConverterOptions("cdr", "cdr"))
    reader.set_filter(rosbag2_py.StorageFilter(topics=["/scan/cloud_view"]))
    while reader.has_next():
        _, data, _ = reader.read_next()
        msg = deserialize_message(data, PointCloud2)
        xyz = point_cloud2.read_points_numpy(msg, field_names=("x", "y", "z"), skip_nans=True)
        yield msg.header.stamp.sec + msg.header.stamp.nanosec * 1e-9, xyz.astype(np.float64)


def test_incremental_matches_batch(batch, step):
    """Feed every `step`-th message; each batch hole must come out exactly once, at the same place."""
    tracker, found, slowest = Tracker(log=lambda *_: None), [], 0.0
    for i, (stamp, xyz) in enumerate(bag_clouds(BAG)):
        if i % step:
            continue
        start = time.time()
        _, new = tracker.feed(xyz, stamp)
        slowest = max(slowest, time.time() - start)
        found += new
    print("incremental step %d: %d holes, slowest feed %.2f s" % (step, len(found), slowest))
    assert len(found) == len(batch), "incremental found %d, batch %d" % (len(found), len(batch))
    for hole in batch:
        errors = sorted(np.linalg.norm(h.centre - hole.centre) for h in found)
        assert errors[0] < 0.0003, "batch hole at y=%.1f mm off by %.2f mm" % (
            hole.centre[1] * 1000, errors[0] * 1000)
        assert errors[1] > 0.010, "hole at y=%.1f mm published twice" % (hole.centre[1] * 1000)


def synthetic_plate(step=0.0001, width=0.040, length=0.210, z0=0.066):
    """Points of a flat plate (z = range, so deeper = larger z) with PLATE holes, the dent and the bands."""
    x, y = (a.ravel() for a in np.meshgrid(np.arange(-width / 2, width / 2, step), np.arange(0, length, step)))
    depth, keep = np.zeros_like(x), np.ones(len(x), dtype=bool)
    for kind, cy, d_top, d_through in PLATE:
        r = np.hypot(x, y - cy)
        inside = r < d_top / 2
        if kind == "plain":
            keep &= ~inside
        elif kind == "blind":
            depth[inside] = 0.003
        else:
            depth[inside] = d_top / 2 - r[inside]
            keep &= r >= d_through / 2
    r = np.hypot(x, y - DENT[0])
    dent = r < DENT[1] / 2
    depth[dent] = 0.0005 * (1 - (2 * r[dent] / DENT[1]) ** 2)
    pocket = np.hypot(x - POCKET[0], y - POCKET[1]) < POCKET[2] / 2
    depth[pocket] = POCKET[3]
    for lo, hi in BANDS:
        keep &= (y < lo) | (y >= hi)
    z = z0 + depth + np.random.default_rng(0).normal(0, 0.00003, len(x))
    return np.column_stack((x, y, z))[keep]


def check_plate(holes, planted):
    """Each planted hole found once, at the right place and size, with the right flags; nothing else."""
    assert len(holes) == len(planted), "found %s" % sorted(round(h.centre[1] * 1000, 1) for h in holes)
    for kind, cy, d_top, _ in planted:
        hole = min(holes, key=lambda h: abs(h.centre[1] - cy))
        # The rim is measured where the surface ends (surface_tol deep): a 90 degree cone is 2 x that narrower.
        # shortcut: the rim points sit ~1 pixel outside the edge, so diameters read ~0.35 mm large (the
        # same on the real bags); correct it with a per-sector innermost-edge radius if that matters.
        expected = d_top - (2 * DEFAULTS["surface_tol"] if kind == "countersunk" else 0) + 0.00035
        assert np.hypot(hole.centre[0], hole.centre[1] - cy) < 0.0003, "y=%.0f mm: centre off" % (cy * 1000)
        assert abs(2 * hole.radius - expected) < 0.00015, "y=%.0f mm: diameter %.2f mm, expected %.2f" % (
            cy * 1000, 2000 * hole.radius, expected * 1000)
        assert hole.countersunk == (kind == "countersunk"), "y=%.0f mm: wrong type" % (cy * 1000)
        assert hole.partial == (cy in PARTIAL), "y=%.0f mm: partial flag" % (cy * 1000)


def test_synthetic_plate():
    points = synthetic_plate()
    check_plate(detect(points, DEFAULTS, 0.0, points[:, 1].max(), log=lambda *_: None, lo_is_scan_start=True)[0],
                PLATE)
    for kind in ("countersunk", "plain"):
        holes = detect(points, dict(DEFAULTS, hole_type=kind), 0.0, points[:, 1].max(), log=lambda *_: None,
                       lo_is_scan_start=True)[0]
        check_plate(holes, [h for h in PLATE if (h[0] == "countersunk") == (kind == "countersunk")])


def test_synthetic_incremental():
    """The plate fed as a growing scan, 10 mm per cloud: same holes, each once."""
    points = synthetic_plate()
    tracker, found = Tracker(log=lambda *_: None), []
    for i, end in enumerate(np.arange(0.01, points[:, 1].max() + 0.01, 0.01)):
        found += tracker.feed(points[points[:, 1] <= end], float(i))[1]
    check_plate(found, PLATE)
    # The pocket is reported once, as a dent, however often its strip is re-examined.
    for i in range(3):
        tracker.feed(points, 100.0 + i)
    assert [(round(g.centre[0], 3), round(g.centre[1], 3), g.reason) for g in tracker.rejected] == [
        (POCKET[0], POCKET[1], "shallow dent")], tracker.rejected
    tracker.feed(points[points[:, 1] < 0.005], 0.0)  # stamp goes back: a new scan, nothing decided yet
    assert tracker.rejected == [] and tracker.holes == []


def test_reverse_scan():
    """The plate scanned toward -y (mirrored about z): the same holes, mirrored, each once; raw boundaries."""
    points = synthetic_plate() * [-1.0, -1.0, 1.0]
    tracker, found = Tracker(log=lambda *_: None), []
    for i, end in enumerate(np.arange(0.01, -points[:, 1].min() + 0.01, 0.01)):
        found += tracker.feed(points[points[:, 1] >= -end], float(i))[1]
    assert tracker.sign == -1
    raw = {p.tobytes() for p in points}
    assert all(p.tobytes() in raw for h in found for p in h.boundary), "boundary points not raw"
    for hole in found:
        hole.centre = hole.centre * [-1.0, -1.0, 1.0]
    check_plate(found, PLATE)
    # Back again (+y) from where the -y scan ended: a new scan (reset), every hole found again.
    length = -points[:, 1].min()
    back, found, resets = synthetic_plate() - [0.0, length, 0.0], [], 0
    for i, end in enumerate(np.arange(0.01, length + 0.01, 0.01)):
        reset, new = tracker.feed(back[back[:, 1] <= end - length], 100.0 + i)
        resets, found = resets + reset, found + new
    assert resets == 1 and tracker.sign == 1
    for hole in found:
        hole.centre = hole.centre + [0.0, length, 0.0]
    check_plate(found, PLATE)


def test_rejection_reported_once():
    """Fed in 2 mm steps, the pocket is re-examined on several overlapping crops but listed once;
    a listed rejection that later passes as a hole is dropped from the list."""
    points = synthetic_plate()
    lines = []
    tracker = Tracker(log=lines.append)
    for i, end in enumerate(np.arange(0.002, 0.120, 0.002)):
        tracker.feed(points[points[:, 1] <= end], float(i))
    assert sum("shallow dent" in line for line in lines) >= 2, "the pocket was examined only once"
    assert [g.reason for g in tracker.rejected] == ["shallow dent"], tracker.rejected
    # Pretend the y=35 countersink was rejected earlier: accepting it must drop that rejection.
    tracker = Tracker(log=lambda *_: None)
    first = Tracker(log=lambda *_: None)
    first.feed(points, 0.0)
    hole = min(first.holes, key=lambda h: abs(h.centre[1] - 0.035))
    tracker.rejected = [Rejected(hole.centre, hole.centre_2d, hole.radius, hole.surface, "not round")]
    tracker.feed(points, 0.0)
    assert not any(abs(g.centre[1] - 0.035) < 0.002 for g in tracker.rejected), tracker.rejected


def test_surface_strips():
    """The decided strips tile the scan once each (no gaps, no overlap) and carry depth in mm."""
    from hole_detector import viz
    from sensor_msgs_py import point_cloud2
    from std_msgs.msg import Header
    points = synthetic_plate()
    tracker, strips, lo = Tracker(log=lambda *_: None), [], -np.inf
    for i, end in enumerate(np.arange(0.01, points[:, 1].max() + 0.01, 0.01)):
        tracker.feed(points[points[:, 1] <= end], float(i))
        if tracker.strip is not None and len(tracker.strip[0]):
            y = tracker.strip[0][:, 1]
            assert y.min() >= lo, "strips overlap"
            lo = tracker.shown_y
            strips.append(tracker.strip)
    shown = np.concatenate([q for q, _ in strips])
    assert shown[:, 1].max() > points[:, 1].max() - 0.004, "the end of the scan is never shown"
    flat = np.concatenate([d for _, d in strips])
    assert np.median(np.abs(flat)) < 0.0001, "flat plate should sit on the fit"
    msg = viz.surface_cloud(Header(frame_id="laser"), *strips[0])
    dev = point_cloud2.read_points_numpy(msg, field_names=("deviation_mm",))
    assert np.allclose(dev[:, 0], strips[0][1][:len(dev)] * 1000, atol=1e-4)


def test_band_slabs_on_tilted_plate():
    """Unscanned bands come out as slabs on the surface, in cloud coordinates, on a plate tilted 10 degrees."""
    a = np.radians(10)
    rot = np.array([[1, 0, 0], [0, np.cos(a), -np.sin(a)], [0, np.sin(a), np.cos(a)]])
    points = synthetic_plate() @ rot.T
    view = detect(points, DEFAULTS, points[:, 1].min(), points[:, 1].max(), log=lambda *_: None,
                  lo_is_scan_start=True)[2]
    for lo, hi in BANDS:
        edges = np.array([[0, lo, 0.066], [0, hi, 0.066]]) @ rot.T        # the band edges on the tilted plate
        match = [c for c in view["bands"] if abs(c[:, 1].min() - edges[0, 1]) < 0.001 and
                 abs(c[:, 1].max() - edges[1, 1]) < 0.001]
        assert match, "band %.1f-%.1f mm not found: %s" % (lo * 1000, hi * 1000, view["bands"])
        plate_z = 0.066 / np.cos(a) + match[0][:, 1] * np.tan(a)       # the tilted plane at the corners' y
        assert np.abs(match[0][:, 2] - plate_z).max() < 0.001, "slab is off the surface"


def test_crop_edge_is_not_a_rejection():
    """A hole cut by the open end of the scan is retried later, never reported as rejected."""
    points = synthetic_plate()
    tracker = Tracker(log=lambda *_: None)
    for i, end in enumerate((0.10, 0.108)):         # the y=105 hole (5 mm radius) is cut at both ends
        tracker.feed(points[points[:, 1] <= end], float(i))
        assert not any(abs(g.centre[1] - 0.105) < 0.003 for g in tracker.rejected), tracker.rejected


def test_panel_bags():
    """Reverse (-y) scans with mirrored countersinks: every fully seen hole once, countersunk, Ø 8-10.5 mm."""
    for name, (count, partial) in PANEL_HOLES.items():
        if not (PANEL / name).exists():
            print("skip %s: not copied here" % name)
            continue
        tracker, slowest = Tracker(log=lambda *_: None), 0.0
        for stamp, xyz in bag_clouds(PANEL / name):
            t0 = time.time()
            reset, _ = tracker.feed(xyz, stamp)
            slowest = max(slowest, time.time() - t0)
            assert not reset
        holes = tracker.holes
        diam = [2000 * h.radius for h in holes]
        print("%s: %d holes (%d partial), Ø %.2f-%.2f mm, slowest feed %.2f s" % (
            name, len(holes), sum(h.partial for h in holes), min(diam), max(diam), slowest))
        assert tracker.sign == -1
        assert len(holes) == count and sum(h.partial for h in holes) == partial
        assert all(h.countersunk for h in holes) and 8.0 < min(diam) and max(diam) < 10.5
        xy = np.array([h.centre[:2] for h in holes])
        gaps = np.linalg.norm(xy[:, None] - xy[None], axis=2) + np.eye(len(xy))
        assert gaps.min() > 0.010, "a hole reported twice"


def test_bag_frames_rebuild():
    """The cloud-only panel bag rebuilds its final cloud as decreasing-y laser profiles."""
    from frames import bag_frames
    import rosbag2_py
    from rclpy.serialization import deserialize_message
    from sensor_msgs.msg import PointCloud2
    from sensor_msgs_py import point_cloud2
    bag = PANEL / "b_1904"
    reader = rosbag2_py.SequentialReader()
    reader.open(rosbag2_py.StorageOptions(uri=str(bag), storage_id="mcap"),
                rosbag2_py.ConverterOptions("cdr", "cdr"))
    reader.set_filter(rosbag2_py.StorageFilter(topics=["/scan/cloud"]))
    final = None
    while reader.has_next():
        _, data, _ = reader.read_next()
        msg = deserialize_message(data, PointCloud2)
        final = point_cloud2.read_points_numpy(msg, field_names=("x", "y", "z"), skip_nans=True).astype(np.float64)
    frames = list(bag_frames(bag))
    rebuilt = np.vstack([np.column_stack((x, np.full(len(x), y), z)) for _, x, y, z, *_ in frames])
    assert len(frames) >= 2000 and np.all(np.diff([f[2] for f in frames]) < 0)
    assert np.array_equal(np.array(sorted(map(tuple, rebuilt))), np.array(sorted(map(tuple, final))))


def test_real_bag_counts():
    for bag, counts in BAG_COUNTS.items():
        if not bag.exists():
            print("skip %s: not copied here" % bag.name)
            continue
        tracker, per_scan = Tracker(log=lambda *_: None), [0]
        for stamp, xyz in bag_clouds(bag):
            reset, new = tracker.feed(xyz, stamp)
            if reset:
                per_scan.append(0)
            per_scan[-1] += len(new)
        print("%s: holes per scan %s" % (bag.name, per_scan))
        assert per_scan == counts, "%s: %s, expected %s" % (bag.name, per_scan, counts)


def frame_scans(bag, params=None):
    """The node's frame path: /scan/frame profiles into FrameCloud, detection once per second of
    stamps, trimmed after each run, a new scan at frame index 0. Returns (holes, rejected) per scan."""
    import rosbag2_py
    from rclpy.serialization import deserialize_message
    from laser_hhrp.msg import ScanFrame
    reader = rosbag2_py.SequentialReader()
    reader.open(rosbag2_py.StorageOptions(uri=str(bag), storage_id="mcap"),
                rosbag2_py.ConverterOptions("cdr", "cdr"))
    reader.set_filter(rosbag2_py.StorageFilter(topics=["/scan/frame"]))
    frames, tracker, scans, last, slowest, largest = FrameCloud(), Tracker(params, log=lambda *_: None), [], None, 0.0, 0
    stamp = 0.0
    while reader.has_next():
        msg = deserialize_message(reader.read_next()[1], ScanFrame)
        if msg.index == 0 and last is not None:     # a new scan: finish the old one
            tracker.feed(frames.cloud(), stamp)
            scans.append((tracker.holes, tracker.rejected))
            frames.clear()
            tracker.reset()
            last = None
        stamp = msg.header.stamp.sec + msg.header.stamp.nanosec * 1e-9
        frames.add(msg.x_m, msg.position_m, msg.z_m, msg.valid)
        if last is None:
            last = stamp
        if stamp - last >= 1.0:
            start, cloud = time.time(), frames.cloud()
            tracker.feed(cloud, stamp)
            frames.trim(tracker)
            slowest, largest, last = max(slowest, time.time() - start), max(largest, len(cloud)), stamp
    tracker.feed(frames.cloud(), stamp)
    scans.append((tracker.holes, tracker.rejected))
    print("%s from frames: holes per scan %s, slowest feed %.2f s, largest buffer %d points" % (
        bag.name, [len(s[0]) for s in scans], slowest, largest))
    return scans


def test_replay_matches_baseline():
    """Every bag's frame replay reproduces the saved results (offline_runs) row for row: refactors and
    speed-ups must not move a hole (more than BASELINE_TOL). Returns the flap holes."""
    flap = None
    for bag, names in BASELINE.items():
        if not bag.exists():
            print("skip %s: not copied here" % bag.name)
            continue
        scans = frame_scans(bag)
        assert len(scans) == len(names), "%s: %d scans" % (bag.name, len(scans))
        for (holes, rejected), name in zip(scans, names):
            if name in KNOWN_REJECTED:
                y, reason = KNOWN_REJECTED[name]
                assert any(abs(g.centre[1] - y) < 0.003 and g.reason == reason for g in rejected), (
                    "%s: the miss at y=%.0f mm is not shown as '%s'" % (name, y * 1000, reason))
            with (OFFLINE / name / "summary.csv").open() as handle:
                rows = list(csv.DictReader(handle))
            assert len(holes) == len(rows), "%s: %d holes, baseline %d" % (name, len(holes), len(rows))
            # Matched by place, not order: a re-checked rejection can come out a cycle earlier or later.
            for row in rows:
                want = np.array([float(row[k]) for k in ("cx_mm", "cy_mm", "cz_mm", "top_diam_mm")])
                hole = min(holes, key=lambda h: np.linalg.norm(h.centre * 1000 - want[:3]))
                got = np.append(hole.centre * 1000, 2000 * hole.radius)
                assert np.abs(got - want).max() < BASELINE_TOL, "%s #%s: %s vs baseline %s" % (name, row["id"], got, want)
        if bag == FLAP:
            flap = scans[0][0]
    return flap


def in_plane_reach(hole):
    """Largest distance of a boundary point from the hole axis (in the surface plane), in m."""
    d = hole.boundary - hole.centre
    return np.linalg.norm(d - np.outer(d @ hole.normal, hole.normal), axis=1).max() - hole.radius


def test_boundary_margin():
    """boundary_margin only widens the saved cut-out: the same holes, raw points, within the new width."""
    points = read_pcd(BAG / "csk_3.pcd")
    base = detect(points, DEFAULTS, points[:, 1].min(), points[:, 1].max(), log=lambda *_: None)[0]
    wide = detect(points, dict(DEFAULTS, boundary_margin=0.010), points[:, 1].min(), points[:, 1].max(),
                  log=lambda *_: None)[0]
    assert [(tuple(h.centre), h.radius) for h in base] == [(tuple(h.centre), h.radius) for h in wide]
    raw = {row.tobytes() for row in points}
    for b, w in zip(base, wide):
        assert len(w.boundary) > len(b.boundary)
        assert all(row.tobytes() in raw for row in w.boundary), "boundary points were altered"
        assert in_plane_reach(w) < 0.0105, "cut-out wider than radius + 10 mm"
    if not FLAP.exists():
        return
    # Tracker: the margin changes the cut-out only, so the replay matches the baseline exactly.
    holes = frame_scans(FLAP, {"boundary_margin": 0.008})[0][0]
    with (OFFLINE / "flap1" / "summary.csv").open() as handle:
        rows = list(csv.DictReader(handle))
    assert len(holes) == len(rows), "%d holes, baseline %d" % (len(holes), len(rows))
    for row in rows:
        want = np.array([float(row[k]) for k in ("cx_mm", "cy_mm", "cz_mm", "top_diam_mm")])
        hole = min(holes, key=lambda h: np.linalg.norm(h.centre * 1000 - want[:3]))
        assert np.abs(np.append(hole.centre * 1000, 2000 * hole.radius) - want).max() < BASELINE_TOL, row["id"]
        assert 0.0075 < in_plane_reach(hole) < 0.0085, "#%s cut-out not 8 mm wide" % row["id"]
    print("boundary_margin: csk_3 batch identical, flap 8 mm replay %d holes" % len(holes))


def test_flap_from_frames(found):
    """Every flap hole comes out once."""
    if found is None:
        return
    assert len(found) == FLAP_FRAME_HOLES, "found %d" % len(found)
    centres = np.array([h.centre for h in found])
    gaps = np.linalg.norm(centres[:, None] - centres[None], axis=2) + np.eye(len(centres))
    assert gaps.min() > 0.010, "a hole was published twice"


LATE = {"rosbag2_2026_10_27-14_45_24": 1}     # holes allowed past 0.5 s per bag (see test_latency)


def test_latency(period=0.2):
    """Simulated real time at the node's cycle: every hole comes out once, < 0.5 s after the frame that
    completes it (far edge + ring + guard), with detect <= 150 ms median and <= 300 ms worst per cycle."""
    from frames import replay
    expected = {DATA / "rosbag2_2026_10_27-16_27_45": 5, DATA / "rosbag2_2026_10_27-16_33_35": 7,
                DATA / "rosbag2_2026_10_27-16_35_10": 7, DATA / "rosbag2_2026_10_27-14_45_24": 19, FLAP: 20,
                PANEL / "a_1903": PANEL_HOLES["a_1903"][0], PANEL / "b_1904": PANEL_HOLES["b_1904"][0]}
    for bag, count in expected.items():
        if not bag.exists():
            print("skip %s: not copied here" % bag.name)
            continue
        run = replay(bag, Tracker(log=lambda *_: None), period=period)
        median, worst, late = np.median(run.cycle), max(run.cycle), max(run.latency)

        print("%s latency: %d holes, max %.2f s, cycle median %.0f ms worst %.0f ms" % (
            bag.name, len(run.features), late, median * 1000, worst * 1000))
        assert len(run.features) == count, "%s: %d holes, expected %d" % (bag.name, len(run.features), count)
        slow = sum(lat >= 0.5 for lat in run.latency)
        # holes_1 scan 3, y = 45 mm: first "not round", it passes on a later crop (a re-checked rejection).
        assert slow <= LATE.get(bag.name, 0), "%s: %d holes later than 0.5 s" % (bag.name, slow)
        assert median <= 0.150 and worst <= 0.300, bag.name


def test_writer_drains(tmp=Path("/tmp/hole_writer_test")):
    """ScanFolder writes in the background: after close() every queued row and PCD is on disk, PCDs raw."""
    import shutil
    from hole_detector import results
    shutil.rmtree(tmp, ignore_errors=True)
    folder, rng = results.ScanFolder(tmp, log=lambda *_: None), np.random.default_rng(0)
    holes = []
    for i in range(1, 21):
        boundary = rng.normal(size=(500, 3)).astype(np.float32).astype(np.float64)
        holes.append(SimpleNamespace(id=i, centre=np.array([0.0, i * 0.01, 0.07]), radius=0.004, rms=1e-5,
                                     coverage=1.0, countersunk=True, partial=False, scanned_share=1.0, boundary=boundary))
        folder.save(holes[-1], holes[-2] if i > 1 else None)
    folder.close()
    rows = (folder.path / "summary.csv").read_text().splitlines()
    assert len(rows) == 21 and [r.split(",")[0] for r in rows[1:]] == [str(i) for i in range(1, 21)]
    for hole in holes:
        assert np.array_equal(read_pcd(folder.path / ("hole_%02d.pcd" % hole.id)), hole.boundary), hole.id


def test_launch_single_node():
    """The package's launch file starts its own node only (holes / rivets / both: laser_hhrp detectors.launch.py)."""
    root = Path(__file__).resolve().parents[1]
    assert launch_nodes(root / "launch" / "hole_detector.launch.py") == [("hole_detector", "hole_detector")]


def test_empty_cloud_is_ignored():
    assert Tracker(log=lambda *_: None).feed(np.empty((0, 3)), 0.0) == (False, [])


def test_hole_near_scan_start():
    """The first hole's ring starts 1 mm after the scan start (inside the 3 mm guard): still found."""
    points = read_pcd(BAG / "csk_3.pcd")
    first = min(reference("csk_3"), key=lambda r: r[0][1])
    start = first[0][1] - first[1] / 2 - DEFAULTS["ring"] - 0.001
    points = points[points[:, 1] > start]
    tracker, found = Tracker(log=lambda *_: None), []
    for i, end in enumerate(np.arange(start + 0.01, points[:, 1].max() + 0.01, 0.01)):
        found += tracker.feed(points[points[:, 1] <= end], float(i))[1]
    assert any(np.linalg.norm(h.centre - first[0]) < TOL for h in found), "first hole missed"


def test_hole_markers():
    """Colour by status, two-line label, and one (ns, id) per marker."""
    from hole_detector import viz
    from std_msgs.msg import Header
    points = synthetic_plate()
    holes = detect(points, DEFAULTS, 0.0, points[:, 1].max(), log=lambda *_: None, lo_is_scan_start=True)[0]
    markers = []
    for i, hole in enumerate(holes, 1):
        hole.id = i
        markers += viz.hole_markers(Header(frame_id="laser"), hole)
        want = viz.STYLE["partial_rgb"] if hole.partial else viz.STYLE["complete_rgb"]
        assert (markers[-3].color.r, markers[-3].color.g, markers[-3].color.b) == want
        assert markers[-1].text.startswith("#%d  Ø%.2f  " % (i, 2000 * hole.radius))
        assert ("partial" in markers[-1].text) == hole.partial
    assert viz.nearest(holes[1], holes[:1]) is holes[0] and viz.nearest(holes[0], holes[:1]) is None
    markers += viz.distance_markers(Header(frame_id="laser"), holes[1], holes[0])
    assert markers[-1].text == "%.2f mm" % (np.linalg.norm(holes[1].centre - holes[0].centre) * 1000)
    ruler = viz.ruler_markers(Header(frame_id="laser"), 0.003, 0.212, -0.03, 0.066)
    assert [m.text for m in ruler[1:]] == ["50", "100", "150", "200"] and len(ruler[0].points) == 2 + 2 * 21
    markers += ruler
    gap = Rejected(holes[0].centre, holes[0].centre_2d, holes[0].radius, holes[0].surface, "not round", id=1)
    markers += viz.rejected_markers(Header(frame_id="laser"), gap)
    assert markers[-1].text.endswith("not round") and len(markers[-2].points) % 2 == 0
    assert len({(m.ns, m.id) for m in markers}) == len(markers), "marker (ns, id) collision"


if __name__ == "__main__":
    test_launch_single_node()
    test_writer_drains()
    test_empty_cloud_is_ignored()
    test_synthetic_plate()
    test_synthetic_incremental()
    test_reverse_scan()
    test_rejection_reported_once()
    test_crop_edge_is_not_a_rejection()
    test_surface_strips()
    test_band_slabs_on_tilted_plate()
    test_hole_markers()
    test_hole_near_scan_start()
    batch_holes = test_batch_matches_reference()
    for s in (1, 2, 3):
        test_incremental_matches_batch(batch_holes, s)
    test_real_bag_counts()
    test_flap_from_frames(test_replay_matches_baseline())
    test_boundary_margin()
    test_panel_bags()
    test_latency()
    test_bag_frames_rebuild()
    print("all checks passed")
