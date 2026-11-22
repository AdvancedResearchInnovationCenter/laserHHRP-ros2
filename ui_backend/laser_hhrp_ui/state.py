"""Authoritative UI/inspection state (plan sections 4, 5, 8).

The Engine runs on one thread (the asyncio loop). It consumes immutable input events (events.py),
persists them, decides scan lifecycle and emits UI events through `emit(type, payload, scan_id,
inspection_id)`. Commands raise CommandError, which the API maps to HTTP status codes.

Completion barrier (section 5): a scan's analysis is complete only when acquisition ended, the
synchronizer's stream-close and the detector's terminal acknowledgement arrived with equal frame
counts, every declared result sequence is committed to SQLite, and inspection_node's measurement
of the scan arrived for exactly its target features (MEASUREMENT_INTEGRATION.md). Timeouts, errors
and restarts fail explicitly; expected-count equality is never completion.
"""

from __future__ import annotations

import uuid as uuidlib
from dataclasses import dataclass

from . import models, raw_recording
from .bag_recorder import NullBag
from .device import NoDevice
from .clock import Clock, iso
from .events import (DetectionFinalized, DetectorStatus, HardwareStatus, LegacyResult, MeasurementFinalized,
                     MeasurementStatus, RecordingSession, ScanTransition, StreamClosed, TaggedResult)
from .models import Acquisition, Analysis, Decision, Mode, Screen

ACQUIRING = (Acquisition.SCANNING.value, Acquisition.PAUSED.value)
HW_ACQUIRING = ("SCANNING", "PAUSED")
UNCONFIRMED = "UNCONFIRMED"   # stop inferred from the next scan's start, not confirmed by the controller
PREVIEW_NOTICE = ("Preview: live detections are shown but not recorded. Scan identity and analysis "
                  "completion arrive in Phase 3.")
PREVIEW_FEATURE_LIMIT = 1000
MIN_FREE_MB = 500.0


class CommandError(Exception):
    def __init__(self, status: int, code: str, message: str, field: str | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.field = status, code, message, field


@dataclass
class ScanRuntime:
    closed: StreamClosed | None = None
    finalized: DetectionFinalized | None = None
    measured: MeasurementFinalized | None = None
    released_mono: float | None = None
    warned: bool = False


class NullDetectorControl:
    """Phase 1 placeholder; Phase 2 supplies the process supervisor, the mock supplies a fake."""

    def request(self, mode: Mode, inspection_uuid: str | None = None) -> None:
        pass

    def stop(self) -> None:
        pass

    def replay(self, scan: dict) -> None:
        pass


class NullRecordingExport:
    """No per-feature recording (tests, mock); the server supplies recording_export.RecordingExport with ROS."""

    def export(self, scan: dict, meta_only: bool = False) -> None:
        pass


class Engine:
    def __init__(self, repo, clock: Clock | None = None, emit=None, detector_control=None,
                 warn_after_s: float = 10.0, fail_after_s: float = 120.0, hardware_stale_s: float = 2.5,
                 orphan_limit: int = 2000, orphan_ttl_s: float = 30.0, source: str = "none"):
        self.repo = repo
        self.clock = clock or Clock()
        self.emit = emit or (lambda *a, **k: None)
        self.detector_control = detector_control or NullDetectorControl()
        self.recording_export = NullRecordingExport()   # per-feature legacy files after each scan, set by the server
        self.exported: set[str] = set()           # scans handed to recording_export (this run)
        self.bag = NullBag()                      # ros2 bag record -a per scan (bag_recorder.py), set by the server
        self.bag_enabled = False                  # the screens' "Record bags" switch (off after a restart)
        self.bag_scan: tuple[str, int] | None = None   # (inspection uuid, scan number) the running bag is for
        self._bag_key = None
        self.device = NoDevice()                  # calibration / power off (device.py), set by the server
        self.powering_off = False                 # the operator turned the device off: nothing else is accepted
        self.power_action = "off"                 # "off" or "reboot": what powering_off is doing
        self.service = None                       # developer service menu (device.Service), set by the server
        self.switching: str | None = None         # "desktop" / "restart" requested from the service menu
        self.warn_after_s, self.fail_after_s = warn_after_s, fail_after_s
        self.hardware_stale_s = hardware_stale_s
        self.orphan_limit, self.orphan_ttl_s = orphan_limit, orphan_ttl_s
        self.source = source                      # "none" | "mock" | "ros"
        self.hardware: HardwareStatus | None = None
        self.hardware_mono: float | None = None
        self.detector = DetectorStatus(mode=None, alive=False, ready=False)
        self.measurement_available: bool | None = None   # inspection_node in the ROS graph (None: not known yet)
        self.runtime: dict[str, ScanRuntime] = {}
        self.orphans: dict[str, list] = {}        # scan_id -> [(mono, event)] before the scan is known
        self.last_transition: dict[str, int] = {}
        self.alerts: list[dict] = []              # sticky operator-visible system alerts
        self._last_ready_key = None
        self._last_progress_mono = 0.0
        self.preview: dict | None = None          # Phase 2: live view of the current physical scan
        self.preview_count = 0
        self.pending_recordings: dict[int, RecordingSession] = {}   # OPENED before its scan is known
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
            self.detector_control.request(Mode(insp["mode"]), insp["uuid"])

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
            if hw.calibrating:
                reasons.append("Calibrating: keep the device still")
            elif not hw.calibrated:
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
            if self.source == "ros" and self.measurement_available is not True:
                reasons.append("Measurement not running (inspection_node)")
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
        if self.preview_mode and self.hardware_fresh() and self.hardware.scan_state in HW_ACQUIRING:
            return Screen.SCANNING
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
        mc = self.repo.measurement_counts(s["scan_id"])
        v = {
            "scan_id": s["scan_id"], "scan_number": s["scan_number"],
            "display_name": f"Scan {s['scan_number']:03d}" if s["scan_number"] else "Unassigned scan",
            "inspection_uuid": s["inspection_uuid"], "mode": s["mode"], "operator_id": s["operator_id"],
            "expected_count": s["expected_count"],
            "detected_target_count": c["targets"], "non_target_count": c["total"] - c["targets"],
            "partial_target_count": c["partial_targets"],
            # target features by measurement outcome (GO / NO-GO against the limits inspection_node applied)
            "measurement": mc,
            "count_mismatch": None if s["count_mismatch"] is None else bool(s["count_mismatch"]),
            "acquisition": s["acquisition"], "acquisition_note": s["acquisition_note"],
            "analysis": s["analysis"], "analysis_error": s["analysis_error"],
            "decision": s["decision"], "decided_by": s["decided_by"], "decided_at": s["decided_at"],
            "status": self.status_label(s), "unassigned_reason": s["unassigned_reason"],
            "started_at": s["started_at"], "ended_at": s["ended_at"], "clock_quality": s["clock_quality"],
            "frame_count": s["published_frame_count"], "replay_eligible": bool(s["replay_eligible"]),
            # Retry Analysis is offered only when it can actually run (plan section 4.5)
            "retry_available": bool(s["replay_eligible"]) and bool(getattr(self.detector_control, "can_replay",
                                                                          False)),
            "raw": {"session_id": s["raw_session_id"], "dir": s["raw_dir"], "state": s["raw_state"],
                    "frame_count": s["raw_frame_count"], "note": s["raw_note"]},
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
                "calibrating": hw.calibrating,
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
            "device": self.device_view(),
            "current_scan": self.scan_view(current, features=True) if current else None,
            "recent_scans": [self.scan_view(s) for s in history],
            "pending_scans": [self.scan_view(s) for s in self.repo.scans_where("analysis = 'pending'")],
            "preview": self.preview_view(),
            "preview_notice": PREVIEW_NOTICE if self.preview_mode else None,
            "bag": self.bag_view(),
        }

    def bag_view(self) -> dict:
        st = self.bag.status()
        free = self.bag.free_bytes() if self.bag.available else None
        return {"available": bool(self.bag.available), "enabled": self.bag_enabled, "state": st["state"],
                "name": st["name"], "bytes": st["bytes"], "error": st["error"], "free_bytes": free}

    def device_view(self) -> dict:
        return {"can_calibrate": bool(self.device.can_calibrate), "can_power_off": bool(self.device.can_power_off),
                "powering_off": self.powering_off, "power_action": self.power_action if self.powering_off else None,
                "can_reboot": bool(getattr(self.device, "can_reboot", False)),
                "can_service": bool(self.service is not None and self.service.available), "switching": self.switching}

    # ------------------------------------------------------------------ emit helpers
    def _emit_system(self, force: bool = True) -> None:
        ready, reasons, warnings = self.readiness()
        key = (ready, tuple(reasons), tuple(warnings), self.screen().value, len(self.alerts), self.powering_off,
               self.switching)
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
        elif isinstance(e, MeasurementFinalized):
            self._on_measurement(e)
        elif isinstance(e, MeasurementStatus):
            self._on_measurement_status(e)
        elif isinstance(e, HardwareStatus):
            self._on_hardware(e)
        elif isinstance(e, DetectorStatus):
            self._on_detector(e)
        elif isinstance(e, LegacyResult):
            self._on_legacy_result(e)
        elif isinstance(e, RecordingSession):
            self._on_recording(e)
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
            self._late_stop_confirmation(e)                   # a stop delivered after the next start
            return                                            # otherwise duplicate or stale delivery
        if last is not None and e.transition_seq != last + 1:
            self._alert("transition_gap", f"Missed {e.transition_seq - last - 1} scan transition(s)", e.scan_id)
        self.last_transition[e.controller_instance_id] = e.transition_seq
        scan = self.repo.scan(e.scan_id)
        if e.state == "SCANNING":
            if scan is None:
                for other in self.repo.scans_where("acquisition IN ('scanning', 'paused')"):
                    # inferred, not confirmed: it can never complete on this evidence alone
                    self._end_acquisition(other, Acquisition.ENDED, UNCONFIRMED,
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
                self.repo.update_scan(e.scan_id, stop_stamp_ns=e.status_stamp_ns)
                self._end_acquisition(scan, acq, e.state, e.message or None)
            elif scan["stop_state"] == UNCONFIRMED:
                self._late_stop_confirmation(e)
        else:
            self._audit("unknown_transition_state", scan_id=e.scan_id, state=e.state)

    def _late_stop_confirmation(self, e: ScanTransition) -> None:
        """The controller's own stop transition for a scan whose stop was only inferred: record it as the
        confirmed acquisition outcome (ERROR stays an error) and re-check completion."""
        if e.state not in ("IDLE", "ERROR"):
            return
        scan = self.repo.scan(e.scan_id)
        if scan is None or scan["stop_state"] != UNCONFIRMED:
            return
        acq = Acquisition.ERROR if e.state == "ERROR" else Acquisition.ENDED
        note = "stop confirmed late by the controller" + (f": {e.message}" if e.message else "")
        self.repo.update_scan(e.scan_id, acquisition=acq.value, stop_state=e.state, acquisition_note=note,
                              stop_stamp_ns=e.status_stamp_ns)
        self._audit("stop_confirmed_late", scan_id=e.scan_id, state=e.state, transition_seq=e.transition_seq)
        self._try_complete(e.scan_id)

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
        early = self.pending_recordings.pop(e.status_stamp_ns, None)
        if early is not None:
            self._on_recording(early)
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
            self._check_late_terminal(scan, e)
            return None
        return scan

    def _check_late_terminal(self, scan: dict, e) -> None:
        """A terminal message after the analysis was closed: a redelivery must match what was recorded.
        A conflicting one cannot change a closed (possibly decided) scan, but must not pass silently."""
        if isinstance(e, StreamClosed):
            same = scan["published_frame_count"] in (None, e.frame_count) and not e.error
        elif isinstance(e, DetectionFinalized):
            same = (scan["emitted_result_count"] in (None, e.emitted_result_count)
                    and (e.success or scan["analysis"] != Analysis.COMPLETE.value))
        elif isinstance(e, MeasurementFinalized):
            same = e.success or scan["analysis"] != Analysis.COMPLETE.value
            if same and e.success and scan["analysis"] == Analysis.COMPLETE.value:
                stored = {f["feature_id"]: f["measurement"] for f in self.repo.features_of(scan["scan_id"])
                          if f["is_target"]}
                try:
                    fresh = self._measurements(scan, e)
                except (models.ValidationError, KeyError, ValueError):
                    fresh = None
                same = fresh == stored
        else:
            return
        if not same:
            self._alert("conflicting_terminal", f"{type(e).__name__} for an already closed scan conflicts with its "
                        f"record ({scan['analysis']}): check this scan's data", scan["scan_id"])

    def _conflict(self, scan_id: str, kind: str, first, second) -> None:
        """A second, different terminal message for the same scan: the evidence disagrees, so the analysis
        cannot be trusted (identical redeliveries are ignored before this)."""
        self._audit("conflicting_" + kind, scan_id=scan_id, first=repr(first), second=repr(second))
        self._fail(scan_id, f"conflicting {kind.replace('_', ' ')} messages for this scan")

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
            if not self.repo.same_feature(e.scan_id, e.detector_mode, e.result_seq, feature, e.detail):
                self._fail(e.scan_id, f"conflicting detector result #{e.result_seq} (feature {e.feature_id}): "
                                      f"same sequence or feature id with different content")
            return                                            # identical redelivery: idempotent
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
            if rt.closed != e:
                self._conflict(e.scan_id, "stream_close", rt.closed, e)
            return                                            # identical redelivery: ignored
        rt.closed = e
        self.repo.update_scan(e.scan_id, published_frame_count=e.frame_count)
        self._verify_raw(e.scan_id)
        if e.error:
            self._audit("stream_closed_error", scan_id=e.scan_id, error=e.error)
            self._fail(e.scan_id, f"frame stream closed with error: {e.error}")
            return
        self._try_complete(e.scan_id)

    def _on_finalized(self, e: DetectionFinalized) -> None:
        scan = self._assigned_pending(e.scan_id, e)
        if scan is None:
            return
        rt = self.runtime.setdefault(e.scan_id, ScanRuntime())
        if rt.finalized is not None:
            if rt.finalized != e:
                self._conflict(e.scan_id, "detector_finalization", rt.finalized, e)
            return                                            # identical redelivery: ignored
        rt.finalized = e
        self.repo.update_scan(e.scan_id, emitted_result_count=e.emitted_result_count)
        self._audit("detection_finalized", scan_id=e.scan_id, success=e.success, error=e.error,
                    frame_count=e.frame_count, emitted=e.emitted_result_count,
                    detector_targets=e.detected_target_count)
        if not e.success:
            self._fail(e.scan_id, f"detector reported failure: {e.error or 'no reason given'}")
            return
        self._try_complete(e.scan_id)

    def _on_measurement(self, e: MeasurementFinalized) -> None:
        scan = self._assigned_pending(e.scan_id, e)
        if scan is None:
            return
        rt = self.runtime.setdefault(e.scan_id, ScanRuntime())
        if rt.measured is not None:
            if rt.measured != e:
                self._conflict(e.scan_id, "measurement", rt.measured, e)
            return                                            # identical redelivery: ignored
        rt.measured = e
        self._audit("measurement_finalized", scan_id=e.scan_id, success=e.success, error=e.error,
                    features=len(e.features), measured=sum(1 for f in e.features if f.measured),
                    elapsed_s=round(e.elapsed_s, 3))
        if not e.success:
            self._fail(e.scan_id, f"measurement failed: {e.error or 'no reason given'}")
            return
        self._try_complete(e.scan_id)

    def _on_measurement_status(self, e: MeasurementStatus) -> None:
        if e.available != self.measurement_available:
            self.measurement_available = e.available
            self._emit_system()

    def _measurements(self, scan: dict, e: MeasurementFinalized) -> dict[int, dict]:
        """feature id -> stored measurement for exactly the scan's target features. Raises ValueError when the
        measurement does not cover them one to one, ValidationError for a value that is not a measurement."""
        m = Mode(scan["mode"])
        if e.detector_mode != scan["mode"] or e.quantity != models.MEASUREMENT[m][0]:
            raise ValueError(f"measurement of {e.detector_mode}/{e.quantity}, scan mode {scan['mode']}")
        ids = [f.feature_id for f in e.features]
        targets = self.repo.target_feature_ids(scan["scan_id"])
        if len(set(ids)) != len(ids) or sorted(ids) != targets:
            raise ValueError(f"measured features {sorted(ids)} are not the scan's targets {targets}")
        return {f.feature_id: models.measured_value(m, f.measured, f.value_m, f.reason, e.lower_limit_m,
                                                    e.upper_limit_m) for f in e.features}

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
        if scan["stop_state"] == UNCONFIRMED:
            return    # everything else is in: wait for the controller's own stop (late) or the watchdog
        if c.stop_state != scan["stop_state"]:
            return self._fail(scan_id, f"stop state mismatch: controller {scan['stop_state']}, "
                                       f"frame stream {c.stop_state}")
        if rt.measured is None:
            return                                            # detection complete: the measurement follows
        try:
            measurements = self._measurements(scan, rt.measured)
        except (models.ValidationError, ValueError) as err:
            return self._fail(scan_id, f"invalid measurement: {err}")
        self.repo.set_measurements(scan_id, measurements)
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
        self._maybe_export(scan_id)

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
        self._maybe_export(scan_id)

    def _maybe_export(self, scan_id: str) -> None:
        """Default recording for experiments and ML: once a scan's analysis has ended (its features and
        measurements final) and its raw recording is closed and verified, whichever comes last."""
        s = self.repo.scan(scan_id)
        if (s is None or scan_id in self.exported or s["analysis"] not in (Analysis.COMPLETE.value, Analysis.FAILED.value)
                or s["raw_state"] != "closed" or not s["replay_eligible"]):
            return
        self.exported.add(scan_id)
        try:
            self.recording_export.export(s)
        except Exception as err:      # noqa: BLE001 - a recording side job never breaks the lifecycle
            self._audit("recording_export_failed", scan_id=scan_id, reason=str(err))

    def _on_hardware(self, e: HardwareStatus) -> None:
        prev = self.hardware
        self.hardware, self.hardware_mono = e, self.clock.monotonic()
        if self.preview_mode:
            self._preview_transition(prev.scan_state if prev else None, e)
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

    # ------------------------------------------------------------------ Phase 2 preview
    @property
    def preview_mode(self) -> bool:
        """Live ROS without tagged identity: show, never persist or accept (plan Phase 2)."""
        return self.source == "ros-preview"

    def preview_view(self) -> dict | None:
        p = self.preview
        if p is None:
            return None
        return {k: (list(v) if isinstance(v, list) else v) for k, v in p.items()}

    def _preview_transition(self, prev_state: str | None, e: HardwareStatus) -> None:
        was, now = prev_state in HW_ACQUIRING, e.scan_state in HW_ACQUIRING
        if now and not was:
            insp = self.inspection
            reason = None
            if not self.operator:
                reason = "no operator identified"
            elif not insp:
                reason = "no open inspection"
            elif self.detector.mode != insp["mode"] or not self.detector.ready:
                reason = "detector not ready"
            self.preview_count += 1
            self.preview = {
                "number": self.preview_count, "inspection_uuid": insp["uuid"] if insp and not reason else None,
                "mode": insp["mode"] if insp and not reason else None, "scanning": True,
                "started_at": self._now(), "ended_at": None, "stop_state": None,
                "expected_count": insp["next_expected_count"] if insp and not reason else None,
                "detected_target_count": 0, "non_target_count": 0, "partial_target_count": 0,
                "ignored_results": 0, "distance_mm": 0.0, "unassigned_reason": reason, "features": []}
            if reason:
                self._alert("unassigned_scan", f"Scan started but not shown for an inspection: {reason}")
            else:
                self.clear_alert("unassigned_scan")
            self.emit("scan_progress", {"phase": "started", "preview": True, **self.preview_view()},
                      inspection_id=self.preview["inspection_uuid"])
        elif was and not now and self.preview is not None:
            self.preview.update(scanning=False, ended_at=self._now(), stop_state=e.scan_state)
            self.emit("scan_acquisition_ended", {"preview": True, **self.preview_view()},
                      inspection_id=self.preview["inspection_uuid"])
        if now and self.preview is not None:
            self.preview["distance_mm"] = round(e.position_m * 1000.0, 1)
        if was != now:
            self._emit_system()

    def _on_legacy_result(self, e: LegacyResult) -> None:
        p = self.preview
        if not self.preview_mode or p is None or p["mode"] is None:
            self._audit("legacy_result_ignored", reason="no previewed scan" if self.preview_mode else "not in preview mode",
                        feature_type=e.feature_type)
            return
        m = Mode(p["mode"])
        try:
            fid = int(e.detail.get("id"))
            feature = models.feature_from_result(m, e.feature_type, fid, e.position_m, e.detail)
        except (models.ValidationError, TypeError, ValueError):
            p["ignored_results"] += 1                    # e.g. another mode's detector publishing
            return
        if any(f["feature_id"] == fid and f["kind"] == feature["kind"] for f in p["features"]) \
                or len(p["features"]) >= PREVIEW_FEATURE_LIMIT:
            return
        feature["result_seq"] = len(p["features"])
        feature["after_release"] = not p["scanning"]
        p["features"].append(feature)
        if feature["is_target"]:
            p["detected_target_count"] += 1
            p["partial_target_count"] += int(feature["partial"])
        else:
            p["non_target_count"] += 1
        self.emit("feature_detected", {**feature, "preview": True, "preview_number": p["number"],
                                       "detected_target_count": p["detected_target_count"],
                                       "non_target_count": p["non_target_count"],
                                       "expected_count": p["expected_count"]},
                  inspection_id=p["inspection_uuid"])

    def restart_detector(self) -> dict:
        """Explicit recovery after a detector crash or startup failure (plan section 6)."""
        insp = self.inspection
        if not insp:
            raise CommandError(409, "no_inspection", "No open inspection")
        if self.detector.mode == insp["mode"] and self.detector.alive and self.detector.ready:
            return self.snapshot()
        op = self.operator
        self._audit("detector_restart", inspection_uuid=insp["uuid"], actor=op["operator_id"] if op else None)
        self.detector_control.request(Mode(insp["mode"]), insp["uuid"])
        return self.snapshot()

    # ------------------------------------------------------------------ raw recording link (Phase 3)
    def _on_recording(self, e: RecordingSession) -> None:
        """Link the recorder's session to its scan by the exact scan/status stamp, then verify it."""
        if e.event in ("OPENED", "OPEN_FAILED"):
            rows = self.repo.scans_where("start_stamp_ns = ? AND start_stamp_ns != 0", e.status_stamp_ns)
            if not rows:
                self.pending_recordings[e.status_stamp_ns] = e          # its scan may not be known yet
                while len(self.pending_recordings) > 50:
                    self.pending_recordings.pop(next(iter(self.pending_recordings)))
                return
            scan = rows[0]
            failed = e.event == "OPEN_FAILED" or e.write_failed
            self.repo.update_scan(scan["scan_id"], raw_session_id=e.session_id or None, raw_dir=e.session_dir or None,
                                  raw_state="open_failed" if failed else "open",
                                  raw_note="the recorder could not open a session" if failed else None)
            self._audit("raw_recording_linked", scan_id=scan["scan_id"], session_id=e.session_id, event=e.event)
            return
        if e.event == "CLOSED":
            rows = self.repo.scans_where("raw_session_id = ?", e.session_id) if e.session_id else []
            if not rows:
                self._audit("raw_recording_unmatched", session_id=e.session_id, event=e.event)
                return
            scan = rows[0]
            self.repo.update_scan(scan["scan_id"], raw_frame_count=e.frame_count,
                                  raw_state="write_failed" if e.write_failed else "closed")
            self._verify_raw(scan["scan_id"])

    def _verify_raw(self, scan_id: str) -> None:
        s = self.repo.scan(scan_id)
        if s is None or s["raw_state"] not in ("closed", "write_failed"):
            return                                   # not closed yet: verified when it is
        if s["raw_state"] == "write_failed":
            ok, note = False, "the recorder reported a write failure"
        else:
            ok, note = raw_recording.verify(s["raw_dir"], s["raw_frame_count"], s["published_frame_count"])
        self.repo.update_scan(scan_id, replay_eligible=int(ok), raw_note=note)
        self._audit("raw_recording_verified", scan_id=scan_id, replay_eligible=ok, note=note)
        self._maybe_export(scan_id)

    def ingress_overflow(self, dropped: int) -> None:
        """Events were dropped between ROS and the engine: pending results may be incomplete."""
        self._alert("event_overflow", f"{dropped} ROS event(s) dropped: analysis results may be incomplete")
        for s in self.repo.scans_where("analysis = 'pending'"):
            self._fail(s["scan_id"], "ROS event queue overflow: results may be lost")

    def tick(self) -> None:
        """Watchdog (plan: warn 10 s, fail 120 s after release; never a fixed-delay success)."""
        try:
            self._bag_sync()
        except Exception as err:      # noqa: BLE001 - never stop the watchdog for a side job
            self._audit("bag_error", reason=str(err))
        now = self.clock.monotonic()
        for scan_id, rt in list(self.runtime.items()):
            if rt.released_mono is None:
                continue
            waited = now - rt.released_mono
            if waited >= self.fail_after_s:
                missing = [n for n, ok in (("stream close", rt.closed), ("detector finalization", rt.finalized),
                                           ("measurement", rt.measured)) if ok is None]
                scan = self.repo.scan(scan_id)
                if scan is not None and scan["stop_state"] == UNCONFIRMED:
                    missing.append("the controller's stop transition: acquisition stop not confirmed (the stop "
                                   "was inferred from the next scan's start)")
                self._fail(scan_id, f"analysis timeout after {waited:.0f} s "
                                    f"(missing {', '.join(missing or ['detector results'])})")
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

    # ------------------------------------------------------------------ bag recording (bag_recorder.py)
    def set_bag(self, enabled) -> dict:
        """The "Record bags" switch: one `ros2 bag record -a` per scan while on. Not while a scan runs or is
        analysed (the running bag belongs to that scan)."""
        if not isinstance(enabled, bool):
            raise models.ValidationError("enabled", "enabled must be true or false")
        if enabled and not self.bag.available:
            raise CommandError(409, "unavailable", "Bag recording is not available here")
        if self._pending_work():
            raise CommandError(409, "scanning", "Not during a scan or its analysis")
        if enabled:
            free = self.bag.free_bytes()
            if free is not None and free < getattr(self.bag, "min_free", 0):
                raise CommandError(409, "low_storage", f"Less than {self.bag.min_free / 1e9:.0f} GB free")
        if enabled != self.bag_enabled:
            self.bag_enabled = enabled
            self._audit("bag_recording", actor=self._actor(), enabled=enabled)
        self._bag_sync()
        self._emit_system()
        return self.bag_view()

    def _bag_name(self, insp: dict, number: int) -> str:
        return f"{self.operator['operator_id']}-{insp['label']}-Scan-{number:03d}"

    def _bag_sync(self) -> None:
        """Called every watchdog tick. The next scan's bag opens on Ready / Not ready (ros2 bag needs 1-3 s to
        subscribe, so it must run before the press) and closes once that scan's analysis has ended."""
        st = self.bag.status()
        if st["error"] and self.bag_enabled:          # stopped itself: low storage or ros2 bag exited
            self.bag_enabled = False
            self._alert("bag_stopped", f"Bag recording off: {st['error']}")
        insp, running = self.inspection, st["state"] in ("starting", "recording")
        if running and not self.bag_scan:             # not ours to keep (never expected): close it
            self.bag.stop()
        elif running and self.bag_scan:
            uuid, number = self.bag_scan
            done = not self.bag_enabled or not self.operator or not insp or insp["uuid"] != uuid
            if not done:
                last = self.repo.latest_scan_of(uuid)
                done = bool(last and last["scan_number"] >= number and last["acquisition"] not in ACQUIRING
                            and last["analysis"] != Analysis.PENDING.value)
            if done:
                self.bag.stop()
                self._audit("bag_closed", inspection_uuid=uuid, name=st["name"], bytes=st["bytes"])
                self.bag_scan = None
        elif (not running and st["state"] == "idle" and self.bag_enabled and self.operator and insp
              and self.screen() in (Screen.READY, Screen.NOT_READY)):
            number = insp["last_scan_number"] + 1
            try:
                self.bag.start(self._bag_name(insp, number))
                self.bag_scan = (insp["uuid"], number)
                self._audit("bag_opened", inspection_uuid=insp["uuid"], name=self.bag.status()["name"])
            except Exception as err:      # noqa: BLE001 - recording is a side job: say why, switch off
                self.bag_enabled = False
                self._alert("bag_stopped", f"Bag recording off: {err}")
        st = self.bag.status()
        key = (self.bag_enabled, st["state"], st["name"], st["error"], st["bytes"] // 50_000_000)
        if key != self._bag_key:
            self._bag_key = key
            self._emit_system()

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
        self.detector_control.request(m, insp["uuid"])
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
        if self.preview_mode and self.hardware is not None and self.hardware.scan_state in HW_ACQUIRING:
            busy = True
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
        if scan["scan_id"] in self.exported:      # the recording's scan.json carries the decision too
            try:
                self.recording_export.export(scan, meta_only=True)
            except Exception as err:      # noqa: BLE001
                self._audit("recording_export_failed", scan_id=scan["scan_id"], reason=str(err))
        return self.scan_view(scan)

    # ------------------------------------------------------------------ device actions (device.py)
    def _acquiring(self) -> bool:
        hw_scanning = self.hardware_fresh() and self.hardware.scan_state in HW_ACQUIRING
        return hw_scanning or bool(self.repo.scans_where("acquisition IN ('scanning', 'paused')"))

    def _actor(self) -> str | None:
        op = self.operator
        return op["operator_id"] if op else None

    def check_calibrate(self) -> None:
        """Gyro calibration only with the device idle: calibration_node itself does not refuse during a scan."""
        if not self.device.can_calibrate:
            raise CommandError(409, "unavailable", "Calibration is not available here")
        if self.powering_off:
            raise CommandError(409, "powering_off", "The device is turning off")
        if not self.hardware_fresh():
            raise CommandError(409, "no_hardware", "No current hardware status: calibration needs the device")
        if self.hardware.scan_state != "IDLE" or self._acquiring():
            raise CommandError(409, "scanning", "Not during a scan: release the switch first")
        if self.hardware.calibrating:
            raise CommandError(409, "calibrating", "Calibration is already running")

    def record_calibration(self, started: bool, message: str) -> dict:
        self._audit("calibration_requested", actor=self._actor(), started=started, message=message)
        if not started:
            raise CommandError(409, "calibration_refused", message or "Calibration did not start")
        return {"started": True, "message": message}

    def begin_power_off(self, action: str = "off") -> bool:
        """Turn off or restart the device. True: the caller runs the command now. False: already turning off or
        restarting (a repeated tap)."""
        if action == "off" and not self.device.can_power_off:
            raise CommandError(409, "unavailable", "Turning off is not enabled on this device")
        if action == "reboot" and not getattr(self.device, "can_reboot", False):
            raise CommandError(409, "unavailable", "Restarting is not enabled on this device")
        if self.powering_off:
            return False
        if self._acquiring():
            raise CommandError(409, "scanning", "A scan is running: release the switch and wait for the analysis")
        if self.repo.scans_where("analysis = 'pending'"):
            raise CommandError(409, "analyzing", "Wait until the analysis has finished")
        self.powering_off = True
        self.power_action = action
        self._audit("power_off_requested" if action == "off" else "reboot_requested", actor=self._actor())
        self._emit_system()
        return True

    def power_off_failed(self, message: str) -> None:
        self.powering_off = False
        self._alert("power_off_failed", message)

    def begin_switch(self, kind: str) -> bool:
        """Service menu: desktop mode or restart of the scanner software (the PIN is checked by the caller).
        True: send the request now; False: already switching (a repeated tap)."""
        if self.powering_off:
            raise CommandError(409, "powering_off", "The device is turning off")
        if self.switching:
            return False
        if self._acquiring():
            raise CommandError(409, "scanning", "A scan is running: release the switch and wait for the analysis")
        if self.repo.scans_where("analysis = 'pending'"):
            raise CommandError(409, "analyzing", "Wait until the analysis has finished")
        self.switching = kind
        self._audit("service_" + kind, actor=self._actor())
        self._emit_system()
        return True

    def switch_failed(self, message: str) -> None:
        self.switching = None
        self._alert("service_failed", message)

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
                raise CommandError(409, "not_replayable",
                                   f"No verified raw recording ({scan['raw_note'] or 'none linked'}): "
                                   "Rescan or Keep Incomplete")
            if not getattr(self.detector_control, "can_replay", False):
                raise CommandError(409, "retry_unavailable", "The raw recording is verified, but replaying it into "
                                   "the detector is not implemented yet: Rescan or Keep Incomplete")
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

    def record_export(self, inspection_uuid: str, ok: bool, destination: str | None, error: str | None,
                      rows: int) -> None:
        """Audit a server-side USB export attempt (plan section 8), successful or not."""
        op = self.operator
        self._audit("export_usb" if ok else "export_usb_failed", inspection_uuid=inspection_uuid,
                    actor=op["operator_id"] if op else None, destination=destination, error=error, rows=rows)

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
