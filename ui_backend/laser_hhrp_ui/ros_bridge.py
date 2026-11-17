"""Live ROS 2 bridge (plan Phase 2): existing topics -> immutable engine events.

Read-only: it subscribes and never publishes or calls a service, so it cannot start, stop or slow
acquisition. Callbacks only convert a message and hand it to `submit` (non-blocking, thread-safe);
SQLite, the UI and the network are never touched on the ROS thread.

Subscribed: scan/status, laser/status, nano/status, scan/storage (readiness), and the operator UI's tagged
identity topics (Phase 3): scan/session_state (scan transitions), scan/stream_closed, inspection/result_tagged,
inspection/detection_finalized, scan/recording_session. The untagged inspection/result is ignored (it carries
no scan identity); only the explicit preview fallback (Phase 2 behaviour) still reads it.
Identity subscriptions are volatile: after a backend restart, history of old transitions must not create
scans. Freshness limits match the scan controller's own start gate: laser < 3 s, Nano < 1.5 s.
"""

from __future__ import annotations

import json
import logging
import threading
import time

from .events import (DetectionFinalized, HardwareStatus, LegacyResult, RecordingSession, ScanTransition, StreamClosed,
                     TaggedResult)

log = logging.getLogger("laser_hhrp_ui.ros_bridge")
STATES = {0: "IDLE", 1: "SCANNING", 2: "PAUSED", 3: "ERROR"}
CALIBRATED = 2
LASER_FRESH_S, NANO_FRESH_S, STORAGE_FRESH_S = 3.0, 1.5, 5.0


def split_faults(faults) -> tuple[tuple, tuple]:
    """ScanStatus.faults ("SEVERITY source: message") -> (fatal, other) as "source: message"."""
    fatal, other = [], []
    for f in faults:
        severity, _, rest = str(f).partition(" ")
        (fatal if severity == "FATAL" else other).append(rest or str(f))
    return tuple(fatal), tuple(other)


class ReadinessCache:
    """Latest auxiliary status messages with their receive times (monotonic)."""

    def __init__(self):
        self.laser = self.nano = self.storage = None
        self.laser_t = self.nano_t = self.storage_t = None

    def hardware(self, status, now: float) -> HardwareStatus:
        laser_ok = (self.laser is not None and now - self.laser_t < LASER_FRESH_S and bool(self.laser.connected))
        nano_fresh = self.nano is not None and now - self.nano_t < NANO_FRESH_S
        storage_fresh = self.storage is not None and now - self.storage_t < STORAGE_FRESH_S
        fatal, other = split_faults(status.faults)
        return HardwareStatus(
            scan_state=STATES.get(int(status.state), f"UNKNOWN({status.state})"),
            calibrated=int(status.calibration) == CALIBRATED,
            laser_ok=laser_ok, nano_ok=nano_fresh, encoder_ok=bool(nano_fresh and self.nano.encoder_ok),
            free_mb=float(self.storage.free_mb) if storage_fresh else None,
            storage_write_failed=bool(storage_fresh and self.storage.write_failed),
            fatal=fatal, warnings=other, position_m=float(status.position_m), message=str(status.message))


def legacy_result(msg) -> LegacyResult:
    try:
        detail = json.loads(msg.detail) if msg.detail else {}
        if not isinstance(detail, dict):
            detail = {}
    except ValueError:
        detail = {}
    stamp = msg.header.stamp
    return LegacyResult(feature_type=str(msg.feature_type), position_m=tuple(float(v) for v in msg.position_m),
                        detail=detail, stamp_ns=int(stamp.sec) * 1_000_000_000 + int(stamp.nanosec))


RECORDING_EVENTS = {0: "OPENED", 1: "CLOSED", 2: "OPEN_FAILED"}


def stamp_ns(t) -> int:
    return int(t.sec) * 1_000_000_000 + int(t.nanosec)


def scan_transition(m) -> ScanTransition | None:
    """ScanSessionState -> engine transition; periodic snapshots and scan-less states are not transitions."""
    if not m.is_transition or not m.scan_id:
        return None
    return ScanTransition(scan_id=str(m.scan_id), controller_instance_id=str(m.controller_instance_id),
                          transition_seq=int(m.transition_seq), state=STATES.get(int(m.state), f"UNKNOWN({m.state})"),
                          status_stamp_ns=stamp_ns(m.status_stamp))


def stream_closed(m) -> StreamClosed:
    return StreamClosed(scan_id=str(m.scan_id), frame_count=int(m.frame_count),
                        stop_state=STATES.get(int(m.stop_state), f"UNKNOWN({m.stop_state})"), error=str(m.error))


def tagged_result(m) -> TaggedResult:
    r = m.result
    try:
        detail = json.loads(r.detail) if r.detail else {}
        if not isinstance(detail, dict):
            detail = {}
    except ValueError:
        detail = {}
    return TaggedResult(scan_id=str(m.scan_id), result_seq=int(m.result_seq), detector_mode=str(m.detector_mode),
                        feature_id=int(m.feature_id), feature_type=str(r.feature_type),
                        position_m=tuple(float(v) for v in r.position_m), detail=detail,
                        countersink_depth_m=float(r.countersink_depth_m), flushness_m=float(r.flushness_m))


def detection_finalized(m) -> DetectionFinalized:
    return DetectionFinalized(scan_id=str(m.scan_id), detector_mode=str(m.detector_mode),
                              frame_count=int(m.frame_count), emitted_result_count=int(m.emitted_result_count),
                              detected_target_count=int(m.detected_target_count), success=bool(m.success),
                              error=str(m.error))


def recording_session(m) -> RecordingSession:
    return RecordingSession(event=RECORDING_EVENTS.get(int(m.event), f"UNKNOWN({m.event})"),
                            status_stamp_ns=stamp_ns(m.status_stamp), session_id=str(m.session_id),
                            session_dir=str(m.session_dir), frame_count=int(m.frame_count),
                            write_failed=bool(m.write_failed))


class RosBridge:
    def __init__(self, submit, node_name: str = "laser_hhrp_ui", preview: bool = False):
        self.submit = submit
        self.preview = preview
        self.node_name = node_name
        self.node = None
        self.executor = None
        self.thread: threading.Thread | None = None
        self.cache = ReadinessCache()
        self._owns_context = False

    def start(self) -> None:
        import rclpy
        from rclpy.executors import SingleThreadedExecutor
        from rclpy.qos import QoSProfile, ReliabilityPolicy
        from laser_hhrp.msg import (DetectionFinalized as Finalized, InspectionResult, LaserStatus, NanoStatus,
                                    ScanRecordingSession, ScanSessionState, ScanStatus, ScanStreamClosed,
                                    StorageStatus, TaggedInspectionResult)

        if not rclpy.ok():
            rclpy.init(args=None)
            self._owns_context = True
        self.node = rclpy.create_node(self.node_name)
        reliable = QoSProfile(depth=10, reliability=ReliabilityPolicy.RELIABLE)
        c = self.cache

        def on_laser(m):
            c.laser, c.laser_t = m, time.monotonic()

        def on_nano(m):
            c.nano, c.nano_t = m, time.monotonic()

        def on_storage(m):
            c.storage, c.storage_t = m, time.monotonic()

        def on_status(m):                       # every message: 5 Hz plus every transition
            self.submit(c.hardware(m, time.monotonic()))

        def on_result(m):
            self.submit(legacy_result(m))

        def on_session(m):
            t = scan_transition(m)
            if t is not None:
                self.submit(t)

        self.node.create_subscription(LaserStatus, "laser/status", on_laser, reliable)
        self.node.create_subscription(NanoStatus, "nano/status", on_nano, reliable)
        self.node.create_subscription(StorageStatus, "scan/storage", on_storage, reliable)
        self.node.create_subscription(ScanStatus, "scan/status", on_status, reliable)
        deep = QoSProfile(depth=200, reliability=ReliabilityPolicy.RELIABLE)
        if self.preview:                         # Phase 2 fallback: untagged results, shown, never recorded
            self.node.create_subscription(InspectionResult, "inspection/result", on_result, deep)
        else:
            self.node.create_subscription(ScanSessionState, "scan/session_state", on_session,
                                          QoSProfile(depth=50, reliability=ReliabilityPolicy.RELIABLE))
            self.node.create_subscription(ScanStreamClosed, "scan/stream_closed",
                                          lambda m: self.submit(stream_closed(m)), reliable)
            self.node.create_subscription(TaggedInspectionResult, "inspection/result_tagged",
                                          lambda m: self.submit(tagged_result(m)), deep)
            self.node.create_subscription(Finalized, "inspection/detection_finalized",
                                          lambda m: self.submit(detection_finalized(m)), reliable)
            self.node.create_subscription(ScanRecordingSession, "scan/recording_session",
                                          lambda m: self.submit(recording_session(m)), reliable)
        self.executor = SingleThreadedExecutor()
        self.executor.add_node(self.node)
        self.thread = threading.Thread(target=self._spin, daemon=True, name="ros-bridge")
        self.thread.start()
        log.info("ROS bridge %s started", self.node_name)

    def _spin(self) -> None:
        from rclpy.executors import ExternalShutdownException
        try:
            self.executor.spin()
        except ExternalShutdownException:
            log.info("ROS context shut down (SIGINT/SIGTERM)")
        except Exception:
            log.exception("ROS executor stopped")

    def stop(self) -> None:
        import rclpy
        if self.executor is not None:
            self.executor.shutdown()
        if self.thread is not None:
            self.thread.join(timeout=5)
        if self.node is not None:
            self.node.destroy_node()
        if self._owns_context:
            rclpy.try_shutdown()
