"""Offline checks for the rivet detector: a synthetic panel, the panel 2 bags replayed from /scan/frame,
raw boundaries, NaN-free results and the launch file.

Run in the ROS container:  python3 /bags/rivet_detector/test/test_rivets.py
"""

import importlib.util
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from rivet_detector import results  # noqa: E402
from rivet_detector.detector import DEFAULTS, FrameCloud, Tracker, detect  # noqa: E402

BAGS = ROOT.parent
# Rivets per panel 2 bag (frame replay), and which ids are partial. Each bag holds 10 heads in its
# middle row. panel2_06's last one (y ~ 188) sits within guard + ring of the scan's end, so it is never
# decided (as for holes at the scan end). panel2_09's #8 is cut by an unscanned band.
PANEL2 = {"panel2_06112026_1": (9, set()), "panel2_09112026_02": (10, {8})}
PITCH = (0.0195, 0.0215)          # neighbouring heads, along the row
# The same kind of panel scanned with the 640-point setup (test.sc1): heads seen ~0.1 mm *below* the skin, scan
# toward -y (frames rebuilt from /scan/cloud). b_1904: 9 heads in its middle column (the one at y = -172 mm is at
# the laser's field edge: partial) and a 10th on the panel's end step, past what can be decided. a_1903: holes only.
PANEL = BAGS / "panel_12112026"
PANEL_RIVETS = {"a_1903": 0, "b_1904": 9}
# Synthetic panel: rivet heads (x, y, height, pit); a band through the last one.
HEADS = [(0.0, 0.015, 0.00015, False), (0.0, 0.035, 0.00015, True), (0.0, 0.055, 0.00007, True),
         (0.0, 0.075, 0.00015, False)]
BAND = (0.0745, 0.0760)           # unscanned scan lines through the y=75 head: partial
HOLES = [(0.010, 0.025), (-0.010, 0.025), (0.010, 0.045), (-0.010, 0.065)]   # countersunk Ø7.5, ignored
SMALL_BUMP = (-0.007, 0.045, 0.005, 0.00015)    # x, y, diameter, height: not a rivet


def synthetic_panel(step=0.00005, width=0.025, length=0.090, z0=0.066, seed=0):
    """A 25 mm strip like panel 2: a gently sloped skin with noise, Ø9 heads proud of it (some with a
    central pit), countersunk holes beside them, a small bump and an unscanned band."""
    rng = np.random.default_rng(seed)
    x, y = np.meshgrid(np.arange(-width / 2, width / 2, step), np.arange(0, length, step))
    x, y = x.ravel(), y.ravel()
    z = z0 + 0.005 * y + 0.002 * x + rng.normal(0, 0.00001, x.size)     # slope 0.05 mm per 10 mm
    keep = np.ones(x.size, bool)
    for hx, hy, height, pit in HEADS:
        r = np.hypot(x - hx, y - hy)
        z[r < 0.0045] -= height                                  # z = range: proud = nearer the sensor
        if pit:
            z[r < 0.0008] += 0.00008
    bx, by, bd, bh = SMALL_BUMP
    z[np.hypot(x - bx, y - by) < bd / 2] -= bh
    for hx, hy in HOLES:
        r = np.hypot(x - hx, y - hy)
        cone = (r < 0.00375) & (r >= 0.002)
        z[cone] += 0.00375 - r[cone]                             # 90 degree countersink
        keep &= r >= 0.002                                       # through
    keep &= (y < BAND[0]) | (y > BAND[1])
    return np.column_stack((x, y, z))[keep]


def check_panel(rivets):
    found = sorted(rivets, key=lambda r: r.centre[1])
    assert len(found) == len(HEADS), "found %d heads, expected %d: %s" % (
        len(found), len(HEADS), [round(r.centre[1] * 1000, 1) for r in found])
    for rivet, (hx, hy, height, _) in zip(found, HEADS):
        assert np.hypot(rivet.centre[0] - hx, rivet.centre[1] - hy) < 0.0003, rivet.centre
        assert abs(2 * rivet.radius - 0.009) < 0.0004, 2 * rivet.radius
        assert abs(rivet.height - height) < 0.00003, (rivet.height, height)
        assert rivet.partial == (BAND[0] < hy + 0.0045 and hy - 0.0045 < BAND[1]), rivet



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


def test_synthetic_batch():
    points = synthetic_panel()
    rivets, rejected, _ = detect(points, dict(DEFAULTS), points[:, 1].min(), points[:, 1].max(),
                                 log=lambda *_: None, lo_is_scan_start=True)
    check_panel(rivets)
    return points


def test_side_clip(points):
    """Heads cut by the part's side (here the strip ends 2 mm right of their centres) are not accepted."""
    clipped = points[points[:, 0] < 0.002]
    rivets, _, _ = detect(clipped, dict(DEFAULTS), clipped[:, 1].min(), clipped[:, 1].max(),
                          log=lambda *_: None, lo_is_scan_start=True)
    assert not rivets, [round(r.centre[1] * 1000, 1) for r in rivets]


def test_synthetic_incremental(points):
    """Fed 2 mm at a time through FrameCloud and the Tracker, as the node does: the same heads, once each."""
    tracker, frames = Tracker(log=lambda *_: None), FrameCloud()
    order = np.argsort(points[:, 1], kind="stable")
    points = points[order]
    start = 0
    for k, y in enumerate(np.arange(0.002, points[-1, 1] + 0.004, 0.002)):
        end = np.searchsorted(points[:, 1], y, side="right")
        frames.chunks.append(points[start:end])
        start = end
        tracker.feed(frames.cloud(), float(k))
        frames.trim(tracker)
    check_panel(tracker.rivets)
    assert [r.id for r in tracker.rivets] == list(range(1, len(HEADS) + 1))
    return tracker.rivets


def test_boundary_is_raw(points, rivets):
    """Boundary clouds are raw input rows (never downsampled or moved), within edge + boundary_margin."""
    raw = {tuple(row) for row in np.round(points, 9)}
    for rivet in rivets:
        rows = np.round(rivet.boundary, 9)
        assert len(rows) > 1000 and all(tuple(row) in raw for row in rows[:: max(1, len(rows) // 2000)])
        reach = np.hypot(rivet.boundary[:, 0] - rivet.centre[0], rivet.boundary[:, 1] - rivet.centre[1]).max()
        assert reach <= rivet.radius + DEFAULTS["boundary_margin"] + 0.0002, reach


def test_results_have_no_nan(rivets):
    """recorder_node writes /inspection/result floats to JSON: never NaN."""
    for rivet in rivets:
        msg = results.inspection_result(SimpleNamespace, None, rivet)
        floats = [msg.countersink_depth_m, msg.flushness_m, msg.surface_deviation_m, *msg.position_m]
        assert all(np.isfinite(floats)), floats
        assert msg.feature_type == "rivet" and json.loads(msg.detail)["id"] == rivet.id


def frame_scans(bag):
    """The node's frame path: /scan/frame into FrameCloud, detection once per second of stamps, trimmed
    after each run, a new scan at frame index 0. Returns the trackers' rivets per scan."""
    import rosbag2_py
    from rclpy.serialization import deserialize_message
    from laser_hhrp.msg import ScanFrame
    reader = rosbag2_py.SequentialReader()
    reader.open(rosbag2_py.StorageOptions(uri=str(bag), storage_id="mcap"),
                rosbag2_py.ConverterOptions("cdr", "cdr"))
    reader.set_filter(rosbag2_py.StorageFilter(topics=["/scan/frame"]))
    frames, tracker, scans, last, stamp, slowest = FrameCloud(), Tracker(log=lambda *_: None), [], None, 0.0, 0.0
    while reader.has_next():
        msg = deserialize_message(reader.read_next()[1], ScanFrame)
        if msg.index == 0 and last is not None:
            tracker.feed(frames.cloud(), stamp)
            scans.append((tracker.rivets, tracker.rejected))
            frames.clear()
            tracker.reset()
            last = None
        stamp = msg.header.stamp.sec + msg.header.stamp.nanosec * 1e-9
        frames.add(msg.x_m, msg.position_m, msg.z_m, msg.valid)
        last = stamp if last is None else last
        if stamp - last >= 1.0:
            t = time.time()
            tracker.feed(frames.cloud(), stamp)
            frames.trim(tracker)
            slowest, last = max(slowest, time.time() - t), stamp
    tracker.feed(frames.cloud(), stamp)
    scans.append((tracker.rivets, tracker.rejected))
    print("%s: rivets per scan %s, slowest feed %.2f s" % (bag.name, [len(s[0]) for s in scans], slowest))
    return scans


def test_panel2_bags():
    for name, (count, partial) in PANEL2.items():
        scans = frame_scans(BAGS / name)
        assert len(scans) == 1, len(scans)
        rivets, rejected = scans[0]
        assert len(rivets) == count, "%s: %d rivets, expected %d" % (name, len(rivets), count)
        assert {r.id for r in rivets if r.partial} == partial, [r.id for r in rivets if r.partial]
        for r in rivets:
            assert 0.0085 <= 2 * r.radius <= 0.0095 and 0.00004 <= r.height <= 0.0003, (r.id, r.radius, r.height)
        ys = np.sort([r.centre[1] for r in rivets])
        steps = np.diff(ys)
        assert np.all((steps > PITCH[0]) & (steps < PITCH[1])), steps     # one row, none missed in between
        # No accepted head is also shown as a rejection, and no head is reported twice.
        assert not any(np.linalg.norm(g.centre[:2] - r.centre[:2]) < 2 * r.radius for g in rejected for r in rivets)


def test_launch_single_node():
    """The package's launch file starts its own node only (holes / rivets / both: laser_hhrp detectors.launch.py)."""
    assert launch_nodes(ROOT / "launch" / "rivet_detector.launch.py") == [("rivet_detector", "rivet_detector")]


def test_panel_bags_generic():
    """Default parameters find the sunken heads of the 640-point panel bags (no polarity setting), once each,
    Ø 9 +- 1 mm, height < 0; a_1903 (holes only, their cones read above the skin) gives none."""
    from frames import replay
    for name, count in PANEL_RIVETS.items():
        if not (PANEL / name).exists():
            print("skip %s: not copied here" % name)
            continue
        run = replay(PANEL / name, Tracker(log=lambda *_: None))
        rivets = run.features
        print("%s: %d rivets, heights %s mm, max latency %.2f s, cycle median %.0f ms worst %.0f ms" % (
            name, len(rivets), [round(r.height * 1000, 2) for r in rivets], max(run.latency, default=0),
            np.median(run.cycle) * 1000, max(run.cycle) * 1000))
        assert len(rivets) == count, "%s: %d rivets, expected %d" % (name, len(rivets), count)
        assert all(r.height < 0 and 0.008 < 2 * r.radius < 0.010 for r in rivets)
        if rivets:
            steps = np.diff(sorted(r.centre[1] for r in rivets))
            assert np.all((steps > PITCH[0]) & (steps < PITCH[1])), steps


def test_synthetic_both_signs(points):
    """The synthetic panel and its mirror image in depth (every head sunk instead of proud) give the same heads,
    with the height's sign flipped; the countersunk holes beside them are never heads, either way."""
    z0 = np.median(points[:, 2])
    for sign, cloud in ((1, points), (-1, np.column_stack((points[:, :2], 2 * z0 - points[:, 2])))):
        rivets, _, _ = detect(cloud, dict(DEFAULTS), cloud[:, 1].min(), cloud[:, 1].max(),
                              log=lambda *_: None, lo_is_scan_start=True)
        found = sorted(rivets, key=lambda r: r.centre[1])
        assert len(found) == len(HEADS), (sign, [round(r.centre[1] * 1000, 1) for r in found])
        for rivet, (hx, hy, height, _) in zip(found, HEADS):
            assert np.hypot(rivet.centre[0] - hx, rivet.centre[1] - hy) < 0.0003, (sign, rivet.centre)
            assert abs(rivet.height - sign * height) < 0.00003, (sign, rivet.height)


if __name__ == "__main__":
    test_launch_single_node()
    panel = test_synthetic_batch()
    test_side_clip(panel)
    test_synthetic_both_signs(panel)
    found = test_synthetic_incremental(panel)
    test_boundary_is_raw(panel, found)
    test_results_have_no_nan(found)
    test_panel2_bags()
    test_panel_bags_generic()
    print("all checks passed")
