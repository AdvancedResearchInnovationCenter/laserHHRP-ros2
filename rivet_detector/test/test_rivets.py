"""Offline checks for the rivet detector: a synthetic panel, the panel 2 bags replayed from /scan/frame,
raw boundaries, NaN-free results and the launch file.

Run in the ROS container:  python3 /bags/rivet_detector/test/test_rivets.py
"""

import importlib.util
import os
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from rivet_detector import results  # noqa: E402
from rivet_detector.detector import DEFAULTS, FrameCloud, Tracker, detect, voxel_down  # noqa: E402

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
    """Heads cut by the part's side are not accepted: a gap inside the laser's field (here the part ends 2 mm
    right of their centres and the field goes on 6 mm, empty). Heads running out of the field itself (the data
    ends 2 mm right of their centres) were seen only in part: never accepted as whole, at most flagged partial."""
    clipped = points[points[:, 0] < 0.002]
    beyond = np.array([[x, y, clipped[:, 2].max() + 0.010] for x in (0.0079, 0.008) for y in
                       np.arange(clipped[:, 1].min(), clipped[:, 1].max(), 0.0003)])     # far floor: field to x = 8 mm
    side = np.vstack((clipped, beyond))
    rivets, _, _ = detect(side, dict(DEFAULTS), side[:, 1].min(), side[:, 1].max(),
                          log=lambda *_: None, lo_is_scan_start=True)
    assert not rivets, [round(r.centre[1] * 1000, 1) for r in rivets]
    rivets, _, _ = detect(clipped, dict(DEFAULTS), clipped[:, 1].min(), clipped[:, 1].max(),
                          log=lambda *_: None, lo_is_scan_start=True)
    assert all(r.partial for r in rivets), [(round(r.centre[1] * 1000, 1), r.partial) for r in rivets]


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


# Rivets allowed past 0.5 s per bag (all <= 0.8 s): heads whose candidate or edge coverage sits right at a limit
# (MIN_ON_PART: the head + ring disc reaches the cones of the holes beside it; min_rim_coverage) pass a cycle or
# two later, and the first head of a scan waits for a crop long enough to fit the skin.
LATE = {"panel2_06112026_1": 1, "panel2_09112026_02": 1, "b_1904": 2}


def test_latency(period=0.2):
    """Simulated real time at the node's cycle: every rivet comes out once, < 0.5 s after the frame that
    completes it (far edge + ring + guard), with detect <= 150 ms median and <= 300 ms at the 99th percentile per cycle."""
    from frames import replay
    expected = {BAGS / name: count for name, (count, _) in PANEL2.items()}
    expected.update({PANEL / name: count for name, count in PANEL_RIVETS.items()})
    for bag, count in expected.items():
        if not bag.exists():
            print("skip %s: not copied here" % bag.name)
            continue
        run = replay(bag, Tracker(log=lambda *_: None), period=period)
        median, worst = np.median(run.cycle), max(run.cycle)
        print("%s latency: %d rivets, max %.2f s, cycle median %.0f ms worst %.0f ms" % (
            bag.name, len(run.features), max(run.latency, default=0), median * 1000, worst * 1000))
        assert len(run.features) == count, "%s: %d rivets, expected %d" % (bag.name, len(run.features), count)
        slow = sum(lat >= 0.5 for lat in run.latency)
        assert slow <= LATE.get(bag.name, 0), "%s: %d rivets later than 0.5 s" % (bag.name, slow)
        # 99th percentile, not the single worst cycle: one scheduling spike on a busy laptop is not a regression.
        # The slowest cycles are at the scan's end, where the laser stands still and profiles pile up at one y.
        assert median <= 0.150 and np.percentile(run.cycle, 99) <= 0.300, bag.name


def test_rviz_is_light(name="rivets.rviz", things="rivets"):
    """The shipped RViz layout draws only the segmented cut-outs (downsampled, latched), markers and progress:
    no full scan cloud and no surface strip, which stall RViz on the Pi."""
    import yaml
    layout = yaml.safe_load((Path(__file__).resolve().parents[1] / "rviz" / name).read_text())
    shown = [d for d in layout["Visualization Manager"]["Displays"] if d.get("Enabled")]
    topics = {d.get("Topic", {}).get("Value") for d in shown}
    assert not topics & {"/scan/cloud_view", "/scan/cloud", "/%s/surface" % things, "/%s/boundary" % things}, topics
    view = [d for d in shown if d.get("Topic", {}).get("Value") == "/%s/boundary_view" % things]
    assert len(view) == 1 and view[0]["Decay Time"] == 0 and view[0]["Topic"]["Durability Policy"] == "Transient Local"


def test_downsample_matches_voxel_down(points):
    """Tracker.downsample (cached voxel rows) gives exactly voxel_down(crop): the same points in the same order,
    on a crop that grows 0.4 mm at a time and is cut at an arbitrary y (the surface fit samples by index)."""
    tracker = Tracker(log=lambda *_: None)
    points = points[np.argsort(points[:, 1], kind="stable")]
    for end in np.arange(0.004, points[-1, 1], 0.0004):
        crop = points[(points[:, 1] < end) & (points[:, 1] > end - 0.0437)]
        assert np.array_equal(tracker.downsample(crop, crop[:, 1].max()), voxel_down(crop, tracker.p["voxel"])), end


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


def test_write_failure_fails_the_scan(tmp=Path("/tmp/rivet_detector_write_fail_test")):
    """Review of 78004e1 #3: a result file (CSV row, PCD) or scan folder that cannot be written makes the scan's
    DetectionFinalized fail with the reason; a scan whose files were all written still succeeds."""
    import os
    import shutil
    import threading
    from rivet_detector import node
    from rivet_detector import results as res
    if node.DetectionFinalized is None:
        print("skip test_write_failure_fails_the_scan: laser_hhrp messages not built / sourced")
        return
    if os.geteuid() == 0:
        print("skip test_write_failure_fails_the_scan: root ignores file permissions")
        return
    from builtin_interfaces.msg import Time
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    published, logged = [], []
    folder = res.ScanFolder(tmp, log=logged.append)
    stub = SimpleNamespace(
        folder=folder, lock=threading.Lock(), jobs={}, done_ids=[], params=dict(DEFAULTS),
        pub_final=SimpleNamespace(publish=published.append),
        get_clock=lambda: SimpleNamespace(now=lambda: SimpleNamespace(to_msg=Time)),
        get_logger=lambda: SimpleNamespace(info=logged.append, error=logged.append))
    Detector = node.RivetDetector

    def one(i):
        return SimpleNamespace(id=i, centre=np.array([0.0, i * 0.01, 0.07]), radius=0.004, rms=1e-5, height=1e-5,
                               coverage=1.0, countersunk=True, partial=False, scanned_share=1.0,
                               boundary=np.zeros((10, 3)))

    def finalize(scan_id, before_save=None):
        job = Detector.job_for(stub, scan_id)
        if before_save:
            before_save(job.path)
        if job.path.is_dir():
            folder.path = job.path
            folder.save(one(1), None)
        Detector.finalize_job(stub, job, True, "")
        return published[-1]

    try:
        ok = finalize("scan-ok")
        assert ok.success and ok.error == "" and (tmp / "scan-ok" / "rivet_01.pcd").exists(), ok.error

        def read_only(path):
            (path / "summary.csv").chmod(0o444)
            path.chmod(0o555)
        bad = finalize("scan-read-only", read_only)
        assert not bad.success and "could not save" in bad.error and "rivet_01.pcd" in bad.error, bad.error

        tmp.chmod(0o555)                           # the scan folder itself cannot be created
        nofolder = finalize("scan-no-folder")
        assert not nofolder.success and "scan folder" in nofolder.error, nofolder.error
        assert folder.take_errors(tmp / "scan-ok") == []        # errors never leak into another scan
    finally:
        tmp.chmod(0o755)
        if (tmp / "scan-read-only").exists():
            (tmp / "scan-read-only").chmod(0o755)
        shutil.rmtree(tmp, ignore_errors=True)


# ---- Rows scanned on the Pi 2026-11-22 (bags pi_20261122/: rivets, rivets2, holes1, holes2; ~2.2 GB, not in the
# repo). Centres along the scan (mm) read off the scans' height maps, one per feature. HHRP_BAGS points at the
# folder holding pi_20261122/ (default: BAGS).
ROWS_20261122 = Path(os.environ.get("HHRP_BAGS", BAGS)) / "pi_20261122"
RIVET_ROWS = {   # (bag, scan number in the bag): rivet centres (y, mm)
    ("rivets", 0): [8, 30, 72, 94, 115], ("rivets", 1): [7, 28, 50, 92, 113, 134],
    ("rivets", 2): [22, 43, 64, 106, 128, 149], ("rivets", 3): [18], ("rivets", 4): [16, 37, 58, 100, 121],
    ("rivets2", 0): [35, 56, 78, 120, 141, 163, 205, 226, 248, 290, 311, 333, 375, 396, 417, 459, 480, 501]}
HOLE_ROWS = {    # countersunk holes; holes1's last one runs out of the laser's field (partial)
    ("holes1", 0): [11.4, 32.7, 53.8, 96.3, 117.6, 139.0, 181.6, 203.0, 224.4, 266.9, 288.1, 309.4, 351.6, 372.8,
                    392.4],
    ("holes2", 0): [15.9, 37.1, 58.3, 100.3, 121.3, 142.5, 184.7, 205.9, 227.2, 269.7, 290.8, 311.9, 354.0, 375.1,
                    396.4, 438.7, 460.2, 481.6, 522.8, 543.6]}


def bag_scans(bag):
    """The scans of a bag's /scan/frame, as lists of ScanFrame (a new scan at frame index 0; frames before the
    first index 0 are left out). Jazzy bags: Humble's rosbag2 cannot read their metadata, so the file is opened."""
    import rosbag2_py
    from rclpy.serialization import deserialize_message
    from laser_hhrp.msg import ScanFrame
    reader = rosbag2_py.SequentialReader()
    try:
        reader.open(rosbag2_py.StorageOptions(uri=str(bag), storage_id="mcap"),
                    rosbag2_py.ConverterOptions("cdr", "cdr"))
    except Exception:
        reader = rosbag2_py.SequentialReader()
        reader.open(rosbag2_py.StorageOptions(uri=str(next(Path(bag).glob("*.mcap"))), storage_id="mcap"),
                    rosbag2_py.ConverterOptions("cdr", "cdr"))
    reader.set_filter(rosbag2_py.StorageFilter(topics=["/scan/frame"]))
    scans = []
    while reader.has_next():
        frame = deserialize_message(reader.read_next()[1], ScanFrame)
        if frame.index == 0:
            scans.append([])
        if scans:
            scans[-1].append(frame)
    return scans


def replay_scan(frames, every):
    """The node's tagged path: frames into FrameCloud, one detection cycle per `every` frames (24 = 0.2 s at
    120 profiles/s, a fast machine; 180 = 1.5 s, the Pi under load), trimmed after each, and the final pass."""
    tracker, cloud, found = Tracker(log=lambda *_: None), FrameCloud(), []
    for i, f in enumerate(frames):
        cloud.add(f.x_m, f.position_m, f.z_m, f.valid)
        last = i == len(frames) - 1
        if (i + 1) % every == 0 or last:
            found += tracker.feed(cloud.cloud(), f.header.stamp.sec + f.header.stamp.nanosec * 1e-9, final=last)[1]
            cloud.trim(tracker)
    return found


def match_rows(found, truth, name, tol=3.0):
    """Each expected centre found exactly once (within tol mm along the scan), nothing else."""
    ys = sorted(round(f.centre[1] * 1000, 1) for f in found)
    for y in truth:
        hits = [v for v in ys if abs(v - y) < tol]
        assert len(hits) == 1, "%s: expected one at %.1f mm, found %s (all: %s)" % (name, y, hits, ys)
    extra = [v for v in ys if not any(abs(v - y) < tol for y in truth)]
    assert not extra, "%s: unexpected at %s (all: %s)" % (name, extra, ys)


def test_rows_20261122():
    """Countersunk blind rivets sitting flush (heads 0-0.08 mm off the skin, no height step to speak of), found by
    the seam around the head, through the laser's glare stripe, the line-to-line rocking of a hand-held roll and the
    field's edge; on the hole rows nothing. Fast and Pi-like detection cycles give the same heads."""
    for bag in ("rivets", "rivets2", "holes1", "holes2"):
        if not (ROWS_20261122 / bag).exists():
            print("skip %s: not copied here" % bag)
            continue
        for k, frames in enumerate(bag_scans(ROWS_20261122 / bag)):
            for every in (24, 180):
                found = replay_scan(frames, every)
                match_rows(found, RIVET_ROWS.get((bag, k), []), "%s scan %d, cycle %d frames" % (bag, k, every))
                for r in found:     # the seam's diameter: 7.9-8.2 mm on these heads
                    assert 0.0077 <= 2 * r.radius <= 0.0085, (bag, k, every, round(r.centre[1] * 1000, 1), 2 * r.radius)


if __name__ == "__main__":
    test_launch_single_node()
    test_write_failure_fails_the_scan()
    test_rviz_is_light()
    panel = test_synthetic_batch()
    test_side_clip(panel)
    test_downsample_matches_voxel_down(panel)
    test_synthetic_both_signs(panel)
    found = test_synthetic_incremental(panel)
    test_boundary_is_raw(panel, found)
    test_results_have_no_nan(found)
    test_panel2_bags()
    test_panel_bags_generic()
    test_rows_20261122()
    test_latency()
    print("all checks passed")
