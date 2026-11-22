#!/usr/bin/env python3
"""The operator, in a headless Chromium at 800 x 480, on a stack replaying a device bag (replay_stack.sh, UX=1):
identifies, starts the inspection, sets each scan's expected count on the keypad, watches the Scanning screen,
opens a feature on Results and accepts it (or keeps a failed scan as incomplete), every step by taps. Every
screen is checked against the layout rules (fits 800 x 480, touch targets >= 48 px, nothing clipped) and shot.

    python3 ux_operator.py URL MODE COUNTS OUT       # MODE csk_depth | rivet_flushness, COUNTS "5,6,6"

OUT: one PNG per screen and state, ux.json (per scan: the live count over time, the time from release to
Results, the screens' texts). Exit 1 if a layout rule or a step failed.
"""

import faulthandler
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ui_verify"))
import phase4_screens as ps  # noqa: E402

MODE_LABEL = {"csk_depth": "Countersink Depth", "rivet_flushness": "Rivet Flushness"}
failures = []


def check(ok, what):
    if not ps.check(ok, what):
        failures.append(what)
    return ok


def layout(b, name):
    r = b.js(ps.LAYOUT_JS)
    problems = {k: v for k, v in (("outside", r["outside"]), ("small", r["small"]), ("clipped", r["clipped"]),
                                  ("hidden", r["hidden"])) if v}
    fits = r["scroll"][0] <= 800 and r["scroll"][1] <= 480
    check(fits and not problems, f"{name}: layout {'ok' if fits and not problems else (r['scroll'], problems)}")


def wait_screen(api, names, timeout, what):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        s = api.state()
        if s and s.get("screen") in names:
            return s
        time.sleep(0.2)
    raise TimeoutError(f"{what}: screen still {api.state().get('screen')}")


def set_count(b, api, count):
    b.click_at(*b.center("document.querySelector('[data-testid=expected]')"))
    b.wait_text("(1–500)")
    b.type_keys(str(count))
    b.tap("OK")
    api.wait(lambda s: (s.get("inspection") or {}).get("next_expected_count") == count, f"count {count}", 10)


def main(base, mode, counts, out):
    out.mkdir(parents=True, exist_ok=True)
    faulthandler.dump_traceback_later(90, repeat=True, file=sys.stderr)     # where it hangs, if it does
    api = ps.Api(base)
    api.wait(lambda s: True, "backend up", 60)
    exe = ps.find_browser()
    profile = ps.new_profile(exe)
    b = ps.Browser(exe, profile)
    report = []
    try:
        b.send("Page.navigate", url=base + "/?kiosk")
        b.wait_text("Operator identification", 30)
        layout(b, "identify")
        b.type_keys("ENG-1042")
        b.shot(out, "00-identify")
        b.tap("Continue")
        b.wait_text("Start inspection")
        b.tap(MODE_LABEL[mode], exact=False)
        layout(b, "setup")
        b.shot(out, "01-setup")
        b.tap("Start inspection")
        done = set()
        for i, count in enumerate(counts, 1):
            tag = f"scan{i}"
            s = wait_screen(api, ("ready", "not_ready"), 120, f"{tag}: Ready")
            if (s.get("inspection") or {}).get("next_expected_count") != count:
                if i == 1:
                    b.shot(out, f"{tag}-0-not-ready")
                    layout(b, f"{tag} not ready")
                set_count(b, api, count)
                time.sleep(0.3)
            b.shot(out, f"{tag}-1-ready")
            layout(b, f"{tag} ready")
            ready_text = b.text()
            # the switch: the replay presses it at the recorded moment (2 s after Ready at the earliest)
            end = time.monotonic() + 600
            while not ((api.state().get("current_scan") or {}).get("scan_id") not in done | {None}):
                if time.monotonic() > end:
                    raise TimeoutError(f"{tag}: no new scan started")
                time.sleep(0.2)
            t0, live, shots = time.monotonic(), [], 0
            while True:
                s = api.state()
                if s.get("screen") != "scanning":
                    break
                n = (s.get("current_scan") or {}).get("detected_target_count")
                drawn = b.js("[...document.querySelectorAll('.circle')].filter(c => c.dataset.state === 'found').length")
                live.append((round(time.monotonic() - t0, 1), n, drawn))
                if shots < 4 and time.monotonic() - t0 > 3 * shots:
                    b.shot(out, f"{tag}-2-scanning-{shots}")
                    if shots == 0:
                        layout(b, f"{tag} scanning")
                    shots += 1
                time.sleep(0.5)
            released = time.monotonic()
            scanning_text = b.text()
            s = api.state()
            if s.get("screen") == "finishing_analysis":
                b.shot(out, f"{tag}-3-finishing")
                layout(b, f"{tag} finishing")
            s = wait_screen(api, ("results", "analysis_failed"), 300, f"{tag}: Results")
            to_results = round(time.monotonic() - released, 1)
            b.shot(out, f"{tag}-4-results")
            layout(b, f"{tag} results")
            scan = s["current_scan"]
            entry = {"scan": i, "expected": count, "found": scan["detected_target_count"],
                     "analysis": scan["analysis"], "measurement": scan["measurement"], "release_to_results_s": to_results,
                     "live": live, "ready_text": ready_text, "scanning_text": scanning_text, "results_text": b.text()}
            if s["screen"] == "results":
                feats = [f for f in scan["features"] if f["is_target"]]
                drawn = b.js("document.querySelectorAll('[data-feature]').length")
                check(drawn == len(scan["features"]), f"{tag}: map draws every feature ({drawn}/{len(scan['features'])})")
                pos = b.center("document.querySelector('.feat.target')")
                if pos:
                    b.click_at(*pos)
                    time.sleep(0.5)
                    b.shot(out, f"{tag}-5-feature")
                    layout(b, f"{tag} feature detail")
                    entry["feature_text"] = b.text()
                if len(scan["features"]) > 1:      # the stepper: reaches the next feature in scan order
                    before = b.js("document.querySelector('[data-testid=feature-pos]').innerText")
                    b.click_at(*b.center("document.querySelector('[aria-label=\"Next feature\"]')"))
                    time.sleep(0.4)
                    after = b.js("document.querySelector('[data-testid=feature-pos]').innerText")
                    check(after != before and after.startswith("Feature "), f"{tag}: › steps to the next feature ({before} -> {after})")
                    b.shot(out, f"{tag}-6-stepped")
                label = "Accept with warning" if scan["count_mismatch"] else "Accept"
                b.wait_button(label)
                b.tap(label)
                check(len(feats) == scan["detected_target_count"], f"{tag}: targets listed = counted")
            else:
                b.tap("Keep incomplete")
                time.sleep(0.4)
                b.tap("Keep incomplete")
            api.wait(lambda st: (st.get("current_scan") or {}).get("decision"), f"{tag}: decided", 20)
            done.add(scan["scan_id"])
            report.append(entry)
            print(f"{tag}: expected {count}, found {entry['found']}, analysis {entry['analysis']}, "
                  f"measurement {entry['measurement']}, release->results {to_results} s", flush=True)
        wait_screen(api, ("ready", "not_ready"), 60, "back to Ready")
        b.shot(out, "99-ready-after")
        layout(b, "ready after the last scan")
    except Exception as e:      # noqa: BLE001 - report and keep the shots taken so far
        failures.append(f"step failed: {e}")
        print("STEP FAILED:", e, flush=True)
        try:
            b.shot(out, "zz-failure")
        except Exception:      # noqa: BLE001
            pass
    finally:
        (out / "ux.json").write_text(json.dumps(report, indent=1))
        b.close()
        shutil.rmtree(profile, ignore_errors=True)    # the throwaway profile this run made (~130 MB each)
    print("UX: %d check(s) failed" % len(failures) if failures else "UX: all checks passed")
    for f in failures:
        print("  FAIL", f)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2], [int(c) for c in sys.argv[3].split(",")], Path(sys.argv[4])))
