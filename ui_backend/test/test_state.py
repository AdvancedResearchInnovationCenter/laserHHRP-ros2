"""Engine: lifecycle, completion barrier, failures, decisions, recovery (plan sections 4, 5, 8, 11)."""

import uuid
from pathlib import Path

import pytest

from laser_hhrp_ui.events import DetectionFinalized, MeasuredValue, MeasurementStatus

from harness import Harness
from laser_hhrp_ui.models import ValidationError
from laser_hhrp_ui.repository import Repository
from laser_hhrp_ui.state import CommandError


def screen(h):
    return h.engine.screen().value


# ---------------------------------------------------------------- setup and readiness
def test_route_identify_setup_ready():
    h = Harness()
    assert screen(h) == "identify"
    h.engine.set_operator("ENG-1042")
    assert screen(h) == "setup"
    insp = h.engine.create_inspection("csk_depth")
    assert h.det.calls == [("request", "csk_depth")]
    assert insp["label"] == "INS-20261117-001" and insp["next_expected_count"] is None
    assert screen(h) == "not_ready"
    h.detector("csk_depth")
    h.hw()
    assert h.engine.readiness()[1] == ["Enter the expected feature count"]
    h.engine.set_next_expected_count(insp["uuid"], 20)
    assert screen(h) == "ready"


@pytest.mark.parametrize("hw, reason", [
    ({"calibrated": False}, "Calibration required"),
    ({"laser_ok": False}, "Laser not connected"),
    ({"nano_ok": False}, "Nano (encoder/IMU) not responding"),
    ({"encoder_ok": False}, "Encoder error"),
    ({"free_mb": 120.0}, "Low storage: 120 MB free (minimum 500 MB)"),
    ({"storage_write_failed": True}, "Cannot write scan data"),
    ({"fatal": ("laser: laser profiler disconnected",)}, "Fault: laser: laser profiler disconnected"),
])
def test_not_ready_reasons(hw, reason):
    h = Harness()
    h.ready_inspection()
    h.hw(**hw)
    assert screen(h) == "not_ready"
    assert reason in h.engine.readiness()[1]


def test_imu_warning_does_not_block_and_stale_hardware_does():
    h = Harness()
    h.ready_inspection()
    h.hw(warnings=("imu: IMU error",))
    assert screen(h) == "ready" and "imu: IMU error" in h.engine.readiness()[2]
    h.clock.advance(3.0)
    assert screen(h) == "not_ready" and "No current hardware status" in h.engine.readiness()[1]


def test_detector_of_other_mode_is_not_ready():
    h = Harness()
    h.ready_inspection("rivet_flushness")
    h.detector("csk_depth")
    assert screen(h) == "not_ready"


def test_unverified_clock_uses_local_ids_and_warns():
    h = Harness(quality="unverified")
    h.engine.set_operator("op1")
    insp = h.engine.create_inspection("rivet_flushness")
    assert insp["label"] == "INS-LOCAL-000001" and insp["clock_quality"] == "unverified"
    assert "Time not verified" in h.engine.readiness()[2]


# ---------------------------------------------------------------- normal flows
def test_normal_csk_with_late_detections_and_no_fake_pass():
    h = Harness()
    h.ready_inspection(count=20)
    sid = h.press()
    assert screen(h) == "scanning"
    h.results(sid, 12)
    h.result(sid, kind="hole")                      # non-target, not counted
    h.release(sid)
    assert screen(h) == "finishing_analysis"
    h.results(sid, 8)                               # late, after release
    assert screen(h) == "finishing_analysis"        # 20/20 is not completion
    h.close(sid)
    h.finalize(sid)
    assert screen(h) == "results"
    v = h.scan(sid)
    assert (v["detected_target_count"], v["non_target_count"], v["expected_count"]) == (20, 1, 20)
    assert v["count_mismatch"] is False and v["analysis"] == "complete"
    targets = [f for f in v["features"] if f["is_target"]]
    assert all(f["measurement"] == {"name": "csk_depth", "value_mm": 1.0, "unit": "mm", "status": "measured",
                                    "reason": None, "verdict": "in_limits", "lower_mm": 0.947, "upper_mm": 1.147}
               for f in targets)
    hole = next(f for f in v["features"] if not f["is_target"])
    assert hole["measurement"]["status"] == "pending"   # plain holes are never measured
    assert v["measurement"] == {"in_limits": 20, "out_of_limits": 0, "not_measured": 0, "pending": 0}
    late = [p for t, p, s in h.events if t == "feature_detected" and p["after_release"]]
    assert len(late) == 8
    h.engine.decide(sid, "accept")
    assert screen(h) == "ready" and h.scan(sid)["status"] == "accepted"


def test_normal_rivets_count_only_rivets_and_reject_other_mode_results():
    h = Harness()
    h.ready_inspection("rivet_flushness", count=10)
    sid = h.press()
    h.results(sid, 10)
    h.result(sid, kind="countersink", mode="csk_depth", seq=99)    # a stray hole_detector result
    h.release(sid)
    h.close(sid)
    h.finalize(sid)
    v = h.scan(sid)
    assert v["detected_target_count"] == 10 and v["analysis"] == "complete"
    assert {f["kind"] for f in v["features"]} == {"rivet"}
    assert v["features"][0]["measurement"]["name"] == "flushness"
    assert h.repo.audit_events("result_mode_mismatch")


def test_count_mismatch_warns_but_can_be_accepted_and_overflow_never_stops():
    h = Harness()
    h.ready_inspection(count=20)
    a = h.complete_scan(18)
    assert h.scan(a)["count_mismatch"] is True
    h.engine.decide(a, "accept")
    v = h.scan(a)
    assert v["status"] == "accepted_with_warning" and v["decided_by"] == "ENG-1042"
    audit = h.repo.audit_events("scan_decision")[-1]
    assert '"detected": 18' in audit["detail_json"] and '"expected": 20' in audit["detail_json"]
    b = h.press()
    h.results(b, 21)
    assert h.scan(b)["acquisition"] == "scanning"   # 21/20 never ends acquisition
    h.release(b)
    h.close(b)
    h.finalize(b)
    assert h.scan(b)["detected_target_count"] == 21 and h.scan(b)["count_mismatch"] is True


def test_expected_count_remembered_and_snapshotted_per_scan():
    h = Harness()
    insp = h.ready_inspection(count=20)
    a = h.complete_scan(20)
    h.engine.decide(a, "accept")
    h.engine.set_next_expected_count(insp["uuid"], 12)
    b = h.complete_scan(12)
    assert h.scan(a)["expected_count"] == 20 and h.scan(b)["expected_count"] == 12
    h.engine.decide(b, "accept")
    h.engine.end_inspection(insp["uuid"])
    second = h.engine.create_inspection("csk_depth")
    assert second["next_expected_count"] == 12      # previous value prefilled


def test_expected_count_only_in_ready_and_validated():
    h = Harness()
    insp = h.ready_inspection(count=20)
    for bad in (0, 501, "20", 2.5, True, None):
        with pytest.raises(ValidationError):
            h.engine.set_next_expected_count(insp["uuid"], bad)
    sid = h.press()
    with pytest.raises(CommandError) as e:
        h.engine.set_next_expected_count(insp["uuid"], 10)
    assert e.value.status == 409
    h.release(sid)


def test_zero_feature_scan_completes_honestly():
    h = Harness()
    h.ready_inspection(count=20)
    sid = h.press()
    h.release(sid)
    h.close(sid, 3)
    h.finalize(sid, 3, emitted=0, targets=0)
    v = h.scan(sid)
    assert v["analysis"] == "complete" and v["detected_target_count"] == 0 and v["count_mismatch"] is True


def test_partial_flag_comes_only_from_detector():
    h = Harness()
    h.ready_inspection(count=2)
    sid = h.press()
    h.result(sid)
    h.result(sid, partial=True)
    h.release(sid)
    h.close(sid)
    h.finalize(sid)
    v = h.scan(sid)
    assert [f["partial"] for f in v["features"]] == [False, True] and v["partial_target_count"] == 1


# ---------------------------------------------------------------- scan identity and ordering
def test_fast_next_scan_keeps_late_results_with_first_scan():
    h = Harness()
    h.ready_inspection(count=4)
    a = h.press()
    h.results(a, 2)
    h.release(a)
    b = h.press()                                   # B starts before A's analysis finished
    assert screen(h) == "scanning"
    h.results(a, 2)                                 # A's late results
    h.results(b, 1)
    h.close(a)
    h.finalize(a)
    assert h.scan(a)["detected_target_count"] == 4 and h.scan(a)["analysis"] == "complete"
    assert h.scan(b)["detected_target_count"] == 1 and h.scan(b)["analysis"] == "pending"
    assert (h.scan(a)["scan_number"], h.scan(b)["scan_number"]) == (1, 2)
    assert h.scan(a)["status"] == "undecided"       # B started: A stays UNDECIDED, decidable later
    h.release(b)
    h.close(b)
    h.finalize(b)
    assert screen(h) == "results" and h.engine.snapshot()["current_scan"]["scan_id"] == b
    h.engine.decide(a, "rescan")                     # from history
    assert h.scan(a)["status"] == "rescanned"


def test_finalization_waits_for_results_reordered_across_topics():
    h = Harness()
    h.ready_inspection(count=3)
    sid = h.press()
    h.result(sid)
    h.release(sid)
    h.finalize(sid, emitted=3, targets=3, measure=False)   # arrives before results 1, 2 and stream close ...
    h.measure(sid, features=[MeasuredValue(i, True, 0.001) for i in (1, 2, 3)])   # ... and so does the measurement
    assert h.scan(sid)["analysis"] == "pending"
    h.result(sid, seq=2)
    h.close(sid)
    assert h.scan(sid)["analysis"] == "pending"
    h.result(sid, seq=1)
    assert h.scan(sid)["analysis"] == "complete" and h.scan(sid)["detected_target_count"] == 3


def test_results_before_scan_start_are_buffered():
    h = Harness()
    h.ready_inspection(count=1)
    sid = "early-scan"
    h.next_seq[sid] = 0
    from laser_hhrp_ui.events import TaggedResult
    h.handle(TaggedResult(sid, 0, "csk_depth", 1, "countersink", (0, 0.01, 0), {"id": 1, "partial": False}))
    assert h.repo.scan(sid) is None
    h.press(sid)
    assert h.scan(sid)["detected_target_count"] == 1


def test_duplicate_delivery_is_idempotent_and_conflict_fails():
    h = Harness()
    h.ready_inspection(count=2)
    sid = h.press()
    h.result(sid, seq=0, fid=1)
    h.result(sid, seq=0, fid=1)                     # redelivery
    assert h.scan(sid)["detected_target_count"] == 1 and h.scan(sid)["analysis"] == "pending"
    h.result(sid, seq=0, fid=7)                     # same seq, different feature
    assert h.scan(sid)["analysis"] == "failed" and "conflicting" in h.scan(sid)["analysis_error"]


def test_missing_frame_count_or_extra_results_fail():
    h = Harness()
    h.ready_inspection(count=1)
    a = h.press()
    h.result(a)
    h.release(a)
    h.close(a, 500)
    h.finalize(a, 497)
    assert "frame count mismatch" in h.scan(a)["analysis_error"]
    b = h.press()
    h.results(b, 2)
    h.release(b)
    h.close(b)
    h.finalize(b, emitted=1, targets=1)
    assert "beyond" in h.scan(b)["analysis_error"]


def test_missing_result_times_out_with_warning_first():
    h = Harness()
    h.ready_inspection(count=3)
    sid = h.press()
    h.result(sid, seq=0)
    h.result(sid, seq=2)                             # seq 1 never arrives
    h.release(sid)
    h.close(sid)
    h.finalize(sid, emitted=3, targets=3)
    h.clock.advance(11)
    h.engine.tick()
    assert "Analysis is taking longer than expected" in h.engine.readiness()[2]
    assert h.scan(sid)["analysis"] == "pending"
    h.clock.advance(110)
    h.engine.tick()
    v = h.scan(sid)
    assert v["analysis"] == "failed" and "timeout" in v["analysis_error"] and screen(h) == "analysis_failed"


def test_detector_failure_and_error_stop_fail_and_cannot_be_accepted():
    h = Harness()
    h.ready_inspection(count=2)
    a = h.press()
    h.results(a, 2)
    h.release(a)
    h.close(a)
    h.finalize(a, success=False, error="tracker exception")
    assert h.scan(a)["analysis"] == "failed" and "tracker exception" in h.scan(a)["analysis_error"]
    with pytest.raises(CommandError) as e:
        h.engine.decide(a, "accept")
    assert e.value.code == "analysis_not_complete"
    with pytest.raises(CommandError) as e:
        h.engine.failure_action(a, "retry")
    assert e.value.code == "not_replayable"
    h.engine.failure_action(a, "keep_incomplete")
    assert h.scan(a)["status"] == "incomplete_retained" and screen(h) == "ready"
    b = h.press()
    h.results(b, 2)
    h.release(b, "ERROR")
    h.close(b, stop_state="ERROR")
    h.finalize(b)
    v = h.scan(b)
    assert v["acquisition"] == "error" and v["analysis"] == "failed" and v["detected_target_count"] == 2
    h.engine.failure_action(b, "rescan")
    h.engine.failure_action(b, "rescan")             # idempotent
    assert h.scan(b)["status"] == "rescanned"


def test_retry_only_with_verified_raw_data():
    h = Harness()
    h.ready_inspection(count=1)
    sid = h.press()
    h.release(sid)
    h.close(sid)
    h.finalize(sid, success=False)
    h.repo.update_scan(sid, replay_eligible=1)
    h.engine.failure_action(sid, "retry")
    assert h.scan(sid)["analysis"] == "pending" and ("replay", sid) in h.det.calls


def test_detector_crash_fails_pending_scans():
    h = Harness()
    h.ready_inspection(count=1)
    sid = h.press()
    h.release(sid)
    h.detector("csk_depth", alive=False, ready=False, error="exit code 1")
    assert h.scan(sid)["analysis"] == "failed" and "exit code 1" in h.scan(sid)["analysis_error"]
    assert any(a["code"] == "detector_stopped" for a in h.engine.alerts)


def test_scan_without_inspection_or_detector_is_unassigned():
    h = Harness()
    h.engine.set_operator("op")
    h.hw()
    sid = h.press()
    v = h.scan(sid)
    assert v["status"] == "unassigned" and v["unassigned_reason"] == "no open inspection"
    assert any(a["code"] == "unassigned_scan" for a in h.engine.alerts)
    h.result(sid)                                     # ignored, never attributed
    assert h.scan(sid)["detected_target_count"] == 0
    insp = h.engine.create_inspection("csk_depth")    # detector requested but not ready yet
    sid2 = h.press()
    assert h.scan(sid2)["unassigned_reason"] == "detector not ready"
    assert h.engine.scans(insp["uuid"]) == []


def test_missed_stop_transition_ends_previous_scan_and_gap_is_flagged():
    h = Harness()
    h.ready_inspection(count=1)
    a = h.press()
    b = h.press()                                     # no IDLE for A in between
    assert h.scan(a)["acquisition"] == "ended" and "stop transition missing" in h.scan(a)["acquisition_note"]
    h.transition(b, "IDLE", seq=h.seq + 3)            # transitions lost
    assert any(al["code"] == "transition_gap" for al in h.engine.alerts)
    h.transition(b, "IDLE", seq=1)                    # stale redelivery ignored
    assert h.scan(b)["acquisition"] == "ended"


def test_queue_overflow_fails_pending_analysis():
    h = Harness()
    h.ready_inspection(count=1)
    sid = h.press()
    h.engine.ingress_overflow(5)
    assert h.scan(sid)["analysis"] == "failed" and "overflow" in h.scan(sid)["analysis_error"]


# ---------------------------------------------------------------- commands and guards
def test_decisions_idempotent_and_conflicting_rejected():
    h = Harness()
    h.ready_inspection(count=1)
    sid = h.complete_scan(1)
    h.engine.decide(sid, "accept")
    h.engine.decide(sid, "accept")
    with pytest.raises(CommandError) as e:
        h.engine.decide(sid, "rescan")
    assert e.value.status == 409
    with pytest.raises(ValidationError):
        h.engine.decide(sid, "pass")
    assert len(h.repo.audit_events("scan_decision")) == 1


def test_rescan_keeps_the_original_record():
    h = Harness()
    insp = h.ready_inspection(count=3)
    a = h.complete_scan(2)
    h.engine.decide(a, "rescan")
    b = h.complete_scan(3)
    h.engine.decide(b, "accept")
    rows = h.engine.scans(insp["uuid"])
    assert [(r["scan_number"], r["status"], r["detected_target_count"]) for r in rows] == \
        [(1, "rescanned", 2), (2, "accepted", 3)]
    assert len(h.scan(a)["features"]) == 2


def test_operator_and_inspection_guards():
    h = Harness()
    with pytest.raises(CommandError):
        h.engine.create_inspection("csk_depth")
    with pytest.raises(ValidationError):
        h.engine.set_operator("bad id!")
    insp = h.ready_inspection(count=1)
    with pytest.raises(CommandError):
        h.engine.create_inspection("rivet_flushness")   # one open inspection at a time
    with pytest.raises(CommandError):
        h.engine.set_operator("someone-else")
    with pytest.raises(CommandError):
        h.engine.logout()
    sid = h.press()
    h.release(sid)
    with pytest.raises(CommandError) as e:
        h.engine.end_inspection(insp["uuid"])          # analysis pending
    assert e.value.code == "analysis_pending"
    h.close(sid)
    h.finalize(sid, emitted=0, targets=0)
    h.engine.end_inspection(insp["uuid"])               # an undecided scan stays undecided
    h.engine.end_inspection(insp["uuid"])               # idempotent
    assert ("stop", None) in h.det.calls and h.scan(sid)["status"] == "undecided"
    h.engine.logout()
    assert screen(h) == "identify"


def test_labels_unique_case_insensitive_and_request_id_idempotent():
    h = Harness()
    h.engine.set_operator("op")
    first = h.engine.create_inspection("csk_depth", "Wing-7", request_id="r1")
    again = h.engine.create_inspection("csk_depth", "Wing-7", request_id="r1")
    assert again["uuid"] == first["uuid"]
    h.engine.end_inspection(first["uuid"])
    with pytest.raises(CommandError) as e:
        h.engine.create_inspection("csk_depth", "WING-7")
    assert e.value.code == "label_taken"


def test_mode_cannot_change_within_inspection():
    h = Harness()
    insp = h.ready_inspection("csk_depth", count=1)
    with pytest.raises(CommandError):
        h.engine.create_inspection("rivet_flushness")
    assert h.engine.inspection["mode"] == "csk_depth" and h.engine.inspection["uuid"] == insp["uuid"]


# ---------------------------------------------------------------- restart
def test_restart_keeps_history_and_interrupts_unfinished(tmp_path):
    db = tmp_path / "ui.db"
    h = Harness(path=db)
    insp = h.ready_inspection(count=2)
    done = h.complete_scan(2)
    h.engine.decide(done, "accept")
    analyzing = h.press()
    h.result(analyzing)
    h.release(analyzing)
    acquiring = h.press()
    h.repo.close()

    h2 = Harness(repo=Repository(db))
    assert h2.engine.operator["operator_id"] == "ENG-1042" and h2.engine.inspection["uuid"] == insp["uuid"]
    assert h2.scan(done)["status"] == "accepted" and len(h2.scan(done)["features"]) == 2
    assert h2.scan(analyzing)["analysis"] == "interrupted" and len(h2.scan(analyzing)["features"]) == 1
    assert h2.scan(acquiring)["acquisition"] == "interrupted"
    assert screen(h2) == "analysis_failed"           # latest scan unresolved: operator decides
    h2.engine.start()
    assert h2.det.calls == [("request", "csk_depth")]
    h2.result(analyzing, seq=5)                      # late result after restart is not attributed
    assert len(h2.scan(analyzing)["features"]) == 1
    with pytest.raises(CommandError):
        h2.engine.decide(analyzing, "accept")
    h2.engine.failure_action(acquiring, "rescan")
    assert h2.repo.audit_events("recovered_interrupted_analysis")


# ---------------------------------------------------------------- review fixes (code review of 3d7f52e)
def test_stream_close_error_fails_even_when_counts_match():
    h = Harness()
    h.ready_inspection(count=2)
    sid = h.press()
    h.results(sid, 2)
    h.release(sid)
    from laser_hhrp_ui.events import StreamClosed
    h.handle(StreamClosed(sid, 500, "IDLE", error="pairer overflow"))
    h.finalize(sid)                                    # counts would match
    v = h.scan(sid)
    assert v["analysis"] == "failed" and "stream closed with error: pairer overflow" in v["analysis_error"]
    with pytest.raises(CommandError):
        h.engine.decide(sid, "accept")


def test_stream_stop_state_must_match_the_controller():
    h = Harness()
    h.ready_inspection(count=1)
    sid = h.press()
    h.result(sid)
    h.release(sid, "IDLE")
    h.close(sid, stop_state="ERROR")                  # synchronizer saw an error stop
    h.finalize(sid)
    assert "stop state mismatch: controller IDLE, frame stream ERROR" in h.scan(sid)["analysis_error"]


def test_inferred_stop_never_completes():
    """Review 2026-11-18 #2: an inferred stop waits (it does not fail at once); without the controller's
    confirmation the watchdog fails it, never success."""
    h = Harness()
    h.ready_inspection(count=2)
    a = h.press()
    h.results(a, 2)
    b = h.press()                                      # A's stop transition never arrived
    assert h.scan(a)["acquisition"] == "ended" and h.repo.scan(a)["stop_state"] == "UNCONFIRMED"
    h.close(a)
    h.finalize(a)                                      # everything else matches
    assert h.scan(a)["analysis"] == "pending"          # waiting for the controller's stop
    with pytest.raises(CommandError):
        h.engine.decide(a, "accept")
    h.clock.advance(h.engine.fail_after_s + 1)
    h.engine.tick()
    v = h.scan(a)
    assert v["analysis"] == "failed" and "stop not confirmed" in v["analysis_error"]
    assert "timeout" in v["analysis_error"]
    with pytest.raises(CommandError):
        h.engine.decide(a, "accept")


@pytest.mark.parametrize("late_state, outcome", [("IDLE", "complete"), ("ERROR", "failed")])
def test_controller_stop_arriving_after_close_and_finalization(late_state, outcome):
    """Review 2026-11-18 #2: stream close and finalization first, the controller's real stop last."""
    h = Harness()
    h.ready_inspection(count=2)
    a = h.press()                                      # transition 1
    h.results(a, 2)
    b = str(uuid.uuid4())
    h.transition(b, "SCANNING", seq=3)                 # A's stop (seq 2) is late
    h.close(a, stop_state=late_state)
    h.finalize(a)
    assert h.scan(a)["analysis"] == "pending" and h.repo.scan(a)["stop_state"] == "UNCONFIRMED"
    h.clock.advance(h.engine.fail_after_s / 2)
    h.engine.tick()
    assert h.scan(a)["analysis"] == "pending"
    h.transition(a, late_state, seq=2)                 # the controller's stop, last of all
    assert h.scan(a)["analysis"] == outcome
    assert h.repo.audit_events("stop_confirmed_late")


@pytest.mark.parametrize("late_state, outcome", [("IDLE", "complete"), ("ERROR", "failed")])
def test_late_controller_stop_confirms_an_inferred_stop(late_state, outcome):
    h = Harness()
    h.ready_inspection(count=2)
    a = h.press()                                      # transition 1
    h.results(a, 2)
    b = str(uuid.uuid4())
    h.transition(b, "SCANNING", seq=3)                 # A's stop (seq 2) is late: gap now
    assert h.repo.scan(a)["stop_state"] == "UNCONFIRMED"
    assert any(al["code"] == "transition_gap" for al in h.engine.alerts)
    h.transition(a, late_state, seq=2)                 # arrives after B's start (stale seq)
    assert h.repo.scan(a)["stop_state"] == late_state
    h.seq = 3
    h.close(a, stop_state=late_state)
    h.finalize(a)
    assert h.scan(a)["analysis"] == outcome
    assert h.repo.audit_events("stop_confirmed_late")


def test_redelivery_counts_as_duplicate_only_if_identical():
    h = Harness()
    h.ready_inspection(count=2)
    sid = h.press()
    h.result(sid, seq=0, fid=1, y_mm=15.0)
    h.result(sid, seq=0, fid=1, y_mm=15.0)             # identical: ignored
    assert h.scan(sid)["analysis"] == "pending" and h.scan(sid)["detected_target_count"] == 1
    h.result(sid, seq=0, fid=1, y_mm=99.0)             # same seq and id, different position
    v = h.scan(sid)
    assert v["analysis"] == "failed" and "different content" in v["analysis_error"]
    assert v["features"][0]["y_mm"] == 15.0            # the stored result is not overwritten


# ---------------------------------------------------------------- Phase 3 raw recording link
import struct  # noqa: E402

from laser_hhrp_ui.events import RecordingSession  # noqa: E402


def write_frames_bin(path, n, truncate=False):
    path.mkdir(parents=True, exist_ok=True)
    data = b"".join(struct.pack("<I", 10) + bytes(10) for _ in range(n))
    (path / "frames.bin").write_bytes(data[:-3] if truncate else data)


def recorded_scan(h, tmp_path, frames=20, recorded=None, truncate=False, open_first=False, write_failed=False):
    """A scan with a raw recording: OPENED matched by the start stamp, CLOSED with `recorded` frames."""
    raw = tmp_path / "rec"
    write_frames_bin(raw, frames if recorded is None else recorded, truncate)
    start_stamp = (h.seq + 1) * 1000                   # what h.press() will use as status_stamp_ns
    opened = RecordingSession("OPENED", start_stamp, "20261117-120000", str(raw))
    if open_first:
        h.handle(opened)                               # before the scan is known: kept pending
    sid = h.press()
    if not open_first:
        h.handle(opened)
    h.release(sid)
    h.close(sid, frames)
    h.handle(RecordingSession("CLOSED", h.seq * 1000, "20261117-120000", str(raw),
                              frames if recorded is None else recorded, write_failed))
    h.finalize(sid, frames, emitted=0, targets=0)
    return sid


@pytest.mark.parametrize("open_first", [False, True])
def test_raw_recording_linked_by_stamp_and_verified(tmp_path, open_first):
    h = Harness()
    h.ready_inspection(count=1)
    sid = recorded_scan(h, tmp_path, open_first=open_first)
    v = h.scan(sid)
    assert v["raw"]["dir"] == str(tmp_path / "rec") and v["raw"]["state"] == "closed"
    assert v["replay_eligible"] and v["raw"]["note"] == "frames.bin verified: 20 frames"


@pytest.mark.parametrize("kwargs, note", [
    ({"truncate": True}, "frames.bin damaged"),
    ({"recorded": 19}, "frames.bin holds 19 frames, the scan published 20"),
    ({"write_failed": True}, "the recorder reported a write failure"),
])
def test_raw_recording_not_replayable(tmp_path, kwargs, note):
    h = Harness()
    h.ready_inspection(count=1)
    sid = recorded_scan(h, tmp_path, **kwargs)
    v = h.scan(sid)
    assert not v["replay_eligible"] and note in v["raw"]["note"]


def test_retry_refused_while_replay_is_not_implemented(tmp_path):
    h = Harness()
    h.det.can_replay = False                           # the real supervisor: replay not implemented yet
    h.ready_inspection(count=1)
    sid = recorded_scan(h, tmp_path)
    h.repo.update_scan(sid, analysis="failed", analysis_error="x")
    with pytest.raises(CommandError) as e:
        h.engine.failure_action(sid, "retry")
    assert e.value.code == "retry_unavailable" and h.scan(sid)["replay_eligible"]


def test_recording_of_an_unknown_stamp_is_never_attributed(tmp_path):
    h = Harness()
    h.ready_inspection(count=1)
    h.handle(RecordingSession("OPENED", 123456, "x", str(tmp_path)))
    sid = h.press()                                    # its start stamp differs
    assert h.scan(sid)["raw"]["dir"] is None


def test_retry_available_only_when_replay_can_run():
    """The UI shows Retry Analysis from `retry_available`: verified raw data AND a replay path (4.5)."""
    h = Harness()
    h.ready_inspection()
    sid = h.press()
    h.release(sid)
    h.close(sid, 10)
    h.finalize(sid, 10, success=False, error="x")
    assert h.scan(sid)["retry_available"] is False                 # no verified raw recording
    h.repo.update_scan(sid, replay_eligible=1)
    assert h.scan(sid)["retry_available"] is True                  # harness detector can replay
    h.det.can_replay = False
    assert h.scan(sid)["retry_available"] is False                 # verified, but no replay path


@pytest.mark.parametrize("kind", ["close", "finalize"])
def test_conflicting_terminal_message_fails_identical_is_ignored(kind):
    """Review 2026-11-18 #1: identical terminal redeliveries are harmless; a different second one fails."""
    h = Harness()
    h.ready_inspection(count=2)
    sid = h.press()
    h.results(sid, 2)
    h.release(sid)
    if kind == "close":
        h.close(sid, 500)
        h.close(sid, 500)                              # identical: ignored
        assert h.scan(sid)["analysis"] == "pending"
        h.close(sid, 499)                              # different frame count
    else:
        h.close(sid, 500)
        h.handle(DetectionFinalized(sid, "csk_depth", 500, 3, 3, True, ""))   # declares a result not yet in
        h.handle(DetectionFinalized(sid, "csk_depth", 500, 3, 3, True, ""))   # identical: ignored
        assert h.scan(sid)["analysis"] == "pending"
        h.handle(DetectionFinalized(sid, "csk_depth", 500, 2, 2, True, ""))   # different
    v = h.scan(sid)
    assert v["analysis"] == "failed" and "conflicting" in v["analysis_error"], v["analysis_error"]
    assert h.repo.audit_events("conflicting_stream_close" if kind == "close" else "conflicting_detector_finalization")


def test_conflicting_terminal_message_after_completion_alerts():
    h = Harness()
    h.ready_inspection(count=2)
    sid = h.complete_scan(2, frames=500)
    assert h.scan(sid)["analysis"] == "complete"
    h.close(sid, 500)                                  # redelivery after completion: nothing to report
    h.finalize(sid, 500)
    assert not any(a["code"] == "conflicting_terminal" for a in h.engine.alerts)
    h.handle(DetectionFinalized(sid, "csk_depth", 500, 2, 2, False, "late failure"))
    assert any(a["code"] == "conflicting_terminal" for a in h.engine.alerts)
    assert h.scan(sid)["analysis"] == "complete"       # a closed scan is not rewritten


# ---------------------------------------------------------------- measurement (inspection_node)
def detected(h, n=3, mode="csk_depth", partial_last=False):
    """A scan whose detection is complete (close + finalization) but not yet measured."""
    h.ready_inspection(mode, count=max(n, 1))
    sid = h.press()
    for i in range(n):
        h.result(sid, partial=partial_last and i == n - 1)
    h.release(sid)
    h.close(sid)
    h.finalize(sid, measure=False)
    return sid


def test_scan_completes_only_with_its_measurement_and_records_go_no_go():
    h = Harness()
    sid = detected(h, 4)
    assert h.scan(sid)["analysis"] == "pending" and screen(h) == "finishing_analysis"
    h.measure(sid, {1: 0.001, 2: 0.00095, 3: 0.0012, 4: "too_few_points"})
    v = h.scan(sid)
    assert v["analysis"] == "complete" and screen(h) == "results"
    assert v["measurement"] == {"in_limits": 2, "out_of_limits": 1, "not_measured": 1, "pending": 0}
    by_id = {f["feature_id"]: f["measurement"] for f in v["features"]}
    assert by_id[3]["verdict"] == "out_of_limits" and by_id[3]["value_mm"] == 1.2
    assert by_id[4]["status"] == "not_measured" and by_id[4]["reason"] == "too_few_points"
    assert h.repo.audit_events("measurement_finalized")
    h.engine.decide(sid, "accept")                  # out-of-limits features never block the decision
    assert h.scan(sid)["status"] == "accepted"


def test_partial_target_is_reported_not_measured():
    h = Harness()
    sid = detected(h, 3, partial_last=True)
    h.measure(sid)                                  # the node reports partial features as "partial"
    v = h.scan(sid)
    assert v["analysis"] == "complete" and v["partial_target_count"] == 1
    assert v["measurement"]["not_measured"] == 1
    assert [f["measurement"]["reason"] for f in v["features"]] == [None, None, "partial"]


@pytest.mark.parametrize("features, why", [
    ([MeasuredValue(1, True, 0.001), MeasuredValue(2, True, 0.001)], "not the scan's targets"),          # one missing
    ([MeasuredValue(i, True, 0.001) for i in (1, 2, 3, 4)], "not the scan's targets"),                    # one extra
    ([MeasuredValue(i, True, 0.001) for i in (1, 2, 2)], "not the scan's targets"),                       # duplicate
    ([MeasuredValue(1, True, 0.001), MeasuredValue(2, True, 0.001), MeasuredValue(3, True, float("nan"))],
     "implausible"),
])
def test_measurement_not_matching_the_targets_fails_the_scan(features, why):
    h = Harness()
    sid = detected(h, 3)
    h.measure(sid, features=features)
    v = h.scan(sid)
    assert v["analysis"] == "failed" and "invalid measurement" in v["analysis_error"] and why in v["analysis_error"]
    assert v["measurement"]["pending"] == 3          # nothing half-written


def test_measurement_of_the_other_quantity_fails_the_scan():
    h = Harness()
    sid = detected(h, 2)
    from laser_hhrp_ui.events import MeasurementFinalized
    h.handle(MeasurementFinalized(sid, "csk_depth", "flushness", -0.00015, 0.00005,
                                  (MeasuredValue(1, True, 0.0), MeasuredValue(2, True, 0.0))))
    assert h.scan(sid)["analysis"] == "failed"


def test_measurement_failure_fails_the_scan():
    h = Harness()
    sid = detected(h, 2)
    h.measure(sid, success=False, error="cannot read summary.csv")
    v = h.scan(sid)
    assert v["analysis"] == "failed" and v["analysis_error"] == "measurement failed: cannot read summary.csv"
    assert screen(h) == "analysis_failed"


def test_missing_measurement_times_out_and_names_it():
    h = Harness()
    sid = detected(h, 2)
    h.clock.advance(h.engine.fail_after_s + 1)
    h.engine.tick()
    v = h.scan(sid)
    assert v["analysis"] == "failed" and "missing measurement" in v["analysis_error"], v["analysis_error"]


def test_measurement_redelivery_ignored_conflict_fails():
    h = Harness()
    sid = detected(h, 2)
    h.finalize(sid, measure=False)                   # (identical detector finalization: ignored)
    first = MeasuredValue(1, True, 0.001)
    h.measure(sid, features=[first])                 # covers only feature 1: results still fine, so ...
    assert h.scan(sid)["analysis"] == "failed"       # ... the scan fails on the mismatch
    h2 = Harness()
    h2.ready_inspection(count=2)
    sid = h2.press()
    h2.results(sid, 2)
    h2.release(sid)
    h2.measure(sid)                                  # arrives before close and finalization: kept
    h2.measure(sid)                                  # identical: ignored
    assert h2.scan(sid)["analysis"] == "pending"
    h2.measure(sid, {1: 0.0012})                     # different content
    assert h2.scan(sid)["analysis"] == "failed" and "conflicting measurement" in h2.scan(sid)["analysis_error"]


def test_late_measurement_after_completion_identical_is_quiet_different_alerts():
    h = Harness()
    sid = detected(h, 2)
    h.measure(sid)
    assert h.scan(sid)["analysis"] == "complete"
    h.measure(sid)                                   # redelivered (transient local): same content
    assert not any(a["code"] == "conflicting_terminal" for a in h.engine.alerts)
    h.measure(sid, {2: 0.0013})
    assert any(a["code"] == "conflicting_terminal" for a in h.engine.alerts)
    assert h.scan(sid)["features"][1]["measurement"]["value_mm"] == 1.0   # a closed scan is not rewritten


def test_ros_needs_the_measurement_node_to_be_ready():
    h = Harness(source="ros")
    h.ready_inspection(count=5)
    assert "Measurement not running (inspection_node)" in h.engine.readiness()[1]
    h.handle(MeasurementStatus(available=True))
    assert h.engine.readiness()[0] and screen(h) == "ready"
    h.handle(MeasurementStatus(available=False))
    assert screen(h) == "not_ready"
    assert any(t == "system_state" for t in h.types())


def test_zero_target_scan_needs_an_empty_measurement():
    h = Harness()
    sid = detected(h, 0)
    assert h.scan(sid)["analysis"] == "pending"
    h.measure(sid)
    assert h.scan(sid)["analysis"] == "complete" and h.scan(sid)["measurement"]["in_limits"] == 0


# ---- default recording: per-feature legacy files after every scan (recording_export.py) ----

class FakeExport:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def export(self, scan, meta_only=False):
        self.calls.append((scan["scan_id"], meta_only))
        if self.fail:
            raise OSError("disk full")


@pytest.mark.parametrize("closed_first", [True, False])
def test_recording_exported_once_when_analysis_ended_and_raw_verified(tmp_path, closed_first):
    h = Harness()
    h.engine.recording_export = ex = FakeExport()
    h.ready_inspection(count=1)
    if closed_first:
        sid = recorded_scan(h, tmp_path)                  # raw CLOSED, then the analysis completes
    else:
        raw = tmp_path / "rec"
        write_frames_bin(raw, 20)
        start = (h.seq + 1) * 1000
        sid = h.press()
        h.handle(RecordingSession("OPENED", start, "s1", str(raw)))
        h.release(sid)
        h.close(sid, 20)
        h.finalize(sid, 20, emitted=0, targets=0)         # analysis complete before the recorder closes
        assert ex.calls == [], "not before the raw recording is closed"
        h.handle(RecordingSession("CLOSED", h.seq * 1000, "s1", str(raw), 20, False))
    assert h.scan(sid)["analysis"] == "complete"
    assert ex.calls == [(sid, False)]
    h.engine._maybe_export(sid)                           # never twice
    assert ex.calls == [(sid, False)]


def test_recording_refreshed_with_the_decision(tmp_path):
    h = Harness()
    h.engine.recording_export = ex = FakeExport()
    h.ready_inspection(count=1)
    sid = recorded_scan(h, tmp_path)
    h.engine.decide(sid, "accept")
    assert ex.calls == [(sid, False), (sid, True)]


def test_recording_also_for_a_failed_analysis_never_for_a_bad_raw_file(tmp_path):
    h = Harness()
    h.engine.recording_export = ex = FakeExport()
    h.ready_inspection(count=2)
    bad = recorded_scan(h, tmp_path / "a", truncate=True)
    assert h.scan(bad)["analysis"] == "complete" and not h.scan(bad)["replay_eligible"]
    assert ex.calls == [], "a damaged frames.bin is not exported"
    raw = tmp_path / "b"
    write_frames_bin(raw, 20)
    start = (h.seq + 1) * 1000
    sid = h.press()
    h.handle(RecordingSession("OPENED", start, "s2", str(raw)))
    h.release(sid)
    h.close(sid, 20)
    h.handle(RecordingSession("CLOSED", h.seq * 1000, "s2", str(raw), 20, False))
    h.finalize(sid, 20, success=False, error="detector crashed")
    assert h.scan(sid)["analysis"] == "failed"
    assert ex.calls == [(sid, False)]


def test_recording_failure_never_touches_the_scan(tmp_path):
    h = Harness()
    h.engine.recording_export = FakeExport(fail=True)
    h.ready_inspection(count=1)
    sid = recorded_scan(h, tmp_path)
    assert h.scan(sid)["analysis"] == "complete"
    assert h.engine.decide(sid, "accept")["decision"] == "accepted"
    assert len(h.repo.audit_events("recording_export_failed")) == 2       # the export and the decision refresh


def test_recording_export_command_runs_last_in_line():
    from pathlib import Path

    from laser_hhrp_ui.recording_export import RecordingExport
    ex = RecordingExport("/data/ui.db", Path("/ws/export_legacy.py"))
    cmd = ex.command("abc", "/scans/20261122-120000", False)
    assert cmd[:3] == ["nice", "-n", "19"]
    assert cmd[-7:] == ["/ws/export_legacy.py", "/scans/20261122-120000", "--db", "/data/ui.db", "--scan", "abc", "--flat"]
    assert ex.command("abc", "/x", True)[-1] == "--meta-only"


# ---- "Record bags": one ros2 bag record -a per scan (bag_recorder.py) ----

def test_bag_per_scan_opens_on_ready_closes_after_analysis():
    from laser_hhrp_ui.bag_recorder import FakeBag
    h = Harness()
    h.engine.bag = bag = FakeBag()
    h.ready_inspection(count=3, operator="ENG-1042", label="INS-20261122-001")
    h.engine.tick()
    assert bag.started == [] and bag.state == "idle", "off by default"
    h.engine.set_bag(True)
    assert bag.state == "recording" and bag.started == ["ENG-1042-INS-20261122-001-Scan-001"], "opens on Ready"
    sid = h.press()
    h.results(sid, 3)
    h.release(sid)
    h.engine.tick()
    assert bag.state == "recording", "kept through the scan and its analysis"
    h.close(sid, 500)
    h.finalize(sid, 500)
    h.engine.tick()
    assert bag.state == "idle", "closed once the analysis ended"
    h.engine.tick()
    assert bag.started == ["ENG-1042-INS-20261122-001-Scan-001"], "the next bag waits for Ready (decision first)"
    h.engine.decide(sid, "accept")
    h.engine.tick()
    assert bag.started[-1] == "ENG-1042-INS-20261122-001-Scan-002" and bag.state == "recording"
    with pytest.raises(CommandError):
        h.press()
        h.engine.set_bag(False)                         # not during a scan
    h.engine.tick()
    assert bag.state == "recording"


def test_bag_switch_refused_mid_scan_and_stops_on_low_storage():
    from laser_hhrp_ui.bag_recorder import FakeBag
    h = Harness()
    h.engine.bag = bag = FakeBag(free_bytes=4e9)
    h.ready_inspection(count=1)
    with pytest.raises(CommandError) as e:
        h.engine.set_bag(True)
    assert e.value.code == "low_storage" and not h.engine.bag_enabled
    bag.free = 30e9
    h.engine.set_bag(True)
    bag.error, bag.state = "stopped: less than 5 GB free", "idle"     # the recorder's guard stopped it
    h.engine.tick()
    assert not h.engine.bag_enabled and any(a["code"] == "bag_stopped" for a in h.engine.alerts)
    view = h.engine.snapshot()["bag"]
    assert view == {**view, "enabled": False, "error": "stopped: less than 5 GB free"}


def test_bag_unavailable_without_a_recorder():
    h = Harness()
    h.ready_inspection(count=1)
    with pytest.raises(CommandError) as e:
        h.engine.set_bag(True)
    assert e.value.code == "unavailable" and h.engine.snapshot()["bag"]["available"] is False


def test_bag_recorder_closes_with_sigint_and_never_overwrites(tmp_path):
    """The real BagRecorder around a stand-in for ros2 bag: readiness from its output, SIGINT to close."""
    import sys
    import time as _t
    from laser_hhrp_ui.bag_recorder import BagRecorder
    stub = tmp_path / "fake_bag.py"
    stub.write_text("import os, signal, sys, time\np = sys.argv[1]\nos.makedirs(p)\nprint('Recording...', flush=True)\n"
                    "def bye(*a):\n    open(os.path.join(p, 'closed'), 'w').write('ok')\n    sys.exit(0)\n"
                    "signal.signal(signal.SIGINT, bye)\nwhile True:\n    open(os.path.join(p, 'data'), 'a').write('x' * 1000)\n"
                    "    time.sleep(0.05)\n")
    rec = BagRecorder(tmp_path / "bags", min_free_bytes=0, sources=[], command=[sys.executable, str(stub)])
    for n in range(2):
        rec.start("ENG-1042-INS-1-Scan-001")
        end = _t.monotonic() + 10
        while rec.status()["state"] != "recording" and _t.monotonic() < end:
            _t.sleep(0.05)
        assert rec.status()["state"] == "recording"
        rec.stop()
        while rec.status()["state"] != "idle" and _t.monotonic() < end + 10:
            _t.sleep(0.05)
        assert rec.status()["state"] == "idle"
    rec.start("ENG-1042-INS-1-Scan-002")                 # the backend stops with a bag open
    end = _t.monotonic() + 10
    while rec.status()["state"] != "recording" and _t.monotonic() < end:
        _t.sleep(0.05)
    rec.shutdown()
    assert rec.status()["state"] == "idle" and (tmp_path / "bags" / "ENG-1042-INS-1-Scan-002" / "closed").is_file()
    with pytest.raises(RuntimeError):
        rec.start("ENG-1042-INS-1-Scan-003")             # no new bag once the backend is stopping
    names = sorted(p.name for p in (tmp_path / "bags").iterdir() if "002" not in p.name)
    assert names == ["ENG-1042-INS-1-Scan-001", "ENG-1042-INS-1-Scan-001_2"]
    assert all((tmp_path / "bags" / n / "closed").is_file() for n in names), "closed cleanly by SIGINT"


def test_bag_closed_cleanly_when_the_backend_dies_hard(tmp_path):
    """Backend killed (no shutdown): the kernel sends the bag SIGINT (PR_SET_PDEATHSIG) and it closes itself."""
    import subprocess
    import sys
    import time as _t
    stub = tmp_path / "fake_bag.py"
    stub.write_text("import os, signal, sys, time\np = sys.argv[1]\nos.makedirs(p)\nprint('Recording...', flush=True)\n"
                    "def bye(*a):\n    open(os.path.join(p, 'closed'), 'w').write('ok')\n    sys.exit(0)\n"
                    "signal.signal(signal.SIGINT, bye)\nwhile True:\n    time.sleep(0.05)\n")
    backend = ("import sys, time, os\nsys.path.insert(0, %r)\nfrom laser_hhrp_ui.bag_recorder import BagRecorder\n"
               "r = BagRecorder(%r, 0, sources=[], command=[sys.executable, %r])\nr.start('Scan-009')\n"
               "while r.status()['state'] != 'recording': time.sleep(0.05)\nos._exit(0)\n"
               % (str(Path(__file__).resolve().parents[1]), str(tmp_path / "bags"), str(stub)))
    subprocess.run([sys.executable, "-c", backend], timeout=30, check=True)
    end = _t.monotonic() + 10
    while not (tmp_path / "bags" / "Scan-009" / "closed").is_file() and _t.monotonic() < end:
        _t.sleep(0.05)
    assert (tmp_path / "bags" / "Scan-009" / "closed").is_file()
