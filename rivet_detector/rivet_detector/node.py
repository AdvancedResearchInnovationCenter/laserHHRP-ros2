"""ROS 2 node: rivet heads from the scanner's profiles (/scan/frame) or its cumulative cloud, published once each.
A copy of the hole node's wiring (same inputs, threading, outputs), on /rivets/*; nothing is imported from it."""

import threading
from collections import deque
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

from rivet_detector import results, viz
from rivet_detector.detector import DEFAULTS, UNITLESS, FrameCloud, Tracker, circle_points, voxel_down

try:
    from laser_hhrp.msg import InspectionResult, ScanFrame
except ImportError:  # the node still runs on input_type cloud where laser_hhrp is not built
    InspectionResult = ScanFrame = None

try:   # operator UI messages (newer laser_hhrp only); legacy mode works without them
    from laser_hhrp.msg import DetectionFinalized, ScanStreamClosed, TaggedInspectionResult, TaggedScanFrame
except ImportError:
    DetectionFinalized = ScanStreamClosed = TaggedInspectionResult = TaggedScanFrame = None

MODE = "rivet_flushness"                      # detector_mode in the tagged messages (the operator UI's inspection mode)
VIEW_VOXEL = 0.0003                # m: the RViz copy of the cut-outs
LATCHED = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE,
                     durability=DurabilityPolicy.TRANSIENT_LOCAL)


class ScanJob:
    """Operator UI (tagged) mode: one scan's frames, tracker and results, keyed by its scan UUID."""

    def __init__(self, scan_id, tracker, path):
        self.scan_id, self.tracker, self.path = scan_id, tracker, path
        self.frames, self.indices = FrameCloud(), set()
        self.header, self.fresh = None, False
        self.closed, self.closed_at = None, None       # ScanStreamClosed and its monotonic arrival
        self.result_seq = self.targets = 0


class RivetDetector(Node):
    def __init__(self):
        super().__init__("rivet_detector")
        # frame: build the cloud from /scan/frame profiles (complete, light messages);
        # cloud: use a cumulative PointCloud2 such as /scan/cloud_view.
        self.declare_parameter("input_type", "frame")
        self.declare_parameter("input_topic", "/scan/frame")
        self.declare_parameter("out_dir", "~/hole_detector_out")
        # The decided surface, coloured by depth below the fit: turn off on the Pi to save CPU and network.
        self.declare_parameter("viz_surface", False)
        self.declare_parameter("period", 0.2)    # s between detection cycles (frame input)
        # Operator UI: scan identity in, tagged results and a terminal acknowledgement out (see setup_tagged).
        self.declare_parameter("tagged", False)
        self.declare_parameter("final_wait_s", 10.0)   # after the stream close: wait this long for missing frames
        self.tagged, self.job = self.get_parameter("tagged").value, None
        self.viz_surface = self.get_parameter("viz_surface").value
        self.folder = results.ScanFolder(Path(self.get_parameter("out_dir").value).expanduser(),
                                         self.get_logger().info)
        # Lengths in mm; the detector works in metres.
        for name, value in DEFAULTS.items():
            self.declare_parameter(name, value if name in UNITLESS else value * 1000)
        params = {n: self.get_parameter(n).value if n in UNITLESS else self.get_parameter(n).value * 0.001
                  for n in DEFAULTS}
        self.params = params
        self.tracker = Tracker(params, log=self.get_logger().info)
        self.markers, self.circles, self.poses = [], [], []
        self.views = []                    # each rivet's cut-out downsampled for RViz (/rivets/boundary_view)
        self.shown_rejected = set()        # rejection ids RViz holds, so a withdrawn one can be deleted
        self.shown_bands = 0
        self.scans = 1
        self.ruler_x = np.inf              # beside the widest part seen this scan
        self.pub_markers = self.create_publisher(MarkerArray, "/rivets/markers", LATCHED)
        self.pub_circles = self.create_publisher(PointCloud2, "/rivets/circles", LATCHED)
        self.pub_poses = self.create_publisher(PoseArray, "/rivets/poses", LATCHED)
        self.pub_boundary = self.create_publisher(PointCloud2, "/rivets/boundary", 10)   # raw, one rivet per message
        # The same cut-outs at 0.3 mm, all of the scan's in one latched cloud: what RViz draws (the raw ones are
        # ~100k points each; a Pi's RViz stalls on millions).
        self.pub_view = self.create_publisher(PointCloud2, "/rivets/boundary_view", LATCHED)
        self.pub_surface = self.create_publisher(PointCloud2, "/rivets/surface", 10)
        self.pub_progress = self.create_publisher(MarkerArray, "/rivets/progress", 10)   # changes every cycle
        self.pub_result = (self.create_publisher(InspectionResult, "/inspection/result", 10)
                           if InspectionResult else None)
        topic = self.get_parameter("input_topic").value
        if self.get_parameter("input_type").value == "frame" and self.tagged:
            self.setup_tagged()
        elif self.get_parameter("input_type").value == "frame":
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

    # ---- operator UI (tagged) mode -------------------------------------------------------------
    # Frames come with their scan UUID (/scan/frame_tagged); each scan gets its own job (tracker, output
    # folder named by the UUID, made at once). When /scan/stream_closed has arrived and frames
    # 0..frame_count-1 are all in, the job's remaining frames get their last detection pass, the files are
    # flushed and exactly one DetectionFinalized is published, without waiting for a next scan. Missing
    # frames fail the scan after final_wait_s; nothing beyond the scanned length is invented.
    def setup_tagged(self):
        if TaggedScanFrame is None:
            raise RuntimeError("tagged mode needs laser_hhrp with the operator UI messages")
        self.jobs, self.done_ids, self.current = {}, deque(maxlen=500), None
        self.late_frames = 0
        self.lock = threading.Lock()
        reliable = QoSProfile(depth=50, reliability=ReliabilityPolicy.RELIABLE)
        inputs = MutuallyExclusiveCallbackGroup()
        # 400 deep: ~3 s of profiles while a detection runs; a lost frame fails the scan, it is never skipped.
        self.create_subscription(TaggedScanFrame, "/scan/frame_tagged", self.on_tagged_frame, QoSProfile(
            depth=400, reliability=ReliabilityPolicy.RELIABLE), callback_group=inputs)
        # Volatile: closes of scans from before this process started are none of its business.
        self.create_subscription(ScanStreamClosed, "/scan/stream_closed", self.on_stream_closed, reliable,
                                 callback_group=inputs)
        self.pub_tagged = self.create_publisher(TaggedInspectionResult, "/inspection/result_tagged", QoSProfile(
            depth=200, reliability=ReliabilityPolicy.RELIABLE))
        self.pub_final = self.create_publisher(DetectionFinalized, "/inspection/detection_finalized", QoSProfile(
            depth=50, reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.TRANSIENT_LOCAL))
        self.create_timer(self.get_parameter("period").value, self.on_tagged_timer,
                          callback_group=MutuallyExclusiveCallbackGroup())
        self.get_logger().info("tagged mode (operator UI): /scan/frame_tagged -> /inspection/result_tagged")

    def job_for(self, scan_id):
        """The scan's job, made on first sight (caller holds the lock)."""
        job = self.jobs.get(scan_id)
        if job is None:
            path = self.folder.root / scan_id
            try:
                self.folder.make_fixed(path)
            except OSError as error:               # the scan cannot be saved: its finalization says so
                self.get_logger().error("scan %s: cannot create %s: %s" % (scan_id, path, error))
                self.folder.note_error(path, "scan folder: %s" % (error.strerror or error))
            job = self.jobs[scan_id] = ScanJob(scan_id, Tracker(self.params, log=self.get_logger().info), path)
            self.get_logger().info("scan %s: results in %s" % (scan_id, path))
        return job

    def on_tagged_frame(self, msg):
        with self.lock:
            if msg.scan_id in self.done_ids:
                self.late_frames += 1              # its scan is already finalized: never re-opened
                return
            job, f = self.job_for(msg.scan_id), msg.frame
            if f.index in job.indices:
                return                             # redelivered
            job.indices.add(f.index)
            job.frames.add(f.x_m, f.position_m, f.z_m, f.valid)
            job.header, job.fresh = f.header, True

    def on_stream_closed(self, msg):
        with self.lock:
            if msg.scan_id in self.done_ids:
                return
            job = self.job_for(msg.scan_id)
            if job.closed is None:
                job.closed, job.closed_at = msg, time.monotonic()

    def on_tagged_timer(self):
        with self.lock:
            jobs = list(self.jobs.values())
        for job in jobs:                           # oldest first: a finishing scan before the next one
            t0 = time.time()
            with self.lock:
                fresh, job.fresh = job.fresh, False
                xyz, header = (job.frames.cloud(), job.header) if fresh else (None, None)
            if fresh:
                if self.current is not job:        # RViz shows the newest scan
                    self.current = job
                    self.tracker = job.tracker
                    self.new_scan(Header(stamp=header.stamp, frame_id="laser"))
                self.tracker, self.folder.path, self.job = job.tracker, job.path, job
                self.process(xyz, self.stamp_of(header), Header(stamp=header.stamp, frame_id="laser"), t0)
                self.job = None
                with self.lock:
                    job.frames.trim(job.tracker)
            self.maybe_finalize(job)

    def maybe_finalize(self, job):
        with self.lock:
            closed, fresh = job.closed, job.fresh
            have, top = len(job.indices), max(job.indices, default=-1)
        if closed is None or fresh:
            return                                 # not closed yet, or frames still to process first
        n = closed.frame_count
        if have > n or top >= n:
            self.finalize_job(job, False, "received frames beyond the %d declared" % n)
        elif have == n:                            # every frame 0..n-1 is in and processed: the final pass ran
            self.finalize_job(job, not closed.error, ("frame stream: " + closed.error) if closed.error else "")
        elif time.monotonic() - job.closed_at > self.get_parameter("final_wait_s").value:
            self.finalize_job(job, False, "missing frames: %d of %d never arrived" % (n - have, n))

    def finalize_job(self, job, success, error):
        self.folder.close()                        # every queued CSV row and PCD is on disk first
        lost = self.folder.take_errors(job.path)   # ... or reported: a scan with missing files is not a success
        if lost:
            note = "could not save %d result file(s): %s" % (len(lost), "; ".join(lost[:3]))
            success, error = False, (error + "; " + note) if error else note
        msg = DetectionFinalized(scan_id=job.scan_id, detector_mode=MODE, frame_count=len(job.indices),
                                 emitted_result_count=job.result_seq, detected_target_count=job.targets,
                                 success=success, error=error, output_dir=str(job.path))
        msg.header.stamp = self.get_clock().now().to_msg()
        self.pub_final.publish(msg)
        with self.lock:
            self.jobs.pop(job.scan_id, None)
            self.done_ids.append(job.scan_id)
        self.get_logger().info("scan %s finalized: %s, %d frames, %d results (%d targets)%s" % (
            job.scan_id, "ok" if success else "FAILED", len(job.indices), job.result_seq, job.targets,
            (": " + error) if error else ""))

    @staticmethod
    def stamp_of(header):
        return header.stamp.sec + header.stamp.nanosec * 1e-9

    def new_scan(self, header):
        """Clear RViz and start a new output folder."""
        if not self.tagged:                # tagged: each job has its UUID folder
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
        for rivet in new:
            self.add_rivet(rivet, header)
        tracker = self.tracker
        # shortcut: a band that only widens is redrawn with the next change; track a version if that shows.
        changed = {gap.id for gap in tracker.rejected} != self.shown_rejected or len(tracker.bands) != self.shown_bands
        if new or changed:
            self.publish_state(header)
        if tracker.view is not None:
            status = "scan %d · %d rivets · %.0f mm scanned · %d rejected · detect %.0f ms" % (
                self.scans, len(tracker.rivets), (tracker.frontier - tracker.start_y) * 1000, len(tracker.rejected),
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
            "%d pts: convert %.0f ms, detect %.0f ms, publish %.0f ms (surface %.0f kB), %d new rivet(s)" % (
                len(xyz), (t1 - t0) * 1000, (t2 - t1) * 1000, (time.time() - t2) * 1000, surface_kb, len(new)))

    def add_rivet(self, rivet, header):
        self.get_logger().info("rivet #%d at (%.1f, %.1f, %.1f) mm: head %.2f mm, %.2f mm proud%s" % (
            rivet.id, *(rivet.centre * 1000), 2000 * rivet.radius, rivet.height * 1000,
            ", partial (%.0f%% scanned)" % (rivet.scanned_share * 100) if rivet.partial else ""))
        self.markers += viz.rivet_markers(header, rivet)
        other = viz.nearest(rivet, self.tracker.rivets[:rivet.id - 1])
        if other is not None:
            self.markers += viz.distance_markers(header, rivet, other)
        self.circles.append(viz.with_id(circle_points(rivet), rivet.id))
        self.views.append(viz.with_id(voxel_down(rivet.boundary, VIEW_VOXEL), rivet.id))
        self.poses.append(viz.pose(rivet.centre, rivet.normal))
        self.pub_boundary.publish(viz.id_cloud(header, rivet.boundary, rivet.id))
        self.folder.save(rivet, other)
        result = results.inspection_result(InspectionResult, header, rivet) if InspectionResult else None
        if self.pub_result:
            self.pub_result.publish(result)
        job = self.job
        if job is not None:                        # tagged: numbered per scan, published once
            self.pub_tagged.publish(TaggedInspectionResult(scan_id=job.scan_id, result_seq=job.result_seq,
                                                           detector_mode=MODE, feature_id=rivet.id, result=result))
            job.result_seq += 1
            job.targets += int(bool(True))

    def publish_state(self, header):
        """Latched topics always hold every rivet and rejection of the current scan, for a late RViz."""
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
    node = RivetDetector()
    executor.add_node(node)
    try:
        executor.spin()
    except (KeyboardInterrupt, ExternalShutdownException):
        pass                                   # Ctrl+C / launch shutdown: a clean exit, not a crash
    finally:
        node.folder.close()                    # every rivet found is on disk before the process ends


if __name__ == "__main__":
    main()
