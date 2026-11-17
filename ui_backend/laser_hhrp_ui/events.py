"""Inputs to the engine: immutable events from the ROS bridge (Phase 2/3) or the mock source.

They follow the plan's tagged contract (section 5) so the live bridge only has to translate ROS
messages; nothing here imports rclpy.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ScanTransition:
    """Controller acquisition transition with identity (ScanSessionState)."""
    scan_id: str                 # UUID allocated by the controller on a successful start
    controller_instance_id: str  # changes when the controller restarts
    transition_seq: int          # +1 on every transition of this controller instance: gaps are detectable
    state: str                   # "SCANNING" | "PAUSED" | "IDLE" | "ERROR"
    status_stamp_ns: int = 0     # exact ScanStatus.header.stamp of the transition
    message: str = ""


@dataclass(frozen=True)
class StreamClosed:
    """Synchronizer: no more tagged frames for this scan."""
    scan_id: str
    frame_count: int             # tagged frames actually published
    stop_state: str = "IDLE"
    error: str = ""


@dataclass(frozen=True)
class TaggedResult:
    """One detector result tagged with its scan (TaggedInspectionResult)."""
    scan_id: str
    result_seq: int              # 0, 1, 2 ... per scan
    detector_mode: str           # models.Mode value
    feature_id: int              # detail.id
    feature_type: str
    position_m: tuple
    detail: dict = field(default_factory=dict)
    countersink_depth_m: float = -1.0
    flushness_m: float = -1.0


@dataclass(frozen=True)
class DetectionFinalized:
    """Detector terminal acknowledgement: exactly one per scan."""
    scan_id: str
    detector_mode: str
    frame_count: int
    emitted_result_count: int
    detected_target_count: int
    success: bool
    error: str = ""


@dataclass(frozen=True)
class DetectorStatus:
    """Supervisor view of the selected detector process."""
    mode: str | None             # Mode value of the running detector, None if none
    alive: bool
    ready: bool                  # process alive AND its ROS subscriptions confirmed
    error: str = ""


@dataclass(frozen=True)
class RecordingSession:
    """The legacy recorder opened / closed a raw session (scan/recording_session). Matched to a scan by the exact
    stamp of the scan/status transition that caused it; the recorder does not know scan UUIDs."""
    event: str                   # "OPENED" | "CLOSED" | "OPEN_FAILED"
    status_stamp_ns: int
    session_id: str
    session_dir: str
    frame_count: int = 0
    write_failed: bool = False


@dataclass(frozen=True)
class LegacyResult:
    """Untagged /inspection/result (Phase 2 preview). Carries no scan identity, so it is shown live
    but never persisted or attributed to a recorded scan."""
    feature_type: str
    position_m: tuple
    detail: dict = field(default_factory=dict)
    stamp_ns: int = 0


@dataclass(frozen=True)
class HardwareStatus:
    """Aggregated readiness from scan/status, laser/status, nano/status, scan/storage (plan section 9)."""
    scan_state: str = "IDLE"
    calibrated: bool = False
    laser_ok: bool = False       # laser/status connected and fresh (< 3 s)
    nano_ok: bool = False        # nano/status fresh (< 1.5 s)
    encoder_ok: bool = False
    free_mb: float | None = None
    storage_write_failed: bool = False
    fatal: tuple = ()            # active FATAL faults, "source: message"
    warnings: tuple = ()         # active WARNING/ERROR faults
    position_m: float = 0.0      # scan-axis distance of the current scan
    message: str = ""
