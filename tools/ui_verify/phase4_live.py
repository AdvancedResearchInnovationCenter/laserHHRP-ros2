#!/usr/bin/env python3
"""Phase 4 on live ROS input: the built operator UI in a headless browser, tapped like an operator, against
the real backend on a simulated core (run by phase4_live.sh). The physical switch is scan/start and
scan/stop (what scan_switch_node calls). The simulator detects nothing, so scans complete with zero
features; detections through the live path are covered by the Phase 3 checks.
"""

import os
import shutil
import sys
import threading
import time
from pathlib import Path

import rclpy
from std_srvs.srv import Trigger

sys.path.insert(0, str(Path(__file__).resolve().parent))
from phase4_screens import RESULTS, Api, Browser, check, find_browser, layout_ok, new_profile  # noqa: E402

BASE = os.environ.get("UI_BASE", "http://127.0.0.1:8098")


class Probe:
    def __init__(self):
        rclpy.init()
        self.node = rclpy.create_node("phase4_probe")
        self.clients = {n: self.node.create_client(Trigger, n) for n in ("calibration/start", "scan/start", "scan/stop")}
        threading.Thread(target=rclpy.spin, args=(self.node,), daemon=True).start()

    def call(self, name):
        c = self.clients[name]
        if not c.wait_for_service(timeout_sec=20):
            return False
        fut = c.call_async(Trigger.Request())
        end = time.monotonic() + 10
        while not fut.done() and time.monotonic() < end:
            time.sleep(0.01)
        return bool(fut.result() and fut.result().success)

    def nodes(self, name):
        return sum(1 for n, _ in self.node.get_node_names_and_namespaces() if n == name)


def main(out: Path) -> int:
    out.mkdir(parents=True, exist_ok=True)
    api = Api(BASE)
    api.wait(lambda s: True, "backend up", 60)
    p = Probe()
    exe = os.environ.get("BROWSER") or find_browser()
    profile = new_profile(exe)
    b = Browser(exe, profile)
    try:
        check(api.state()["source"] == "ros", "backend runs on the live ROS source")
        b.send("Page.navigate", url=BASE + "/?kiosk")
        b.wait_text("Operator identification")
        b.type_keys("ENG-1042")
        b.tap("Continue")
        b.wait_text("Start inspection")
        b.tap("Countersink Depth", exact=False)
        b.tap("Start inspection")
        api.wait(lambda s: s["detector"]["ready"], "hole_detector ready", 60)
        end = time.monotonic() + 15                      # the probe's view of the ROS graph lags
        while time.monotonic() < end and (p.nodes("hole_detector"), p.nodes("rivet_detector")) != (1, 0):
            time.sleep(0.2)
        check(p.nodes("hole_detector") == 1 and p.nodes("rivet_detector") == 0,
              "CSK inspection from the screen starts exactly one hole_detector")
        b.wait_text("Calibration required", 20)
        layout_ok(b, "live not_ready")
        b.shot(out, "live-01-not-ready")
        check(True, "NOT READY lists live hardware reasons (calibration required)")
        b.tap("Set")
        b.type_keys("3")
        b.tap("OK")
        check(p.call("calibration/start"), "calibration (ROS service)")
        b.wait_text("Hold the switch to scan", 30)
        layout_ok(b, "live ready")
        b.shot(out, "live-02-ready")
        check(api.state()["ready"], "READY from live status: calibrated, laser and Nano fresh, detector ready")

        # ---- one scan: switch held 2.5 s ----
        check(p.call("scan/start"), "switch pressed (scan/start)")
        b.wait_text("Scanning")
        time.sleep(1.5)
        layout_ok(b, "live scanning")
        b.shot(out, "live-03-scanning")
        dist = api.state()["hardware"]["distance_mm"]
        check(api.state()["screen"] == "scanning", f"SCANNING shown from the controller's transition ({dist:.0f} mm)")
        time.sleep(1.0)
        check(p.call("scan/stop"), "switch released (scan/stop)")
        api.wait(lambda s: s["screen"] == "results", "results", 60)
        b.wait_button("Accept with warning")
        s = api.state()
        sc = s["current_scan"]
        check(sc["analysis"] == "complete" and sc["frame_count"] and sc["raw"]["state"],
              f"analysis complete from the detector's finalization ({sc['frame_count']} frames, raw {sc['raw']['state']})")
        layout_ok(b, "live results")
        b.shot(out, "live-04-results")
        check("nothing found" in b.text() and "3 fewer than expected" in b.text(),
              "zero-feature scan shown honestly (0 / 3, nothing found)")
        b.tap("Accept with warning")
        b.wait_text("Hold the switch to scan")

        # ---- two quick presses: the first stays UNDECIDED ----
        p.call("scan/start"); time.sleep(0.6); p.call("scan/stop")
        api.wait(lambda s: s["screen"] == "results", "first quick scan analysed", 60)
        p.call("scan/start"); time.sleep(0.6); p.call("scan/stop")
        api.wait(lambda s: s["screen"] == "results" and s["inspection"]["scan_count"] == 3, "second quick scan", 60)
        b.wait_button("Rescan")
        b.tap("History")
        b.wait_text("Download CSV")
        b.wait_text("Undecided")
        layout_ok(b, "live history")
        b.shot(out, "live-05-history")
        scans = api.call("GET", f"/inspections/{s['inspection']['uuid']}/scans")[1]
        check([x["status"] for x in scans] == ["accepted_with_warning", "undecided", "undecided"],
              f"history: Scan 002 stays UNDECIDED while Scan 003 is current ({[x['status'] for x in scans]})")
        b.tap("Scan 002", exact=False)
        b.wait_button("Rescan")
        b.tap("Rescan")
        api.wait(lambda s: s["recent_scans"][1]["status"] == "rescanned", "decided from history")
        check(True, "Scan 002 decided later from History")
        b.tap("‹ Back")
        b.wait_button("Accept with warning")

        # ---- reload while scanning: same scan, nothing restarted ----
        b.tap("Rescan")
        b.wait_text("Hold the switch to scan")
        p.call("scan/start")
        api.wait(lambda s: s["screen"] == "scanning", "scanning", 20)
        before = api.state()["current_scan"]["scan_id"]
        b.send("Page.reload")
        b.wait_text("Scanning")
        check(api.state()["current_scan"]["scan_id"] == before and api.state()["screen"] == "scanning",
              "browser reload while scanning: same scan continues")
        p.call("scan/stop")
        api.wait(lambda s: s["screen"] == "results", "results", 60)
        b.wait_button("Rescan")
        b.tap("Rescan")
        b.wait_text("Hold the switch to scan")

        # ---- end inspection, rivet inspection: exactly one rivet_detector ----
        b.tap("End inspection")
        b.wait_text("End inspection?")
        b.tap("End inspection")
        b.wait_text("Start inspection")
        b.tap("Rivet Flushness", exact=False)
        b.tap("Start inspection")
        api.wait(lambda s: s["detector"]["ready"] and s["detector"]["mode"] == "rivet_flushness", "rivet ready", 60)
        end = time.monotonic() + 15
        while time.monotonic() < end and (p.nodes("rivet_detector"), p.nodes("hole_detector")) != (1, 0):
            time.sleep(0.2)
        check(p.nodes("rivet_detector") == 1 and p.nodes("hole_detector") == 0,
              "mode switch from the screens: exactly one rivet_detector, hole_detector gone")
        b.wait_text("Hold the switch to scan", 30)
        b.shot(out, "live-06-rivet-ready")
        check(api.state()["inspection"]["next_expected_count"] == 3, "expected count remembered across inspections")
    except Exception as e:
        check(False, f"step failed: {e}")
    finally:
        b.close()
        shutil.rmtree(profile, ignore_errors=True)
    failed = [w for ok, w in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed; screenshots in {out}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
