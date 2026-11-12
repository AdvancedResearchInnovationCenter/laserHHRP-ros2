"""ROS 2 node: holes (top rim) from the scanner's profiles (/scan/frame) or its cumulative cloud, published once each."""

import threading
import time
from pathlib import Path

import numpy as np
import rclpy
from geometry_msgs.msg import PoseArray
from rclpy.callback_groups import MutuallyExclusiveCallbackGroup
from rclpy.executors import ExternalShutdownException, MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import PointCloud2
from sensor_msgs_py import point_cloud2
from std_msgs.msg import Header
from visualization_msgs.msg import Marker, MarkerArray

from hole_detector import results, viz
from hole_detector.detector import DEFAULTS, UNITLESS, FrameCloud, Tracker, circle_points, voxel_down

try:
    from laser_hhrp.msg import InspectionResult, ScanFrame
except ImportError:  # the node still runs on input_type cloud where laser_hhrp is not built
    InspectionResult = ScanFrame = None

VIEW_VOXEL = 0.0003                # m: the RViz copy of the cut-outs
LATCHED = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE,
                     durability=DurabilityPolicy.TRANSIENT_LOCAL)


class HoleDetector(Node):
    def __init__(self):
        super().__init__("hole_detector")
        # frame: build the cloud from /scan/frame profiles (complete, light messages);
        # cloud: use a cumulative PointCloud2 such as /scan/cloud_view.
        self.declare_parameter("input_type", "frame")
        self.declare_parameter("input_topic", "/scan/frame")
        self.declare_parameter("out_dir", "~/hole_detector_out")
        # The decided surface, coloured by depth below the fit: turn off on the Pi to save CPU and network.
        self.declare_parameter("viz_surface", False)
        self.declare_parameter("period", 0.2)    # s between detection cycles (frame input)
        self.viz_surface = self.get_parameter("viz_surface").value
        self.folder = results.ScanFolder(Path(self.get_parameter("out_dir").value).expanduser(),
                                         self.get_logger().info)
        # Lengths in mm (as in hole_segmentation.py); the detector works in metres.
        for name, value in DEFAULTS.items():
            self.declare_parameter(name, value if name in UNITLESS else value * 1000)
        params = {n: self.get_parameter(n).value if n in UNITLESS else self.get_parameter(n).value * 0.001
                  for n in DEFAULTS}
        self.tracker = Tracker(params, log=self.get_logger().info)
        self.markers, self.circles, self.poses = [], [], []
        self.views = []                    # each hole's cut-out downsampled for RViz (/holes/boundary_view)
        self.shown_rejected = set()        # rejection ids RViz holds, so a withdrawn one can be deleted
        self.shown_bands = 0
        self.scans = 1
        self.ruler_x = np.inf              # beside the widest part seen this scan
        self.pub_markers = self.create_publisher(MarkerArray, "/holes/markers", LATCHED)
        self.pub_circles = self.create_publisher(PointCloud2, "/holes/circles", LATCHED)
        self.pub_poses = self.create_publisher(PoseArray, "/holes/poses", LATCHED)
        self.pub_boundary = self.create_publisher(PointCloud2, "/holes/boundary", 10)   # raw, one hole per message
        # The same cut-outs at 0.3 mm, all of the scan's in one latched cloud: what RViz draws (the raw ones are
        # ~100k points each; a Pi's RViz stalls on millions).
        self.pub_view = self.create_publisher(PointCloud2, "/holes/boundary_view", LATCHED)
        self.pub_surface = self.create_publisher(PointCloud2, "/holes/surface", 10)
        self.pub_progress = self.create_publisher(MarkerArray, "/holes/progress", 10)   # changes every cycle
        self.pub_result = (self.create_publisher(InspectionResult, "/inspection/result", 10)
                           if InspectionResult else None)
        topic = self.get_parameter("input_topic").value
        if self.get_parameter("input_type").value == "frame":
            if ScanFrame is None:
                raise RuntimeError("input_type frame needs laser_hhrp: source /laser_ws/install/setup.bash")
            self.frames, self.last_frame, self.fresh, self.scan = FrameCloud(), None, False, 0
            self.ended = None              # a scan just finished: (its unprocessed cloud or None, header)
            # Frames arrive on their own thread while a detection runs: 200 deep is only ~2.5 s of
            # profiles, and a profile lost is a stripe lost. The lock guards the frame buffer.
            self.lock = threading.Lock()
            self.create_subscription(ScanFrame, topic, self.on_frame, QoSProfile(
                depth=200, reliability=ReliabilityPolicy.RELIABLE),
                callback_group=MutuallyExclusiveCallbackGroup())
            self.create_timer(self.get_parameter("period").value, self.on_timer,
                              callback_group=MutuallyExclusiveCallbackGroup())
        else:
            # Depth 1: when processing falls behind, the newest cumulative cloud wins. Reliable (as the
            # scanner publishes): best effort drops most 10+ MB clouds (10 of 64 arrived in the bag test).
            self.create_subscription(PointCloud2, topic, self.on_cloud, QoSProfile(
                depth=1, reliability=ReliabilityPolicy.RELIABLE))

    def on_frame(self, msg):
        if msg.index == 0 and self.last_frame is not None:     # a new scan: the old one ends here
            with self.lock:
                self.ended = (self.frames.cloud() if self.fresh else None, self.last_frame)
                self.frames.clear()
                self.scan += 1
        with self.lock:
            self.frames.add(msg.x_m, msg.position_m, msg.z_m, msg.valid)
            self.last_frame, self.fresh = msg.header, True

    def on_timer(self):
        t0 = time.time()
        with self.lock:
            ended, self.ended = self.ended, None
            if not self.fresh and ended is None:
                return
            self.fresh = False
            xyz, header, scan = self.frames.cloud(), self.last_frame, self.scan
        if ended is not None:                              # finish the old scan, then start afresh
            if ended[0] is not None:
                self.process(ended[0], self.stamp_of(ended[1]), Header(stamp=ended[1].stamp, frame_id="laser"), t0)
            self.tracker.reset()
            self.new_scan(Header(stamp=header.stamp, frame_id="laser"))
        self.process(xyz, self.stamp_of(header), Header(stamp=header.stamp, frame_id="laser"), t0)
        with self.lock:
            if scan == self.scan:          # a new scan may have started meanwhile: its points stay
                self.frames.trim(self.tracker)

    @staticmethod
    def stamp_of(header):
        return header.stamp.sec + header.stamp.nanosec * 1e-9

    def new_scan(self, header):
        """Clear RViz and start a new output folder."""
        self.folder.new_scan()
        self.markers, self.circles, self.poses = [Marker(action=Marker.DELETEALL)], [], []
        self.views = []
        self.shown_rejected, self.shown_bands = set(), 0
        self.scans += 1
        self.ruler_x = np.inf
        self.publish_state(header)
        self.markers = []

    def on_cloud(self, msg):
        t0 = time.time()
        xyz = point_cloud2.read_points_numpy(msg, field_names=("x", "y", "z"), skip_nans=True)
        stamp = msg.header.stamp.sec + msg.header.stamp.nanosec * 1e-9
        self.process(xyz.astype(np.float64), stamp, msg.header, t0)

    def process(self, xyz, stamp, header, t0):
        t1 = time.time()
        was_reset, new = self.tracker.feed(xyz, stamp)
        t2 = time.time()
        if was_reset:
            self.new_scan(header)
        for hole in new:
            self.add_hole(hole, header)
        tracker = self.tracker
        # shortcut: a band that only widens is redrawn with the next change; track a version if that shows.
        changed = {gap.id for gap in tracker.rejected} != self.shown_rejected or len(tracker.bands) != self.shown_bands
        if new or changed:
            self.publish_state(header)
        if tracker.view is not None:
            status = "scan %d · %d holes · %.0f mm scanned · %d rejected · detect %.0f ms" % (
                self.scans, len(tracker.holes), (tracker.frontier - tracker.start_y) * 1000, len(tracker.rejected),
                (t2 - t1) * 1000)
            points = tracker.view["points"]
            self.ruler_x = min(self.ruler_x, points[:, 0].min() - viz.STYLE["ruler_offset"])
            ruler = viz.ruler_markers(header, *sorted(map(tracker.raw_y, (tracker.start_y, tracker.frontier))),
                                      self.ruler_x, np.median(points[:, 2]))
            self.pub_progress.publish(MarkerArray(markers=viz.progress_markers(
                header, points, tracker.raw_y(tracker.done_y), tracker.raw_y(tracker.frontier), status) + ruler))
        surface_kb = 0
        if self.viz_surface and self.tracker.strip is not None and len(self.tracker.strip[0]):
            cloud = viz.surface_cloud(header, *self.tracker.strip)
            self.pub_surface.publish(cloud)
            surface_kb = len(cloud.data) / 1024
        self.get_logger().info(
            "%d pts: convert %.0f ms, detect %.0f ms, publish %.0f ms (surface %.0f kB), %d new hole(s)" % (
                len(xyz), (t1 - t0) * 1000, (t2 - t1) * 1000, (time.time() - t2) * 1000, surface_kb, len(new)))

    def add_hole(self, hole, header):
        self.get_logger().info("hole #%d at (%.1f, %.1f, %.1f) mm: top %.2f mm, %s%s" % (
            hole.id, *(hole.centre * 1000), 2000 * hole.radius, "countersunk" if hole.countersunk else "plain",
            ", partial (%.0f%% scanned)" % (hole.scanned_share * 100) if hole.partial else ""))
        self.markers += viz.hole_markers(header, hole)
        other = viz.nearest(hole, self.tracker.holes[:hole.id - 1])
        if other is not None:
            self.markers += viz.distance_markers(header, hole, other)
        self.circles.append(viz.with_id(circle_points(hole), hole.id))
        self.views.append(viz.with_id(voxel_down(hole.boundary, VIEW_VOXEL), hole.id))
        self.poses.append(viz.pose(hole.centre, hole.normal))
        self.pub_boundary.publish(viz.id_cloud(header, hole.boundary, hole.id))
        self.folder.save(hole, self.tracker.holes[hole.id - 2] if hole.id > 1 else None)
        if self.pub_result:
            self.pub_result.publish(results.inspection_result(InspectionResult, header, hole))

    def publish_state(self, header):
        """Latched topics always hold every hole and rejection of the current scan, for a late RViz."""
        ids = {gap.id for gap in self.tracker.rejected}
        rejected = [m for gap in self.tracker.rejected for m in viz.rejected_markers(header, gap)]
        withdrawn = [viz.delete(header, ns, i) for i in self.shown_rejected - ids for ns in viz.REJECTED_NS]
        self.shown_rejected, self.shown_bands = ids, len(self.tracker.bands)
        bands = viz.band_markers(header, self.tracker.bands)
        self.pub_markers.publish(MarkerArray(markers=self.markers + rejected + withdrawn + bands))
        cloud = np.vstack(self.circles) if self.circles else np.empty((0, 4), np.float32)
        self.pub_circles.publish(point_cloud2.create_cloud(header, viz.FIELDS, cloud))
        view = np.vstack(self.views) if self.views else np.empty((0, 4), np.float32)
        self.pub_view.publish(point_cloud2.create_cloud(header, viz.FIELDS, view))
        self.pub_poses.publish(PoseArray(header=header, poses=self.poses))


def main():
    rclpy.init()
    executor = MultiThreadedExecutor(num_threads=2)
    node = HoleDetector()
    executor.add_node(node)
    try:
        executor.spin()
    except (KeyboardInterrupt, ExternalShutdownException):
        pass                                   # Ctrl+C / launch shutdown: a clean exit, not a crash
    finally:
        node.folder.close()                    # every hole found is on disk before the process ends


if __name__ == "__main__":
    main()
