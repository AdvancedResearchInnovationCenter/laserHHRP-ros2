"""Phase 2: live ROS preview in the engine, and the bridge's message conversions (no rclpy needed)."""

from types import SimpleNamespace as NS

import pytest

from harness import Harness
from laser_hhrp_ui.events import LegacyResult
from laser_hhrp_ui.ros_bridge import ReadinessCache, legacy_result, split_faults
from laser_hhrp_ui.state import CommandError


def rivet(fid, partial=False, y=0.02):
    return LegacyResult("rivet", (0.0, y, 0.0), {"id": fid, "d_head_mm": 5.6, "partial": partial})


def ros_ready(mode="rivet_flushness", count=10):
    h = Harness(source="ros-preview")
    insp = h.ready_inspection(mode, count=count)
    return h, insp


# ---------------------------------------------------------------- engine preview
def test_preview_follows_confirmed_status_and_never_persists():
    h, insp = ros_ready()
    assert h.engine.screen().value == "ready"
    h.hw(scan_state="SCANNING", position_m=0.031)
    assert h.engine.screen().value == "scanning"
    h.handle(rivet(1))
    h.handle(rivet(2, partial=True))
    h.handle(LegacyResult("countersink", (0, 0, 0), {"id": 9}))    # other detector's type: ignored
    h.hw(scan_state="IDLE")
    h.handle(rivet(3))                                              # late, after release
    h.handle(rivet(3))                                              # redelivered: once only
    snap = h.engine.snapshot()
    p = snap["preview"]
    assert snap["screen"] == "ready" and snap["preview_notice"]
    assert (p["detected_target_count"], p["partial_target_count"], p["ignored_results"]) == (3, 1, 1)
    assert [f["after_release"] for f in p["features"]] == [False, False, True]
    assert p["distance_mm"] == 31.0 and p["stop_state"] == "IDLE" and p["inspection_uuid"] == insp["uuid"]
    assert all(f["measurement"]["status"] == "not_measured" for f in p["features"])
    # nothing recorded: no scan rows, no features, nothing to accept
    assert h.repo.db.execute("SELECT COUNT(*) FROM scans").fetchone()[0] == 0
    assert h.repo.db.execute("SELECT COUNT(*) FROM features").fetchone()[0] == 0
    assert snap["current_scan"] is None and h.engine.scans(insp["uuid"]) == []


def test_new_physical_scan_starts_a_clean_preview():
    h, _ = ros_ready()
    h.hw(scan_state="SCANNING")
    h.handle(rivet(1))
    h.hw(scan_state="IDLE")
    h.hw(scan_state="SCANNING")                                     # quick re-press
    p = h.engine.snapshot()["preview"]
    assert p["number"] == 2 and p["features"] == [] and p["scanning"]
    started = [pl for t, pl, _ in h.events if t == "scan_progress" and pl.get("phase") == "started"]
    assert len(started) == 2


def test_scan_without_inspection_is_not_previewed():
    h = Harness(source="ros-preview")
    h.engine.set_operator("op")
    h.hw(scan_state="SCANNING")
    assert any(a["code"] == "unassigned_scan" for a in h.engine.alerts)
    h.handle(rivet(1))
    assert h.engine.snapshot()["preview"]["features"] == []


def test_cannot_end_inspection_while_hardware_scans_and_restart_detector():
    h, insp = ros_ready()
    h.hw(scan_state="SCANNING")
    with pytest.raises(CommandError):
        h.engine.end_inspection(insp["uuid"])
    h.hw(scan_state="IDLE")
    h.detector("rivet_flushness", alive=False, ready=False, error="rivet_detector exited with code 1")
    assert "Detector not ready: rivet_detector exited with code 1" in h.engine.readiness()[1]
    h.det.calls.clear()
    h.engine.restart_detector()
    assert h.det.calls == [("request", "rivet_flushness")]
    h.engine.end_inspection(insp["uuid"])
    assert ("stop", None) in h.det.calls


def test_mock_and_ros_sources_do_not_mix():
    h, _ = ros_ready()
    h.hw(scan_state="SCANNING")
    h.press()                                  # a tagged transition would be Phase 3; in preview nothing records
    assert h.engine.screen().value == "scanning"


# ---------------------------------------------------------------- bridge conversions
def status(state=0, calibration=2, faults=(), position_m=0.0, message=""):
    return NS(state=state, calibration=calibration, faults=list(faults), position_m=position_m, message=message)


def test_split_faults():
    fatal, other = split_faults(["FATAL laser: laser profiler disconnected", "WARNING imu: IMU error",
                                 "ERROR sync: x", "garbage"])
    assert fatal == ("laser: laser profiler disconnected",)
    assert other == ("imu: IMU error", "sync: x", "garbage")


def test_readiness_freshness_matches_controller_limits():
    c = ReadinessCache()
    hw = c.hardware(status(), now=100.0)
    assert not hw.laser_ok and not hw.nano_ok and hw.free_mb is None          # nothing received yet
    c.laser, c.laser_t = NS(connected=True), 98.0
    c.nano, c.nano_t = NS(encoder_ok=True), 99.0
    c.storage, c.storage_t = NS(free_mb=800.0, write_failed=False), 99.0
    hw = c.hardware(status(state=1, position_m=0.05, faults=["FATAL nano: encoder error"]), now=100.0)
    assert (hw.scan_state, hw.calibrated, hw.laser_ok, hw.nano_ok, hw.encoder_ok, hw.free_mb) == \
        ("SCANNING", True, True, True, True, 800.0)
    assert hw.fatal == ("nano: encoder error",) and hw.position_m == 0.05
    hw = c.hardware(status(), now=101.6)                                    # nano 2.6 s old, laser 3.6 s
    assert not hw.nano_ok and not hw.encoder_ok and not hw.laser_ok
    c.laser = NS(connected=False)
    c.laser_t = 101.5
    assert not c.hardware(status(), now=101.6).laser_ok                       # fresh but disconnected
    assert c.hardware(status(calibration=1), now=101.6).calibrated is False
    assert c.hardware(status(state=7), now=101.6).scan_state == "UNKNOWN(7)"


def test_legacy_result_parsing():
    msg = NS(feature_type="countersink", position_m=[0.01, 0.2, 0.0], detail='{"id": 4, "partial": true}',
             header=NS(stamp=NS(sec=3, nanosec=5)))
    r = legacy_result(msg)
    assert r.detail == {"id": 4, "partial": True} and r.position_m == (0.01, 0.2, 0.0) and r.stamp_ns == 3_000_000_005
    for bad in ("not json", "[1, 2]", ""):
        assert legacy_result(NS(feature_type="hole", position_m=[0, 0, 0], detail=bad,
                                header=NS(stamp=NS(sec=0, nanosec=0)))).detail == {}


# ---------------------------------------------------------------- Phase 3 tagged conversions
from laser_hhrp_ui.ros_bridge import (detection_finalized, recording_session, scan_transition,  # noqa: E402
                                      stream_closed, tagged_result)


def test_session_state_to_transition():
    m = NS(is_transition=True, scan_id="u-1", controller_instance_id="c", transition_seq=7, state=1,
           status_stamp=NS(sec=5, nanosec=9))
    t = scan_transition(m)
    assert (t.scan_id, t.transition_seq, t.state, t.status_stamp_ns) == ("u-1", 7, "SCANNING", 5_000_000_009)
    assert scan_transition(NS(**{**m.__dict__, "is_transition": False})) is None      # periodic snapshot
    assert scan_transition(NS(**{**m.__dict__, "scan_id": ""})) is None               # before any scan


def test_tagged_conversions():
    assert stream_closed(NS(scan_id="u", frame_count=12, stop_state=3, error="x")).stop_state == "ERROR"
    r = tagged_result(NS(scan_id="u", result_seq=2, detector_mode="csk_depth", feature_id=3,
                         result=NS(feature_type="countersink", position_m=[0.01, 0.02, 0.0], detail='{"id": 3}',
                                   countersink_depth_m=-1.0, flushness_m=-1.0)))
    assert (r.result_seq, r.feature_id, r.detail, r.position_m) == (2, 3, {"id": 3}, (0.01, 0.02, 0.0))
    f = detection_finalized(NS(scan_id="u", detector_mode="rivet_flushness", frame_count=10, emitted_result_count=2,
                               detected_target_count=2, success=False, error="missing frames"))
    assert (f.success, f.error, f.emitted_result_count) == (False, "missing frames", 2)
    rs = recording_session(NS(event=1, status_stamp=NS(sec=1, nanosec=2), session_id="20261117-1",
                              session_dir="/d", frame_count=5, write_failed=False))
    assert (rs.event, rs.status_stamp_ns, rs.frame_count) == ("CLOSED", 1_000_000_002, 5)
