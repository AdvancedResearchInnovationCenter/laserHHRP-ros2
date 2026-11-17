"""Deterministic engine harness: events fed directly, fake clock, recorded UI events."""

from __future__ import annotations

import uuid

from laser_hhrp_ui.clock import FakeClock
from laser_hhrp_ui.events import (DetectionFinalized, DetectorStatus, HardwareStatus, ScanTransition,
                                  StreamClosed, TaggedResult)
from laser_hhrp_ui.repository import Repository
from laser_hhrp_ui.state import Engine

TARGET = {"csk_depth": "countersink", "rivet_flushness": "rivet"}


class FakeDetectorControl:
    def __init__(self):
        self.calls = []

    def request(self, mode, inspection_uuid=None):
        self.calls.append(("request", mode.value))

    def stop(self):
        self.calls.append(("stop", None))

    def replay(self, scan):
        self.calls.append(("replay", scan["scan_id"]))


class Harness:
    def __init__(self, path=":memory:", clock=None, repo=None, quality="synced", source="mock"):
        self.clock = clock or FakeClock(quality)
        self.repo = repo or Repository(path)
        self.events = []
        self.det = FakeDetectorControl()
        self.engine = Engine(self.repo, self.clock, emit=self._emit, detector_control=self.det, source=source)
        self.ctrl, self.seq = "ctrl-1", 0
        self.next_seq = {}

    def _emit(self, type_, payload, scan_id=None, inspection_id=None):
        self.events.append((type_, payload, scan_id))

    def types(self, scan_id=None):
        return [t for t, _, s in self.events if scan_id is None or s == scan_id]

    # ---- inputs ----
    def handle(self, e):
        self.engine.handle(e)

    def hw(self, **kw):
        base = dict(scan_state="IDLE", calibrated=True, laser_ok=True, nano_ok=True, encoder_ok=True,
                    free_mb=8000.0)
        base.update(kw)
        self.handle(HardwareStatus(**base))

    def detector(self, mode, alive=True, ready=True, error=""):
        self.handle(DetectorStatus(mode=mode, alive=alive, ready=ready, error=error))

    def ready_inspection(self, mode="csk_depth", count=20, operator="ENG-1042", label=None):
        self.engine.set_operator(operator)
        insp = self.engine.create_inspection(mode, label)
        self.detector(mode)
        self.hw()
        if count is not None:
            self.engine.set_next_expected_count(insp["uuid"], count)
        return insp

    def transition(self, scan_id, state, seq=None):
        self.seq = self.seq + 1 if seq is None else seq
        self.handle(ScanTransition(scan_id, self.ctrl, self.seq, state, self.seq * 1000))

    def press(self, scan_id=None):
        scan_id = scan_id or str(uuid.uuid4())
        self.transition(scan_id, "SCANNING")
        self.next_seq[scan_id] = 0
        return scan_id

    def release(self, scan_id, state="IDLE"):
        self.transition(scan_id, state)

    def result(self, scan_id, kind=None, mode=None, seq=None, fid=None, partial=False, y_mm=None):
        mode = mode or (self.repo.scan(scan_id) or {}).get("mode") or "csk_depth"
        kind = kind or TARGET[mode]
        if seq is None:
            seq = self.next_seq.get(scan_id, 0)
            self.next_seq[scan_id] = seq + 1
        fid = seq + 1 if fid is None else fid
        y = (15 + 25 * seq) if y_mm is None else y_mm
        detail = {"id": fid, "partial": partial, "scanned_share": 0.7 if partial else 1.0, "rms_mm": 0.01}
        detail.update({"d_top_mm": 9.5, "ring_coverage": 0.9} if kind != "rivet"
                      else {"d_head_mm": 5.6, "height_mm": 0.02, "edge_coverage": 0.9})
        self.handle(TaggedResult(scan_id, seq, mode, fid, kind, (0.012, y / 1000, 0.0), detail))
        return seq

    def results(self, scan_id, n, kind=None):
        for _ in range(n):
            self.result(scan_id, kind)

    def close(self, scan_id, frames=500, stop_state="IDLE"):
        self.handle(StreamClosed(scan_id, frames, stop_state))

    def finalize(self, scan_id, frames=500, emitted=None, targets=None, success=True, mode=None, error=""):
        scan = self.repo.scan(scan_id)
        mode = mode or scan["mode"]
        if emitted is None:
            emitted = self.next_seq.get(scan_id, 0)
        if targets is None:
            targets = self.repo.feature_counts(scan_id)["targets"]
        self.handle(DetectionFinalized(scan_id, mode, frames, emitted, targets, success, error))

    def complete_scan(self, n=20, kind=None, frames=500):
        """Press, n detections (half after release), release, close, finalize."""
        sid = self.press()
        self.results(sid, n // 2, kind)
        self.release(sid)
        self.results(sid, n - n // 2, kind)
        self.close(sid, frames)
        self.finalize(sid, frames)
        return sid

    def scan(self, scan_id):
        return self.engine.scan_detail(scan_id)
