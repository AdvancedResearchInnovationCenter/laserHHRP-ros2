#!/usr/bin/env python3
"""Do the frontend tests catch real UI bugs? Plant one bug at a time in a throwaway copy of ui_web and run
`npm test`: a bug is KILLED if a test fails, SURVIVED if every test still passes.

    python3 tools/ui_verify/web_mutation_check.py      # ~1 min; needs ui_web/node_modules (npm ci)

Each mutation is a plausible mistake against a plan.md rule (section in brackets).
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
WEB = REPO / "ui_web"

MUTATIONS = [
    ("Map y drawn upward instead of downward [9 map]", "src/lib/map.ts",
     "return [l.ox + (x_mm - l.minX) * l.scale, l.oy + (y_mm - l.minY) * l.scale];",
     "return [l.ox + (x_mm - l.minX) * l.scale, l.height - (l.oy + (y_mm - l.minY) * l.scale)];"),
    ("Map stretched to fill (non-uniform scale) [9 map]", "src/lib/map.ts",
     "return [l.ox + (x_mm - l.minX) * l.scale,",
     "return [l.ox + (x_mm - l.minX) * l.scale * 1.3,"),
    ("Tap target only as large as the drawn circle [9 map]", "src/lib/map.ts",
     "px: number, py: number, minHit = MIN_HIT_PX)", "px: number, py: number, minHit = 4)"),
    ("No 40-circle cap for large counts [9.3]", "src/lib/circles.ts",
     "const shown = Math.min(total, max);", "const shown = Math.min(total, max + 1000);"),
    ("Extra detections drawn as ordinary [1 count mismatch]", "src/lib/circles.ts",
     "const extra = expected !== null && i >= expected;", "const extra = false;"),
    ("Accept offered on a failed analysis [4.5]", "src/screens/Results.tsx",
     "  if (!failed) {\n    return (", "  if (!failed || failed) {\n    return ("),
    ("Retry offered whenever raw data is verified [4.5]", "src/screens/Results.tsx",
     "      {scan.retry_available\n", "      {scan.replay_eligible\n"),
    ("A value shown although not measured [1, 8]", "src/lib/format.ts",
     'if (m.status !== "measured" || m.value_mm === null) return "Not measured";',
     'if (m.value_mm === null) return "Not measured";'),
    ("NO-GO shown as GO [measurement]", "src/lib/format.ts",
     'out_of_limits: { label: "NO-GO", tone: "bad" },', 'out_of_limits: { label: "GO", tone: "ok" },'),
    ("Flushness shown without its sign [measurement]", "src/lib/format.ts",
     '`${v > 0 ? "+" : v < 0 ? "\\u2212" : ""}${Math.abs(v).toFixed(3)} mm`', '`${Math.abs(v).toFixed(3)} mm`'),
    ("Why a feature was not measured is hidden [measurement]", "src/ui/FeatureDetail.tsx",
     "{reason && <><dt>Why</dt>", "{false && <><dt>Why</dt>"),
    ("Map draws a NO-GO feature as GO [measurement]", "src/ui/FeatureMap.tsx",
     'return m.verdict === "in_limits" ? "go" : "nogo";', 'return "go";'),
    ("Mismatch warning hidden on Results [1]", "src/screens/Results.tsx",
     "{!failed && mismatch && (", "{false && ("),
    ("UI-side partial threshold instead of the detector flag [9 partial]", "src/screens/Scanning.tsx",
     "targets={targets} width={cw} height={ch} />",
     "targets={targets.map((f) => ({ partial: (f.quality.scanned_share ?? 1) < 0.95 }))} width={cw} height={ch} />"),
    ("Scanning counts circles for non-target features [5 filter]", "src/screens/Scanning.tsx",
     "const targets = features.filter((f) => f.is_target);", "const targets = features;"),
    ("Offline state not shown [8 browser crash/reload]", "src/App.tsx",
     "      {!connected && !poweringOff && (\n        <div className=\"offline\"", "      {false && (\n        <div className=\"offline\""),
    ("Duplicate events accepted [7 event_seq]", "src/state/snapshotSync.ts",
     "if (eventSeq <= this.seq) return false;", "if (eventSeq < this.seq) return false;"),
    ("Not-ready reasons not listed [9 ready policy]", "src/screens/Ready.tsx",
     "{snap.not_ready_reasons.map((r) => <li key={r}>{r}</li>)}", "{null}"),
    ("Unverified clock not flagged [8 offline time]", "src/ui/Header.tsx",
     '{snap.clock_quality === "unverified" && <span className="pill warn">Time not verified</span>}', "{null}"),
    # operator device actions (backend device.py)
    ("Turn off offered during a scan [device]", "src/ui/Device.tsx",
     'export const POWER_OFF_SCREENS: readonly Screen[] = ["identify", "setup", "ready", "not_ready"];',
     'export const POWER_OFF_SCREENS: readonly Screen[] = ["identify", "setup", "ready", "not_ready", "scanning"];'),
    ("Turn off offered without a power-off command [device]", "src/ui/Device.tsx",
     "return connected && snap.device.can_power_off && !snap.device.powering_off",
     "return connected && !snap.device.powering_off"),
    ("Turning off replaced by the offline notice [device]", "src/App.tsx",
     "{!connected && !poweringOff && (", "{!connected && ("),
    ("Failed calibration reported as done [device]", "src/lib/format.ts",
     'if (m.startsWith("calibration failed")) {', 'if (false) {'),
    ("Second calibration offered while one runs [device]", "src/screens/Ready.tsx",
     'disabled={calibrating} onClick={() => setCalibrate(true)}>Calibrate gyro', 'onClick={() => setCalibrate(true)}>Calibrate gyro'),
]


def run_tests(root: Path) -> tuple[bool, str]:
    try:
        r = subprocess.run(["npm", "test", "--silent"], cwd=root, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return False, "tests hung"
    out = r.stdout + r.stderr
    failed = [l for l in out.splitlines() if l.startswith("FAIL") or "error TS" in l]
    return r.returncode == 0, (failed[0][:110] if failed else out.strip().splitlines()[-1] if out.strip() else "")


def main() -> int:
    if not (WEB / "node_modules").is_dir():
        print("ui_web/node_modules missing: run `npm ci` in ui_web first", file=sys.stderr)
        return 3
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp) / "ui_web"
        shutil.copytree(WEB, base, ignore=shutil.ignore_patterns("node_modules", "dist", ".test-out"))
        os.symlink(WEB / "node_modules", base / "node_modules")
        ok, summary = run_tests(base)
        print(f"baseline (no bug): {'PASS' if ok else 'FAIL'}  {summary}\n")
        if not ok:
            return 1
        survived = 0
        for name, file, old, new in MUTATIONS:
            path = base / file
            original = path.read_text()
            if original.count(old) != 1:
                print(f"SKIPPED  {name}: pattern not found exactly once (code changed?)")
                survived += 1
                continue
            path.write_text(original.replace(old, new))
            try:
                passed, detail = run_tests(base)
            finally:
                path.write_text(original)
            survived += passed
            print(f"{'SURVIVED' if passed else 'KILLED  '} {name}\n         {'' if passed else 'caught by ' + detail}")
        print(f"\n{len(MUTATIONS) - survived}/{len(MUTATIONS)} planted UI bugs caught")
        return 1 if survived else 0


if __name__ == "__main__":
    sys.exit(main())
