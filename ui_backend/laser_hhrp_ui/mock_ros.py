"""Mock ROS source for Phase 1 (plan section 10): drives the engine without hardware or rclpy.

Simulates the physical button + controller (ScanTransition), synchronizer (StreamClosed), the selected
detector (TaggedResult, DetectionFinalized, DetectorStatus) and hardware readiness (HardwareStatus):
- detections appear while scanning AND after release (late), as the real asynchronous detectors do;
- only features inside the scanned length are reported; the one at the end edge is partial;
- one-shot fault injection: detector failure, missing result, reordered delivery, error stop,
  stalled finalization, detector crash.

It is a development tool: its press/release endpoints exist only when the server runs with --mock,
never next to the live ROS bridge (plan: no web API starts the physical scan).
"""

from __future__ import annotations

import asyncio
import random
import uuid

from .events import (DetectionFinalized, DetectorStatus, HardwareStatus, ScanTransition, StreamClosed,
                     TaggedResult)
from .models import Mode

FAULTS = ("detector_failure", "missing_result", "reorder", "error_stop", "stall", "detector_crash",
          "frame_mismatch")
SCENARIOS = ("normal", "short", "fast_next", "zero", "reordered", "missing_result", "error_stop",
             "detector_failure")
FRAME_HZ = 120.0


class MockRos:
    def __init__(self, submit, speed: float = 1.0, seed: int = 1, targets: int = 20, non_targets: int = 2,
                 rows: int = 2, pitch_mm: float = 25.0, scan_speed_mm_s: float = 40.0,
                 detector_start_s: float = 0.6, late_delay_s: float = 1.2, edge_guard_mm: float = 6.0,
                 decide_lag_mm: float = 12.0):
        self.submit = submit
        self.speed = speed
        self.rng = random.Random(seed)
        self.targets, self.non_targets, self.rows, self.pitch_mm = targets, non_targets, rows, pitch_mm
        self.scan_speed_mm_s, self.detector_start_s = scan_speed_mm_s, detector_start_s
        self.late_delay_s, self.edge_guard_mm = late_delay_s, edge_guard_mm
        self.decide_lag_mm = decide_lag_mm      # the detector decides a feature this far behind the scan front
        self.controller_id = str(uuid.uuid4())
        self.seq = 0
        self.mode: Mode | None = None
        self.detector_alive = False
        self.hw = dict(scan_state="IDLE", calibrated=True, laser_ok=True, nano_ok=True, encoder_ok=True,
                       free_mb=12000.0, storage_write_failed=False, fatal=(), warnings=(), position_m=0.0,
                       message="")
        self.faults: set[str] = set()
        self.active: dict | None = None
        self.tasks: set[asyncio.Task] = set()

    # ---- timing ----
    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds / self.speed)

    def _spawn(self, coro) -> asyncio.Task:
        t = asyncio.get_running_loop().create_task(coro)
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)
        return t

    def _take(self, fault: str) -> bool:
        if fault in self.faults:
            self.faults.discard(fault)
            return True
        return False

    def inject(self, fault: str) -> None:
        if fault not in FAULTS:
            raise ValueError(f"fault must be one of {FAULTS}")
        if fault == "detector_crash":
            self.detector_alive = False
            self.submit(DetectorStatus(mode=self.mode.value if self.mode else None, alive=False, ready=False,
                                       error="mock detector crashed"))
            return
        self.faults.add(fault)

    # ---- hardware status (2 Hz like scan/storage; the bridge aggregates faster topics) ----
    def hardware_status(self) -> HardwareStatus:
        return HardwareStatus(**self.hw)

    async def run(self) -> None:
        while True:
            self.submit(self.hardware_status())
            await self.sleep(0.5)

    def set_hardware(self, **fields) -> None:
        unknown = set(fields) - set(self.hw)
        if unknown:
            raise ValueError(f"unknown hardware fields {sorted(unknown)}")
        self.hw.update(fields)
        self.submit(self.hardware_status())

    # ---- detector supervisor (DetectorControl interface) ----
    def request(self, mode: Mode) -> None:
        self.mode, self.detector_alive = mode, False
        self.submit(DetectorStatus(mode=mode.value, alive=True, ready=False))

        async def ready():
            await self.sleep(self.detector_start_s)
            if self.mode == mode:
                self.detector_alive = True
                self.submit(DetectorStatus(mode=mode.value, alive=True, ready=True))
        self._spawn(ready())

    def stop(self) -> None:
        self.mode, self.detector_alive = None, False
        self.submit(DetectorStatus(mode=None, alive=False, ready=False))

    def replay(self, scan: dict) -> None:
        pass  # Phase 3: replay of verified raw recordings

    # ---- the physical button ----
    def _transition(self, scan_id: str, state: str, message: str = "") -> None:
        self.seq += 1
        self.submit(ScanTransition(scan_id=scan_id, controller_instance_id=self.controller_id,
                                   transition_seq=self.seq, state=state,
                                   status_stamp_ns=int(asyncio.get_running_loop().time() * 1e9),
                                   message=message))

    def layout(self, mode: Mode) -> list[dict]:
        """Feature positions of the simulated part: rows across the laser line, pitch along the scan."""
        feats = []
        kinds = (["countersink"] * self.targets + ["hole"] * self.non_targets) if mode == Mode.CSK_DEPTH \
            else ["rivet"] * self.targets
        for i, kind in enumerate(kinds):
            row, col = i % self.rows, i // self.rows
            x = (row - (self.rows - 1) / 2) * 30.0 + self.rng.uniform(-0.3, 0.3)
            y = 15.0 + col * self.pitch_mm + self.rng.uniform(-0.3, 0.3)
            feats.append({"kind": kind, "x_mm": x, "y_mm": y,
                          "d_mm": (9.5 if kind == "countersink" else 4.8 if kind == "hole" else 5.6)
                          + self.rng.uniform(-0.05, 0.05)})
        feats.sort(key=lambda f: f["y_mm"])
        return feats

    def press(self) -> str | None:
        """Confirmed start (the mock controller never refuses a calibrated, ready start)."""
        if self.active is not None:
            return None
        if not self.hw["calibrated"]:
            return None
        scan_id = str(uuid.uuid4())
        mode = self.mode or Mode.CSK_DEPTH
        self.active = {"scan_id": scan_id, "mode": mode, "released": asyncio.Event(), "error": False,
                       "layout": self.layout(mode), "detector": self.detector_alive and self.mode is not None}
        self.hw.update(scan_state="SCANNING", position_m=0.0)
        self._transition(scan_id, "SCANNING")
        self._spawn(self._scan(self.active))
        return scan_id

    def release(self) -> str | None:
        scan = self.active
        if scan is None:
            return None
        self.active = None
        error = self._take("error_stop")
        scan["error"] = error
        scan["t_release"] = asyncio.get_running_loop().time()
        self.hw.update(scan_state="IDLE")
        self._transition(scan["scan_id"], "ERROR" if error else "IDLE",
                         "laser profiler disconnected" if error else "")
        scan["released"].set()
        return scan["scan_id"]

    def _result(self, scan: dict, seq: int, fid: int, f: dict, partial: bool) -> TaggedResult:
        kind = f["kind"]
        cov = round(self.rng.uniform(0.8, 1.0), 2)
        share = round(self.rng.uniform(0.55, 0.95) if partial else 1.0, 2)
        detail = ({"id": fid, "d_top_mm": round(f["d_mm"], 3), "rms_mm": 0.012, "ring_coverage": cov,
                   "countersunk": kind == "countersink", "partial": partial, "scanned_share": share}
                  if kind != "rivet" else
                  {"id": fid, "d_head_mm": round(f["d_mm"], 3), "height_mm": round(self.rng.uniform(-0.05, 0.05), 3),
                   "rms_mm": 0.01, "edge_coverage": cov, "partial": partial, "scanned_share": share})
        return TaggedResult(scan_id=scan["scan_id"], result_seq=seq, detector_mode=scan["mode"].value,
                            feature_id=fid, feature_type=kind,
                            position_m=(f["x_mm"] / 1000, f["y_mm"] / 1000, 0.0002), detail=detail)

    async def _scan(self, scan: dict) -> None:
        """One scan: detections during acquisition, then the residual pass after release."""
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        emitted: list[TaggedResult] = []
        pending = list(scan["layout"])
        drop = self._take("missing_result")
        dropped_once = [False]

        def send(r: TaggedResult):
            emitted.append(r)
            if drop and not dropped_once[0] and len(emitted) == 2:
                dropped_once[0] = True          # declared by the detector but never delivered
                return
            self.submit(r)

        def y_at(t: float) -> float:
            return (t - t0) * self.speed * self.scan_speed_mm_s

        while not scan["released"].is_set():
            y = y_at(loop.time())
            self.hw["position_m"] = y / 1000.0
            if scan["detector"]:
                while pending and pending[0]["y_mm"] + self.edge_guard_mm + self.decide_lag_mm <= y:
                    f = pending.pop(0)
                    if self.rng.random() < 0.15:     # some features are decided a cycle later
                        pending.insert(0, f)
                        break
                    send(self._result(scan, len(emitted), len(emitted) + 1, f, False))
            await self.sleep(0.05)
        y_end = y_at(scan["t_release"])
        frames = int(round((scan["t_release"] - t0) * self.speed * FRAME_HZ))
        if not scan["detector"]:
            return                               # nothing is listening: no stream close or results
        reorder, fail, stall = self._take("reorder"), self._take("detector_failure"), self._take("stall")
        mismatch = self._take("frame_mismatch")
        if not reorder:
            await self.sleep(0.05)
            self.submit(StreamClosed(scan["scan_id"], frames, "ERROR" if scan["error"] else "IDLE"))
        # residual pass: everything the scan actually covered, end edge partial, nothing beyond
        await self.sleep(self.late_delay_s)
        late = []
        for f in pending:
            if f["y_mm"] + self.edge_guard_mm <= y_end:
                late.append((f, False))
            elif f["y_mm"] <= y_end:
                late.append((f, True))
        held = []
        for i, (f, partial) in enumerate(late):
            r = self._result(scan, len(emitted), len(emitted) + 1, f, partial)
            if reorder and i == len(late) - 1:
                emitted.append(r)
                held.append(r)                   # delivered after the finalization
                continue
            send(r)
            await self.sleep(0.15)
        if stall:
            return                               # never finalizes: the watchdog must fail it
        targets = sum(1 for r in emitted if r.feature_type in ("countersink", "rivet"))
        self.submit(DetectionFinalized(scan["scan_id"], scan["mode"].value, frames + (3 if mismatch else 0),
                                       len(emitted), targets, success=not fail,
                                       error="mock detector failure" if fail else ""))
        if reorder:
            await self.sleep(0.2)
            for r in held:
                self.submit(r)
            self.submit(StreamClosed(scan["scan_id"], frames, "ERROR" if scan["error"] else "IDLE"))

    # ---- scripted scenarios (demo and tests) ----
    def scan_seconds_for_all(self) -> float:
        """Hold time that covers every feature of the layout with margin."""
        n = self.targets + (self.non_targets if (self.mode or Mode.CSK_DEPTH) == Mode.CSK_DEPTH else 0)
        last_y = 15.0 + (-(-n // self.rows) - 1) * self.pitch_mm
        return (last_y + self.edge_guard_mm + 10.0) / self.scan_speed_mm_s

    async def hold(self, seconds: float) -> str | None:
        scan_id = self.press()
        await self.sleep(seconds)
        self.release()
        return scan_id

    async def scenario(self, name: str) -> list[str]:
        """normal | short | fast_next | reordered | missing_result | zero | error_stop | detector_failure"""
        full = self.scan_seconds_for_all()
        if name == "normal":
            return [await self.hold(full)]
        if name == "short":                              # lifted 2 mm past a mid-part feature: it is partial
            n = self.targets + (self.non_targets if (self.mode or Mode.CSK_DEPTH) == Mode.CSK_DEPTH else 0)
            mid_y = 15.0 + (-(-n // self.rows) // 2) * self.pitch_mm
            return [await self.hold((mid_y + 2.0) / self.scan_speed_mm_s)]
        if name == "fast_next":
            a = await self.hold(full)
            await self.sleep(0.1)
            b = await self.hold(full * 0.5)
            return [a, b]
        if name == "zero":
            return [await self.hold(0.2)]
        if name in ("reordered", "missing_result", "error_stop", "detector_failure"):
            self.faults.add({"reordered": "reorder"}.get(name, name))
            return [await self.hold(full)]
        raise ValueError(f"unknown scenario {name}")

    def state(self) -> dict:
        return {"mode": self.mode.value if self.mode else None, "detector_alive": self.detector_alive,
                "scanning": self.active["scan_id"] if self.active else None, "faults": sorted(self.faults),
                "hardware": {k: (list(v) if isinstance(v, tuple) else v) for k, v in self.hw.items()},
                "layout": {"targets": self.targets, "non_targets": self.non_targets, "rows": self.rows},
                "speed": self.speed}
