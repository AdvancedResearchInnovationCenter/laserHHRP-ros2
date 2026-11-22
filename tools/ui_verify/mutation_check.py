#!/usr/bin/env python3
"""Do the backend tests catch real bugs? Plant one bug at a time in a throwaway copy of ui_backend and
run the test suite: a bug is KILLED if at least one test fails, SURVIVED if every test still passes.

    python3 tools/ui_verify/mutation_check.py          # ~1 min; never modifies the source tree

Each mutation is a plausible mistake against a plan.md rule (section in brackets).
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "ui_backend"

MUTATIONS = [
    ("Recording exported before its raw file is verified [recording]", "state.py",
     'or s["raw_state"] != "closed" or not s["replay_eligible"]):', 'or False):'),
    ("Recording exported again on every event [recording]", "state.py",
     "if (s is None or scan_id in self.exported or", "if (s is None or"),
    ("Complete without waiting for all declared results [5.6]", "state.py",
     'if counts["total"] < f.emitted_result_count:\n            return',
     'if False:\n            return'),
    ("Ignore synchronizer/detector frame-count mismatch [5.3]", "state.py",
     "if f.frame_count != c.frame_count:", "if False:"),
    ("Treat a hardware ERROR stop as a successful analysis [4.6]", "state.py",
     'if scan["acquisition"] == Acquisition.ERROR.value:', "if False:"),
    ("Allow Accept on a failed analysis [4.5]", "state.py",
     'if scan["analysis"] != Analysis.COMPLETE.value:\n            raise CommandError(409, "analysis_not_complete"',
     'if False:\n            raise CommandError(409, "analysis_not_complete"'),
    ("Restart marks unfinished analysis as complete [8]", "state.py",
     "self.repo.update_scan(s[\"scan_id\"], analysis=Analysis.INTERRUPTED.value,",
     "self.repo.update_scan(s[\"scan_id\"], analysis=Analysis.COMPLETE.value,"),
    ("Drop detections that arrive after switch release [1, 4.2]", "state.py",
     'if e.detector_mode != scan["mode"]:',
     'if scan["acquisition"] not in ACQUIRING or e.detector_mode != scan["mode"]:'),
    ("Watchdog never fails a stuck analysis [5.7]", "state.py",
     "if waited >= self.fail_after_s:", "if False:"),
    ("Complete as soon as detected count reaches expected [1, 4.3]", "state.py",
     "        rt = self.runtime.get(scan_id)\n        if rt is None or rt.finalized is None or rt.closed is None:",
     "        n = self.repo.feature_counts(scan_id)['targets']\n"
     "        if scan['expected_count'] and n >= scan['expected_count']:\n"
     "            self.repo.update_scan(scan_id, analysis=Analysis.COMPLETE.value, detected_target_count=n,\n"
     "                                  count_mismatch=int(n != scan['expected_count']))\n"
     "            self.runtime.pop(scan_id, None)\n"
     "            return\n"
     "        rt = self.runtime.get(scan_id)\n        if rt is None or rt.finalized is None or rt.closed is None:"),
    ("New scan start does not end a scan whose stop was missed [5, 11.11]", "state.py",
     "for other in self.repo.scans_where(\"acquisition IN ('scanning', 'paused')\"):",
     "for other in []:"),
    ("Results for an unknown scan attributed to nothing / lost [5.3 reorder]", "state.py",
     "            self._orphan(scan_id, e)\n            return None",
     "            return None"),
    ("Count plain holes as CSK targets [5 filter]", "models.py",
     'TARGET_TYPES = {Mode.CSK_DEPTH: {"countersink"}',
     'TARGET_TYPES = {Mode.CSK_DEPTH: {"countersink", "hole"}'),
    ("Report detection-time features as measured [1, 8]", "models.py",
     '"unit": "mm", "status": "pending", "reason": None,', '"unit": "mm", "status": "measured", "reason": None,'),
    ("UI-side partial threshold instead of detector flag [9]", "models.py",
     '"partial": detail.get("partial") is True,',
     '"partial": (detail.get("scanned_share") or 1.0) < 0.9,'),
    ("Expected count editable while scanning [4.1, 7]", "state.py",
     "if self.screen() not in (Screen.READY, Screen.NOT_READY):", "if False:"),
    ("Second inspection may open while one is open [1]", "state.py",
     'if self.inspection:\n            raise CommandError(409, "inspection_active", "An inspection is already open")',
     'if False:\n            raise CommandError(409, "inspection_active", "An inspection is already open")'),
    ("Duplicate decision with a different choice overwrites [7 idempotent]", "state.py",
     'raise CommandError(409, "already_decided", f"Scan already {scan[\'decision\']}")\n'
     '        if scan["analysis"] != Analysis.COMPLETE.value:',
     'pass\n        if scan["analysis"] != Analysis.COMPLETE.value:'),
    ("Queue overflow does not fail pending analyses [5.6]", "state.py",
     'self._fail(s["scan_id"], "ROS event queue overflow: results may be lost")', "pass"),
    # code review of 3d7f52e (findings 1, 2, 5)
    ("Stream-close error ignored [review 1]", "state.py",
     '            self._fail(e.scan_id, f"frame stream closed with error: {e.error}")\n            return\n',
     "            pass\n"),
    ("Stream stop state not compared with the controller's [review 1]", "state.py",
     'if c.stop_state != scan["stop_state"]:', "if False:"),
    ("Inferred stop accepted as a confirmed stop [review 2]", "state.py",
     'if scan["stop_state"] == UNCONFIRMED:\n            return    #',
     'if False:\n            return    #'),
    ("Late controller stop confirmation ignored [review 2]", "state.py",
     "            self._late_stop_confirmation(e)                   # a stop delivered after the next start\n", ""),
    ("Duplicate judged by sequence and feature id only [review 5]", "state.py",
     "if not self.repo.same_feature(e.scan_id, e.detector_mode, e.result_seq, feature, e.detail):",
     "if not [f for f in self.repo.features_of(e.scan_id)\n"
     "                    if f['result_seq'] == e.result_seq and f['feature_id'] == e.feature_id]:"),
    # Phase 3: raw recording link and retry guard
    ("Raw recording replay-eligible without checking frames.bin [5 raw identity]", "raw_recording.py",
     "    n, error = count_records(Path(raw_dir) / \"frames.bin\")\n",
     "    return True, \"frames.bin verified\"\n    n, error = count_records(Path(raw_dir) / \"frames.bin\")\n"),
    ("Retry allowed although replay is not implemented [4.5]", "state.py",
     'if not getattr(self.detector_control, "can_replay", False):', "if False:"),
    # review of 78004e1 (2026-11-18)
    ("Second, different stream close ignored [review 78004e1 #1]", "state.py",
     "            if rt.closed != e:\n                self._conflict(", "            if False:\n                self._conflict("),
    ("Conflicting detector finalization only audited [review 78004e1 #1]", "state.py",
     "            if rt.finalized != e:\n                self._conflict(", "            if False:\n                self._conflict("),
    ("Inferred stop fails before a late controller stop can confirm it [review 78004e1 #2]", "state.py",
     'if scan["stop_state"] == UNCONFIRMED:\n            return    #',
     'if scan["stop_state"] == UNCONFIRMED:\n            return self._fail(scan_id, "stop not confirmed")  #'),
    # Phase 4: export and the Retry flag the screens use
    ("Retry shown whenever raw data is verified [4.5]", "state.py",
     '"retry_available": bool(s["replay_eligible"]) and bool(getattr(self.detector_control, "can_replay",',
     '"retry_available": bool(s["replay_eligible"]) or bool(getattr(self.detector_control, "can_replay",'),
    ("CSV leaves out scans without features [8 history]", "export.py",
     "        if not features:\n", "        if False:\n"),
    ("USB export writes outside the approved mount roots [7 USB]", "export.py",
     "if not any(path != root and root in path.parents for root in self.roots):", "if False:"),
    ("USB export overwrites an existing file [7 USB]", "export.py",
     "os.O_WRONLY | os.O_CREAT | os.O_EXCL", "os.O_WRONLY | os.O_CREAT | os.O_TRUNC"),
    ("Failed USB export not audited [8 export]", "api.py",
     "            engine.record_export(uuid, False, str(mount), e.message, rows)\n", ""),
    # measurement (inspection_node, MEASUREMENT_INTEGRATION.md)
    ("Complete a scan without its measurement [measurement]", "state.py",
     "        if rt.measured is None:\n            return                                            # detection complete: "
     "the measurement follows\n        try:\n            measurements = self._measurements(scan, rt.measured)",
     "        try:\n            measurements = {} if rt.measured is None else self._measurements(scan, rt.measured)"),
    ("Accept a measurement that does not cover exactly the targets [measurement]", "state.py",
     "if len(set(ids)) != len(ids) or sorted(ids) != targets:", "if False:"),
    ("Measurement failure not failing the scan [measurement]", "state.py",
     "        if not e.success:\n            self._fail(e.scan_id, f\"measurement failed",
     "        if False:\n            self._fail(e.scan_id, f\"measurement failed"),
    ("Second, different measurement silently ignored [measurement]", "state.py",
     "            if rt.measured != e:\n                self._conflict(", "            if False:\n                self._conflict("),
    ("Late conflicting measurement after completion not alerted [measurement]", "state.py",
     "                same = fresh == stored", "                same = True"),
    ("GO range exclusive at its ends [measurement limits]", "models.py",
     '"verdict": "in_limits" if lo <= v <= hi else "out_of_limits"',
     '"verdict": "in_limits" if lo < v < hi else "out_of_limits"'),
    ("Implausible measurement accepted as a value [measurement]", "models.py",
     "if v is None or abs(v) > MAX_ABS_MEASUREMENT_M:", "if v is None:"),
    ("Ready without the measurement node [measurement]", "state.py",
     'if self.source == "ros" and self.measurement_available is not True:', "if False:"),
    ("Detector's own value shown as the measurement [1, 8]", "models.py",
     '    return {"name": name, "value_mm": None, "unit": "mm", "status": "pending",',
     '    return {"name": name, "value_mm": raw_value_m, "unit": "mm", "status": "pending",'),
    ("Detector ready as soon as the graph shows it (first frames lost on the Pi) [6]", "detector_supervisor.py",
     "settled = ok and now - ok_since >= self.settle_s", "settled = ok"),
    ("Restart device runs the power-off command [device]", "api.py",
     'run = engine.device.power_off if action == "off" else engine.device.reboot', "run = engine.device.power_off"),
    # developer service menu (device.py: PIN, kiosk requests)
    ("Any PIN opens the service menu [service]", "device.py",
     "if not isinstance(pin, str) or not hmac.compare_digest(pin.encode(), expected.encode()):", "if False:"),
    ("No lockout after repeated wrong PINs [service]", "device.py",
     "            if now < self.locked_until:", "            if False:"),
    ("Desktop mode during a scan [service]", "state.py",
     "        if self.switching:\n            return False\n        if self._acquiring():",
     "        if self.switching:\n            return False\n        if False:"),
    ("Service action runs without the PIN [service]", "api.py",
     "            svc.access.check(body.get(\"pin\"))\n", ""),
    # operator device actions (device.py, approved deviation from plan section 3)
    ("Gyro calibration allowed during a scan [device]", "state.py",
     'if self.hardware.scan_state != "IDLE" or self._acquiring():', "if False:"),
    ("Second calibration started while one runs [device]", "state.py",
     "        if self.hardware.calibrating:\n            raise CommandError(409, \"calibrating\"",
     "        if False:\n            raise CommandError(409, \"calibrating\""),
    ("Power off while an analysis is pending [device]", "state.py",
     "if self.repo.scans_where(\"analysis = 'pending'\"):\n            raise CommandError(409, \"analyzing\", "
     "\"Wait until the analysis has finished\")\n        self.powering_off = True",
     "if False:\n            raise CommandError(409, \"analyzing\", "
     "\"Wait until the analysis has finished\")\n        self.powering_off = True"),
    ("Power off during a scan [device]", "state.py",
     "        if self.powering_off:\n            return False\n        if self._acquiring():",
     "        if self.powering_off:\n            return False\n        if False:"),
    ("A repeated Turn off runs the command twice [device]", "state.py",
     "        if self.powering_off:\n            return False", "        if False:\n            return False"),
    ("Failed power off leaves the screen saying it is turning off [device]", "state.py",
     "    def power_off_failed(self, message: str) -> None:\n        self.powering_off = False",
     "    def power_off_failed(self, message: str) -> None:\n        pass"),
    ("Calibration running shown as 'Calibration required' [device]", "state.py",
     "            if hw.calibrating:\n                reasons.append(\"Calibrating: keep the device still\")\n            elif",
     "            if False:\n                pass\n            elif"),
]


def run_tests(root: Path) -> tuple[bool, str]:
    try:
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", "-p", "no:warnings",
                            "test"], cwd=root, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return False, "a test hung (> 180 s): the bug blocks progress"
    failed = [l for l in r.stdout.splitlines() if l.startswith("FAILED")]
    return r.returncode == 0, (failed[0][7:] if failed else r.stdout.strip().splitlines()[-1])


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp) / "ui_backend"
        shutil.copytree(SRC, base, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
        ok, summary = run_tests(base)
        print(f"baseline (no bug): {'PASS' if ok else 'FAIL'}  {summary}\n")
        if not ok:
            return 1
        survived = 0
        for name, file, old, new in MUTATIONS:
            work = Path(tmp) / "m"
            shutil.rmtree(work, ignore_errors=True)
            shutil.copytree(base, work)
            path = work / "laser_hhrp_ui" / file
            text = path.read_text()
            if text.count(old) != 1:
                print(f"SKIPPED  {name}: pattern not found exactly once (code changed?)")
                survived += 1
                continue
            path.write_text(text.replace(old, new))
            passed, detail = run_tests(work)
            survived += passed
            print(f"{'SURVIVED' if passed else 'KILLED  '} {name}\n         {'' if passed else 'caught by ' + detail}")
        print(f"\n{len(MUTATIONS) - survived}/{len(MUTATIONS)} planted bugs caught")
        return 1 if survived else 0


if __name__ == "__main__":
    sys.exit(main())
