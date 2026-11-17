"""Authoritative UI/inspection state (plan sections 4, 5, 8).

The Engine runs on one thread (the asyncio loop). It consumes immutable input events (events.py),
persists them, decides scan lifecycle and emits UI events through `emit(type, payload, scan_id,
inspection_id)`. Commands raise CommandError, which the API maps to HTTP status codes.

Completion barrier (section 5): a scan's analysis is complete only when acquisition ended, the
synchronizer's stream-close and the detector's terminal acknowledgement arrived with equal frame
counts, and every declared result sequence is committed to SQLite. Timeouts, errors and restarts
fail explicitly; expected-count equality is never completion.
"""

from __future__ import annotations

import uuid as uuidlib
from dataclasses import dataclass

from . import models
from .clock import Clock, iso
from .events import (DetectionFinalized, DetectorStatus, HardwareStatus, ScanTransition, StreamClosed,
                     TaggedResult)
from .models import Acquisition, Analysis, Decision, Mode, Screen

ACQUIRING = (Acquisition.SCANNING.value, Acquisition.PAUSED.value)
MIN_FREE_MB = 500.0


class CommandError(Exception):
    def __init__(self, status: int, code: str, message: str, field: str | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.field = status, code, message, field


@dataclass
class ScanRuntime:
    closed: StreamClosed | None = None
    finalized: DetectionFinalized | None = None
    released_mono: float | None = None
    warned: bool = False


class NullDetectorControl:
    """Phase 1 placeholder; Phase 2 supplies the process supervisor, the mock supplies a fake."""

    def request(self, mode: Mode) -> None:
        pass

    def stop(self) -> None:
        pass

    def replay(self, scan: dict) -> None:
        pass


class Engine:
    def __init__(self, repo, clock: Clock | None = None, emit=None, detector_control=None,
                 warn_after_s: float = 10.0, fail_after_s: float = 120.0, hardware_stale_s: float = 2.5,
                 orphan_limit: int = 2000, orphan_ttl_s: float = 30.0, source: str = "none"):
        self.repo = repo
        self.clock = clock or Clock()
        self.emit = emit or (lambda *a, **k: None)
        self.detector_control = detector_control or NullDetectorControl()
        self.warn_after_s, self.fail_after_s = warn_after_s, fail_after_s
        self.hardware_stale_s = hardware_stale_s
        self.orphan_limit, self.orphan_ttl_s = orphan_limit, orphan_ttl_s
        self.source = source                      # "none" | "mock" | "ros"
        self.hardware: HardwareStatus | None = None
        self.hardware_mono: float | None = None
        self.detector = DetectorStatus(mode=None, alive=False, ready=False)
        self.runtime: dict[str, ScanRuntime] = {}
        self.orphans: dict[str, list] = {}        # scan_id -> [(mono, event)] before the scan is known
        self.last_transition: dict[str, int] = {}
        self.alerts: list[dict] = []              # sticky operator-visible system alerts
        self._last_ready_key = None
        self._last_progress_mono = 0.0
        self._recover()

    # ------------------------------------------------------------------ helpers
    def _now(self) -> str:
        return iso(self.clock.now())

    def _audit(self, kind: str, **kw) -> None:
        self.repo.audit(kind, self._now(), self.clock.quality(), **kw)

    def _alert(self, code: str, message: str, scan_id: str | None = None) -> None:
        self.alerts = [a for a in self.alerts if a["code"] != code][-9:]
        self.alerts.append({"code": code, "message": message, "scan_id": scan_id, "at": self._now()})
        self._audit("alert", scan_id=scan_id, code=code, message=message)
        self._emit_system()

    def clear_alert(self, code: str) -> None:
        self.alerts = [a for a in self.alerts if a["code"] != code]

    @property
    def operator(self) -> dict | None:
        return self.repo.open_operator_session_row()

    @property
    def inspection(self) -> dict | None:
        return self.repo.open_inspection()

    def _recover(self) -> None:
        """Restart: never invent success. Acquiring scans become interrupted, pending analyses failed."""
        now = self._now()
        for s in self.repo.scans_where("acquisition IN ('scanning', 'paused')"):
            self.repo.update_scan(s["scan_id"], acquisition=Acquisition.INTERRUPTED.value,
                                  acquisition_note="backend restarted during acquisition", ended_at=None)
            self._audit("recovered_interrupted_acquisition", scan_id=s["scan_id"],
                        inspection_uuid=s["inspection_uuid"])
        for s in self.repo.scans_where("analysis = 'pending'"):
            self.repo.update_scan(s["scan_id"], analysis=Analysis.INTERRUPTED.value,
                                  analysis_error="backend restarted before analysis finished",
                                  analysis_completed_at=now)
            self._audit("recovered_interrupted_analysis", scan_id=s["scan_id"],
                        inspection_uuid=s["inspection_uuid"])

    def start(self) -> None:
        """After restart: the open inspection needs its detector again."""
        insp = self.inspection
        if insp:
            self.detector_control.request(Mode(insp["mode"]))

    # ------------------------------------------------------------------ readiness and screen
    def hardware_fresh(self) -> bool:
        return (self.hardware is not None and self.hardware_mono is not None
                and self.clock.monotonic() - self.hardware_mono <= self.hardware_stale_s)

    def readiness(self) -> tuple[bool, list[str], list[str]]:
        reasons, warnings = [], []
        insp = self.inspection
        hw = self.hardware
        if self.source == "none":
            reasons.append("ROS bridge not running")
        elif not self.hardware_fresh():
            reasons.append("No current hardware status")
        if hw is not None and self.hardware_fresh():
            if hw.scan_state not in ("IDLE",):
                reasons.append(f"Scanner state {hw.scan_state}")
            if not hw.calibrated:
                reasons.append("Calibration required")
            for f in hw.fatal:
                reasons.append(f"Fault: {f}")
            if not hw.laser_ok:
                reasons.append("Laser not connected")
            if not hw.nano_ok:
                reasons.append("Nano (encoder/IMU) not responding")
            elif not hw.encoder_ok:
                reasons.append("Encoder error")
            if hw.storage_write_failed:
                reasons.append("Cannot write scan data")
            if hw.free_mb is None:
                reasons.append("Storage status unknown")
            elif hw.free_mb < MIN_FREE_MB:
                reasons.append(f"Low storage: {hw.free_mb:.0f} MB free (minimum {MIN_FREE_MB:.0f} MB)")
            warnings.extend(hw.warnings)
        if insp:
            want = insp["mode"]
            if self.detector.mode != want or not self.detector.ready:
                reasons.append(f"Detector not ready{': ' + self.detector.error if self.detector.error else ''}")
            if insp["next_expected_count"] is None:
                reasons.append("Enter the expected feature count")
        if not self.repo.health()["ok"]:
            reasons.append("Database not writable")
        if self.clock.quality() == "unverified":
            warnings.append("Time not verified")
        for rt_id, rt in self.runtime.items():
            if rt.warned:
                warnings.append("Analysis is taking longer than expected")
                break
        return not reasons, reasons, warnings

    def screen(self) -> Screen:
        if not self.operator:
            return Screen.IDENTIFY
        insp = self.inspection
        if not insp:
            return Screen.SETUP
        s = self.repo.latest_scan_of(insp["uuid"])
        if s:
            if s["acquisition"] in ACQUIRING:
                return Screen.SCANNING
            if s["analysis"] == Analysis.PENDING.value:
                return Screen.FINISHING_ANALYSIS
            if s["decision"] is None and s["analysis"] == Analysis.COMPLETE.value:
                return Screen.RESULTS
            if s["decision"] is None and s["analysis"] in (Analysis.FAILED.value, Analysis.INTERRUPTED.value):
                return Screen.ANALYSIS_FAILED
        return Screen.READY if self.readiness()[0] else Screen.NOT_READY

    # ------------------------------------------------------------------ views
    @staticmethod
    def status_label(s: dict) -> str:
        if s["inspection_uuid"] is None:
            return "unassigned"
        if s["acquisition"] in ACQUIRING:
            return "scanning"
        if s["analysis"] == Analysis.PENDING.value:
            return "analyzing"
        d = s["decision"]
        if d == Decision.ACCEPTED.value:
            return "accepted_with_warning" if s["count_mismatch"] else "accepted"
        if d == Decision.RESCANNED.value:
            return "rescanned"
        if d == Decision.INCOMPLETE_RETAINED.value:
            return "incomplete_retained"
        if s["analysis"] == Analysis.COMPLETE.value:
            return "undecided"
        return "failed"

    def scan_view(self, s: dict, features: bool = False) -> dict:
        c = self.repo.feature_counts(s["scan_id"])
        v = {
            "scan_id": s["scan_id"], "scan_number": s["scan_number"],
            "display_name": f"Scan {s['scan_number']:03d}" if s["scan_number"] else "Unassigned scan",
            "inspection_uuid": s["inspection_uuid"], "mode": s["mode"], "operator_id": s["operator_id"],
            "expected_count": s["expected_count"],
            "detected_target_count": c["targets"], "non_target_count": c["total"] - c["targets"],
            "partial_target_count": c["partial_targets"],
            "count_mismatch": None if s["count_mismatch"] is None else bool(s["count_mismatch"]),
            "acquisition": s["acquisition"], "acquisition_note": s["acquisition_note"],
            "analysis": s["analysis"], "analysis_error": s["analysis_error"],
            "decision": s["decision"], "decided_by": s["decided_by"], "decided_at": s["decided_at"],
            "status": self.status_label(s), "unassigned_reason": s["unassigned_reason"],
            "started_at": s["started_at"], "ended_at": s["ended_at"], "clock_quality": s["clock_quality"],
            "frame_count": s["published_frame_count"], "replay_eligible": bool(s["replay_eligible"]),
        }
        if features:
            v["features"] = self.repo.features_of(s["scan_id"])
        return v

    @staticmethod
    def inspection_view(i: dict | None) -> dict | None:
        if not i:
            return None
        return {"uuid": i["uuid"], "label": i["label"], "mode": i["mode"], "status": i["status"],
                "operator_id": i["operator_id"], "next_expected_count": i["next_expected_count"],
                "created_at": i["created_at"], "closed_at": i["closed_at"], "clock_quality": i["clock_quality"],
                "scan_count": i["last_scan_number"]}

    def hardware_view(self) -> dict | None:
        hw = self.hardware
        if hw is None:
            return None
        return {"fresh": self.hardware_fresh(), "scan_state": hw.scan_state, "calibrated": hw.calibrated,
                "laser_ok": hw.laser_ok, "nano_ok": hw.nano_ok, "encoder_ok": hw.encoder_ok,
                "free_mb": hw.free_mb, "storage_write_failed": hw.storage_write_failed,
                "distance_mm": round(hw.position_m * 1000.0, 1), "message": hw.message,
                "fatal": list(hw.fatal), "warnings": list(hw.warnings)}

    def snapshot(self) -> dict:
        insp = self.inspection
        ready, reasons, warnings = self.readiness()
        current = self.repo.latest_scan_of(insp["uuid"]) if insp else None
        history = list(reversed(self.repo.scans_of(insp["uuid"])))[:50] if insp else []
        op = self.operator
        return {
            "screen": self.screen().value,
            "operator": {"operator_id": op["operator_id"], "started_at": op["started_at"]} if op else None,
            "inspection": self.inspection_view(insp),
            "ready": ready, "not_ready_reasons": reasons, "warnings": warnings, "alerts": list(self.alerts),
            "hardware": self.hardware_view(),
            "detector": {"mode": self.detector.mode, "alive": self.detector.alive,
                         "ready": self.detector.ready, "error": self.detector.error},
            "source": self.source, "clock_quality": self.clock.quality(),
            "current_scan": self.scan_view(current, features=True) if current else None,
            "recent_scans": [self.scan_view(s) for s in history],
            "pending_scans": [self.scan_view(s) for s in self.repo.scans_where("analysis = 'pending'")],
        }

    # ------------------------------------------------------------------ emit helpers
    def _emit_system(self, force: bool = True) -> None:
        ready, reasons, warnings = self.readiness()
        key = (ready, tuple(reasons), tuple(warnings), self.screen().value, len(self.alerts))
        if not force and key == self._last_ready_key:
            return
        self._last_ready_key = key
        self.emit("system_state", {"screen": key[3], "ready": ready, "not_ready_reasons": reasons,
                                   "warnings": warnings, "alerts": list(self.alerts),
                                   "hardware": self.hardware_view(),
                                   "detector": {"mode": self.detector.mode, "alive": self.detector.alive,
                                                "ready": self.detector.ready, "error": self.detector.error}})

    def _emit_scan(self, kind: str, s: dict, **extra) -> None:
        payload = self.scan_view(s)
        payload.update(extra)
        self.emit(kind, payload, scan_id=s["scan_id"], inspection_id=s["inspection_uuid"])

    # ------------------------------------------------------------------ input events
    def handle(self, e) -> None:
        if isinstance(e, ScanTransition):
            self._on_transition(e)
        elif isinstance(e, TaggedResult):
            self._on_result(e)
        elif isinstance(e, StreamClosed):
            self._on_closed(e)
        elif isinstance(e, DetectionFinalized):
            self._on_finalized(e)
        elif isinstance(e, HardwareStatus):
            self._on_hardware(e)
        elif isinstance(e, DetectorStatus):
            self._on_detector(e)
        else:
            raise TypeError(f"unknown event {type(e).__name__}")

    def _orphan(self, scan_id: str, e) -> None:
        total = sum(len(v) for v in self.orphans.values())
        if total >= self.orphan_limit:
            self._audit("orphan_dropped", scan_id=scan_id, event=type(e).__name__)
            self._alert("orphan_overflow", "Detections arrived for an unknown scan and were dropped", scan_id)
            return
        self.orphans.setdefault(scan_id, []).append((self.clock.monotonic(), e))

    def _on_transition(self, e: ScanTransition) -> None:
        last = self.last_transition.get(e.controller_instance_id)
        if last is not None and e.transition_seq <= last:
            return                                            # duplicate or stale delivery
        if last is not None and e.transition_seq != last + 1:
            self._alert("transition_gap", f"Missed {e.transition_seq - last - 1} scan transition(s)", e.scan_id)
        self.last_transition[e.controller_instance_id] = e.transition_seq
        scan = self.repo.scan(e.scan_id)
        if e.state == "SCANNING":
            if scan is None:
                for other in self.repo.scans_where("acquisition IN ('scanning', 'paused')"):
                    self._end_acquisition(other, Acquisition.ENDED, "UNKNOWN",
                                          "stop transition missing; ended by the next scan start")
                self._start_scan(e)
            elif scan["acquisition"] == Acquisition.PAUSED.value:
                scan = self.repo.update_scan(e.scan_id, acquisition=Acquisition.SCANNING.value)
                self._emit_scan("scan_progress", scan, phase="resumed")
        elif e.state == "PAUSED":
            if scan and scan["acquisition"] == Acquisition.SCANNING.value:
                scan = self.repo.update_scan(e.scan_id, acquisition=Acquisition.PAUSED.value)
                self._emit_scan("scan_progress", scan, phase="paused")
        elif e.state in ("IDLE", "ERROR"):
            if scan is None:
                self._audit("stop_for_unknown_scan", scan_id=e.scan_id, state=e.state)
                return
            if scan["acquisition"] in ACQUIRING:
                acq = Acquisition.ERROR if e.state == "ERROR" else Acquisition.ENDED
                self._end_acquisition(scan, acq, e.state, e.message or None)
        else:
            self._audit("unknown_transition_state", scan_id=e.scan_id, state=e.state)

    def _start_scan(self, e: ScanTransition) -> None:
        op, insp = self.operator, self.inspection
        reason = None
        if not op:
            reason = "no operator identified"
        elif not insp:
            reason = "no open inspection"
        elif self.detector.mode != insp["mode"] or not self.detector.ready:
            reason = "detector not ready"
        common = dict(scan_id=e.scan_id, controller_instance_id=e.controller_instance_id,
                      transition_seq=e.transition_seq, start_stamp_ns=e.status_stamp_ns,
                      started_at=self._now(), clock_quality=self.clock.quality(),
                      acquisition=Acquisition.SCANNING.value)
        if reason:
            scan = self.repo.create_scan(
                **common, inspection_uuid=None, unassigned_reason=reason,
                operator_id=op["operator_id"] if op else None, analysis=Analysis.FAILED.value,
                analysis_error=f"unassigned: {reason}")
            self._alert("unassigned_scan", f"Scan started but not recorded to an inspection: {reason}",
                        e.scan_id)
            self._emit_scan("scan_progress", scan, phase="started")
            self.orphans.pop(e.scan_id, None)
            return
        number = self.repo.allocate_scan_number(insp["uuid"])
        scan = self.repo.create_scan(
            **common, inspection_uuid=insp["uuid"], scan_number=number, operator_id=op["operator_id"],
            mode=insp["mode"], expected_count=insp["next_expected_count"], analysis=Analysis.PENDING.value)
        self.runtime[e.scan_id] = ScanRuntime()
        self.clear_alert("unassigned_scan")
        self._emit_scan("scan_progress", scan, phase="started")
        for _, early in self.orphans.pop(e.scan_id, []):
            self.handle(early)

    def _end_acquisition(self, scan: dict, acq: Acquisition, stop_state: str, note: str | None) -> None:
        scan = self.repo.update_scan(scan["scan_id"], acquisition=acq.value, stop_state=stop_state,
                                     ended_at=self._now(), acquisition_note=note)
        rt = self.runtime.get(scan["scan_id"])
        if rt:
            rt.released_mono = self.clock.monotonic()
        self._emit_scan("scan_acquisition_ended", scan, stop_state=stop_state)
        self._try_complete(scan["scan_id"])

    def _assigned_pending(self, scan_id: str, e) -> dict | None:
        """The scan an event belongs to, or None after handling unknown / unassigned / closed cases."""
        scan = self.repo.scan(scan_id)
        if scan is None:
            self._orphan(scan_id, e)
            return None
        if scan["inspection_uuid"] is None:
            self._audit("event_for_unassigned_scan", scan_id=scan_id, event=type(e).__name__)
            return None
        if scan["analysis"] != Analysis.PENDING.value:
            self._audit("event_after_analysis_closed", scan_id=scan_id, event=type(e).__name__,
                        analysis=scan["analysis"])
            return None
        return scan

    def _on_result(self, e: TaggedResult) -> None:
        scan = self._assigned_pending(e.scan_id, e)
        if scan is None:
            return
        if e.detector_mode != scan["mode"]:
            self._audit("result_mode_mismatch", scan_id=e.scan_id, detector_mode=e.detector_mode,
                        scan_mode=scan["mode"])
            return
        m = Mode(scan["mode"])
        try:
            if e.result_seq < 0:
                raise models.ValidationError("result_seq", "negative result_seq")
            raw = e.countersink_depth_m if m == Mode.CSK_DEPTH else e.flushness_m
            feature = models.feature_from_result(m, e.feature_type, e.feature_id, e.position_m, e.detail, raw)
        except (models.ValidationError, TypeError, ValueError) as err:
            self._fail(e.scan_id, f"invalid detector result #{e.result_seq}: {err}")
            return
        if not self.repo.add_feature(e.scan_id, e.detector_mode, e.result_seq, feature, e.detail, self._now()):
            same = [f for f in self.repo.features_of(e.scan_id) if f["result_seq"] == e.result_seq]
            if not (same and same[0]["feature_id"] == e.feature_id):
                self._fail(e.scan_id, f"conflicting detector result #{e.result_seq} (feature {e.feature_id})")
            return                                            # exact duplicate delivery: idempotent
        c = self.repo.feature_counts(e.scan_id)
        feature["result_seq"] = e.result_seq
        self.emit("feature_detected", {**feature, "detected_target_count": c["targets"],
                                       "non_target_count": c["total"] - c["targets"],
                                       "expected_count": scan["expected_count"],
                                       "after_release": scan["acquisition"] not in ACQUIRING},
                  scan_id=e.scan_id, inspection_id=scan["inspection_uuid"])
        self._try_complete(e.scan_id)

    def _on_closed(self, e: StreamClosed) -> None:
        scan = self._assigned_pending(e.scan_id, e)
        if scan is None:
            return
        rt = self.runtime.setdefault(e.scan_id, ScanRuntime())
        if rt.closed is not None:
            return
        rt.closed = e
        self.repo.update_scan(e.scan_id, published_frame_count=e.frame_count)
        if e.error:
            self._audit("stream_closed_error", scan_id=e.scan_id, error=e.error)
        self._try_complete(e.scan_id)

    def _on_finalized(self, e: DetectionFinalized) -> None:
        scan = self._assigned_pending(e.scan_id, e)
        if scan is None:
            return
        rt = self.runtime.setdefault(e.scan_id, ScanRuntime())
        if rt.finalized is not None:
            if rt.finalized != e:
                self._audit("conflicting_finalization", scan_id=e.scan_id)
            return
        rt.finalized = e
        self.repo.update_scan(e.scan_id, emitted_result_count=e.emitted_result_count)
        self._audit("detection_finalized", scan_id=e.scan_id, success=e.success, error=e.error,
                    frame_count=e.frame_count, emitted=e.emitted_result_count,
                    detector_targets=e.detected_target_count)
        if not e.success:
            self._fail(e.scan_id, f"detector reported failure: {e.error or 'no reason given'}")
            return
        self._try_complete(e.scan_id)

    def _try_complete(self, scan_id: str) -> None:
        scan = self.repo.scan(scan_id)
        if scan is None or scan["analysis"] != Analysis.PENDING.value or scan["acquisition"] in ACQUIRING:
            return
        rt = self.runtime.get(scan_id)
        if rt is None or rt.finalized is None or rt.closed is None:
            return
        f, c = rt.finalized, rt.closed
        if f.detector_mode != scan["mode"]:
            return self._fail(scan_id, f"finalized by {f.detector_mode}, scan mode {scan['mode']}")
        if f.frame_count != c.frame_count:
            return self._fail(scan_id, f"frame count mismatch: synchronizer published {c.frame_count}, "
                                       f"detector processed {f.frame_count}")
        counts = self.repo.feature_counts(scan_id)
        if counts["total"] > f.emitted_result_count or counts["max_seq"] >= f.emitted_result_count:
            return self._fail(scan_id, f"received results beyond the {f.emitted_result_count} declared")
        if counts["total"] < f.emitted_result_count:
            return                                            # results still in flight: wait (watchdog)
        if counts["targets"] != f.detected_target_count:
            return self._fail(scan_id, f"target count mismatch: backend {counts['targets']}, "
                                       f"detector {f.detected_target_count}")
        if scan["acquisition"] == Acquisition.ERROR.value:
            return self._fail(scan_id, f"acquisition stopped by error{': ' + scan['acquisition_note'] if scan['acquisition_note'] else ''}")
        expected = scan["expected_count"]
        mismatch = None if expected is None else int(counts["targets"] != expected)
        scan = self.repo.update_scan(scan_id, analysis=Analysis.COMPLETE.value,
                                     detected_target_count=counts["targets"], count_mismatch=mismatch,
                                     analysis_completed_at=self._now())
        self.runtime.pop(scan_id, None)
        self._audit("analysis_complete", scan_id=scan_id, inspection_uuid=scan["inspection_uuid"],
                    targets=counts["targets"], expected=expected)
        self._emit_scan("analysis_complete", scan)
        self._emit_system()

    def _fail(self, scan_id: str, reason: str) -> None:
        scan = self.repo.scan(scan_id)
        if scan is None or scan["analysis"] != Analysis.PENDING.value:
            return
        scan = self.repo.update_scan(scan_id, analysis=Analysis.FAILED.value, analysis_error=reason,
                                     analysis_completed_at=self._now())
        self.runtime.pop(scan_id, None)
        self._audit("analysis_failed", scan_id=scan_id, inspection_uuid=scan["inspection_uuid"], reason=reason)
        self._emit_scan("analysis_failed", scan, reason=reason)
        self._emit_system()

    def _on_hardware(self, e: HardwareStatus) -> None:
        self.hardware, self.hardware_mono = e, self.clock.monotonic()
        self._emit_system(force=False)
        insp = self.inspection
        now = self.clock.monotonic()
        if insp and now - self._last_progress_mono >= 0.2:
            s = self.repo.latest_scan_of(insp["uuid"])
            if s and s["acquisition"] in ACQUIRING:
                self._last_progress_mono = now
                self.emit("scan_progress", {"scan_id": s["scan_id"], "phase": "progress",
                                            "distance_mm": round(e.position_m * 1000.0, 1)},
                          scan_id=s["scan_id"], inspection_id=insp["uuid"])

    def _on_detector(self, e: DetectorStatus) -> None:
        prev, self.detector = self.detector, e
        if prev.alive and not e.alive and prev.mode:
            for s in self.repo.scans_where("analysis = 'pending' AND mode = ?", prev.mode):
                self._fail(s["scan_id"], f"detector stopped before finalizing{': ' + e.error if e.error else ''}")
            if self.inspection and self.inspection["mode"] == prev.mode:
                self._alert("detector_stopped", f"Detector stopped{': ' + e.error if e.error else ''}")
        if e.ready:
            self.clear_alert("detector_stopped")
        self._emit_system()

    def ingress_overflow(self, dropped: int) -> None:
        """Events were dropped between ROS and the engine: pending results may be incomplete."""
        self._alert("event_overflow", f"{dropped} ROS event(s) dropped: analysis results may be incomplete")
        for s in self.repo.scans_where("analysis = 'pending'"):
            self._fail(s["scan_id"], "ROS event queue overflow: results may be lost")

    def tick(self) -> None:
        """Watchdog (plan: warn 10 s, fail 120 s after release; never a fixed-delay success)."""
        now = self.clock.monotonic()
        for scan_id, rt in list(self.runtime.items()):
            if rt.released_mono is None:
                continue
            waited = now - rt.released_mono
            if waited >= self.fail_after_s:
                missing = [n for n, ok in (("stream close", rt.closed), ("detector finalization", rt.finalized))
                           if ok is None] or ["detector results"]
                self._fail(scan_id, f"analysis timeout after {waited:.0f} s (missing {', '.join(missing)})")
            elif waited >= self.warn_after_s and not rt.warned:
                rt.warned = True
                self._emit_system()
        for scan_id, items in list(self.orphans.items()):
            fresh = [(t, e) for t, e in items if now - t < self.orphan_ttl_s]
            if len(fresh) != len(items):
                self._audit("orphan_expired", scan_id=scan_id, count=len(items) - len(fresh))
            if fresh:
                self.orphans[scan_id] = fresh
            else:
                del self.orphans[scan_id]

    # ------------------------------------------------------------------ commands
    def _pending_work(self) -> bool:
        return bool(self.repo.scans_where("analysis = 'pending' OR acquisition IN ('scanning', 'paused')"))

    def set_operator(self, raw) -> dict:
        oid = models.operator_id(raw)
        op = self.operator
        if op and op["operator_id"] == oid:
            return self.snapshot()
        if op:
            if self.inspection or self._pending_work():
                raise CommandError(409, "inspection_active", "Close the inspection before changing operator")
            self.repo.close_operator_session(op["id"], self._now())
        self.repo.open_operator_session(oid, self._now(), self.clock.quality())
        self._audit("operator_identified", actor=oid)
        self._emit_system()
        return self.snapshot()

    def logout(self) -> dict:
        op = self.operator
        if not op:
            return self.snapshot()
        if self.inspection:
            raise CommandError(409, "inspection_active", "Close the inspection before logging out")
        if self._pending_work():
            raise CommandError(409, "analysis_pending", "Wait for analysis to finish before logging out")
        self.repo.close_operator_session(op["id"], self._now())
        self._audit("operator_logout", actor=op["operator_id"])
        self._emit_system()
        return self.snapshot()

    def _generate_label(self) -> str:
        if self.clock.quality() in ("synced", "manually_set"):
            prefix = "INS-" + self.clock.now().strftime("%Y%m%d") + "-"
            width = 3
        else:
            prefix, width = "INS-LOCAL-", 6
        n = self.repo.count_labels_like(prefix) + 1
        while self.repo.label_taken(f"{prefix}{n:0{width}d}"):
            n += 1
        return f"{prefix}{n:0{width}d}"

    def create_inspection(self, raw_mode, label=None, request_id=None) -> dict:
        if request_id is not None:
            if not isinstance(request_id, str) or not 1 <= len(request_id) <= 64:
                raise models.ValidationError("request_id", "request_id: 1..64 characters")
            existing = self.repo.inspection_by_request(request_id)
            if existing:
                return self.inspection_view(existing)
        op = self.operator
        if not op:
            raise CommandError(409, "no_operator", "Identify the operator first")
        if self.inspection:
            raise CommandError(409, "inspection_active", "An inspection is already open")
        m = models.mode(raw_mode)
        label = self._generate_label() if label in (None, "") else models.inspection_label(label)
        if self.repo.label_taken(label):
            raise CommandError(409, "label_taken", f"Inspection ID {label} already exists", "label")
        insp = self.repo.create_inspection(
            uuid=str(uuidlib.uuid4()), label=label, mode=m.value, operator_session_id=op["id"],
            operator_id=op["operator_id"], next_expected_count=self.repo.last_expected_count(),
            status="open", created_at=self._now(), clock_quality=self.clock.quality(),
            create_request_id=request_id)
        self._audit("inspection_created", inspection_uuid=insp["uuid"], actor=op["operator_id"],
                    label=label, mode=m.value)
        self.detector_control.request(m)
        self.emit("inspection_updated", self.inspection_view(insp), inspection_id=insp["uuid"])
        self._emit_system()
        return self.inspection_view(insp)

    def _open_inspection(self, uuid: str) -> dict:
        insp = self.repo.inspection(uuid)
        if not insp:
            raise CommandError(404, "not_found", "No such inspection")
        if insp["status"] != "open":
            raise CommandError(409, "inspection_closed", "The inspection is closed")
        return insp

    def set_next_expected_count(self, uuid: str, raw) -> dict:
        insp = self._open_inspection(uuid)
        count = models.expected_count(raw)
        if self.screen() not in (Screen.READY, Screen.NOT_READY):
            raise CommandError(409, "not_ready_screen", "The expected count can only be changed in Ready")
        self.repo.update_inspection(uuid, next_expected_count=count)
        insp = self.repo.inspection(uuid)
        self._audit("expected_count_set", inspection_uuid=uuid, actor=self.operator["operator_id"], count=count)
        self.emit("inspection_updated", self.inspection_view(insp), inspection_id=uuid)
        self._emit_system()
        return self.inspection_view(insp)

    def end_inspection(self, uuid: str) -> dict:
        insp = self.repo.inspection(uuid)
        if not insp:
            raise CommandError(404, "not_found", "No such inspection")
        if insp["status"] == "closed":
            return self.inspection_view(insp)
        busy = self.repo.scans_where(
            "inspection_uuid = ? AND (analysis = 'pending' OR acquisition IN ('scanning', 'paused'))", uuid)
        if busy:
            raise CommandError(409, "analysis_pending", "Wait until scanning and analysis have finished")
        self.repo.update_inspection(uuid, status="closed", closed_at=self._now())
        insp = self.repo.inspection(uuid)
        op = self.operator
        self._audit("inspection_closed", inspection_uuid=uuid, actor=op["operator_id"] if op else None)
        self.detector_control.stop()
        self.emit("inspection_updated", self.inspection_view(insp), inspection_id=uuid)
        self._emit_system()
        return self.inspection_view(insp)

    def _decidable(self, scan_id: str) -> dict:
        scan = self.repo.scan(scan_id)
        if not scan:
            raise CommandError(404, "not_found", "No such scan")
        if scan["inspection_uuid"] is None:
            raise CommandError(409, "unassigned", "The scan belongs to no inspection")
        if not self.operator:
            raise CommandError(409, "no_operator", "Identify the operator first")
        return scan

    def _record_decision(self, scan: dict, decision: Decision, kind: str) -> dict:
        actor = self.operator["operator_id"]
        scan = self.repo.update_scan(scan["scan_id"], decision=decision.value, decided_by=actor,
                                     decided_at=self._now())
        self._audit(kind, scan_id=scan["scan_id"], inspection_uuid=scan["inspection_uuid"], actor=actor,
                    decision=decision.value, expected=scan["expected_count"],
                    detected=scan["detected_target_count"], count_mismatch=scan["count_mismatch"])
        self._emit_scan("scan_decision", scan)
        self._emit_system()
        return self.scan_view(scan)

    def decide(self, scan_id: str, raw) -> dict:
        choice = {"accept": Decision.ACCEPTED, "rescan": Decision.RESCANNED}.get(raw)
        if choice is None:
            raise models.ValidationError("decision", "decision must be 'accept' or 'rescan'")
        scan = self._decidable(scan_id)
        if scan["decision"] is not None:
            if scan["decision"] == choice.value:
                return self.scan_view(scan)
            raise CommandError(409, "already_decided", f"Scan already {scan['decision']}")
        if scan["analysis"] != Analysis.COMPLETE.value:
            raise CommandError(409, "analysis_not_complete",
                               "Only completed analyses can be accepted or rescanned here")
        return self._record_decision(scan, choice, "scan_decision")

    def failure_action(self, scan_id: str, raw) -> dict:
        if raw not in ("retry", "rescan", "keep_incomplete"):
            raise models.ValidationError("action", "action must be retry, rescan or keep_incomplete")
        scan = self._decidable(scan_id)
        if scan["analysis"] not in (Analysis.FAILED.value, Analysis.INTERRUPTED.value):
            raise CommandError(409, "not_failed", "The scan's analysis has not failed")
        target = {"rescan": Decision.RESCANNED, "keep_incomplete": Decision.INCOMPLETE_RETAINED}.get(raw)
        if scan["decision"] is not None:
            if target is not None and scan["decision"] == target.value:
                return self.scan_view(scan)
            raise CommandError(409, "already_decided", f"Scan already {scan['decision']}")
        if raw == "retry":
            if not scan["replay_eligible"]:
                raise CommandError(409, "not_replayable", "No verified raw recording: Rescan or Keep Incomplete")
            scan = self.repo.update_scan(scan_id, analysis=Analysis.PENDING.value, analysis_error=None,
                                         analysis_completed_at=None)
            self.runtime[scan_id] = ScanRuntime(released_mono=self.clock.monotonic())
            self._audit("analysis_retry", scan_id=scan_id, inspection_uuid=scan["inspection_uuid"],
                        actor=self.operator["operator_id"])
            self.detector_control.replay(scan)
            self._emit_scan("scan_progress", scan, phase="retry")
            self._emit_system()
            return self.scan_view(scan)
        return self._record_decision(scan, target, "failure_action")

    # ------------------------------------------------------------------ history
    def inspections(self, limit: int = 50) -> list[dict]:
        return [self.inspection_view(i) for i in self.repo.list_inspections(limit)]

    def scans(self, inspection_uuid: str) -> list[dict]:
        if not self.repo.inspection(inspection_uuid):
            raise CommandError(404, "not_found", "No such inspection")
        return [self.scan_view(s) for s in self.repo.scans_of(inspection_uuid)]

    def scan_detail(self, scan_id: str) -> dict:
        s = self.repo.scan(scan_id)
        if not s:
            raise CommandError(404, "not_found", "No such scan")
        return self.scan_view(s, features=True)
