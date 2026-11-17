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
    ("Report a measured value from detector data [1, 8]", "models.py",
     '"status": "not_measured"}', '"status": "measured"}'),
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
     'if scan["stop_state"] == UNCONFIRMED:\n            return self._fail(',
     'if False:\n            return self._fail('),
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
