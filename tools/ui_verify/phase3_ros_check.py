#!/usr/bin/env python3
"""Phase 3 end-to-end live check: simulated core + real backend + supervised tagged detector + real ROS graph.

Run by phase3_ros_check.sh (core: simulate:=true holes:=false ui:=false scan_switch:=false; backend started from
its installed executable). The operator is played over HTTP, the physical switch by calling scan/start and
scan/stop (what scan_switch_node calls). The simulator detects nothing, so recorded scans complete with zero
features: the detection path itself is checked by phase3_detector_check. PASS/FAIL per check; exit = FAILs.
"""

import json
import os
import signal
import sqlite3
import sys
import threading
import time
import urllib.request

import rclpy
from std_srvs.srv import Trigger

from laser_hhrp.msg import ScanFrame, ScanSessionState

API = os.environ.get("UI_API", "http://127.0.0.1:8096/api/v1")
DATA = os.environ["UI_DATA"]
FAILS = []


def check(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}{('  -- ' + str(detail)[:220]) if detail else ''}", flush=True)
    if not ok:
        FAILS.append(name)
    return ok


def http(method, path, body=None):
    req = urllib.request.Request(API + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")
    except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
        return 0, {"error": str(e)}


def state():
    return http("GET", "/state")[1]


def wait(pred, timeout, period=0.2):
    end = time.monotonic() + timeout
    last = None
    while time.monotonic() < end:
        last = pred()
        if last:
            return last
        time.sleep(period)
    return last


def detector_procs(name):
    pids = []
    for d in os.listdir("/proc"):
        if d.isdigit():
            try:
                cmd = open(f"/proc/{d}/cmdline", "rb").read().split(b"\0")
                st = open(f"/proc/{d}/stat").read().rsplit(")", 1)[-1].split()[0]
            except OSError:
                continue
            if st != "Z" and any(c.endswith(f"/lib/{name}/{name}".encode()) for c in cmd):
                pids.append(int(d))
    return pids


class Probe:
    def __init__(self):
        rclpy.init()
        self.node = rclpy.create_node("phase3_probe")
        self.frames, self.sessions, self.lock = [], [], threading.Lock()
        self.node.create_subscription(ScanFrame, "scan/frame", lambda m: self._add(self.frames, time.monotonic()), 500)
        self.node.create_subscription(ScanSessionState, "scan/session_state",
                                      lambda m: m.is_transition and self._add(self.sessions, m), 50)
        self.clients = {n: self.node.create_client(Trigger, n) for n in ("calibration/start", "scan/start", "scan/stop")}
        threading.Thread(target=rclpy.spin, args=(self.node,), daemon=True).start()

    def _add(self, lst, v):
        with self.lock:
            lst.append(v)

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


def scans_db():
    db = sqlite3.connect(os.path.join(DATA, "inspections.db"))
    db.row_factory = sqlite3.Row
    return [dict(r) for r in db.execute("SELECT * FROM scans ORDER BY rowid")]


def main():
    p = Probe()
    ok = wait(lambda: http("GET", "/health")[0] == 200, 30)
    h = http("GET", "/health")[1] if ok else {}
    if not check("backend up with live ROS (tagged) source", h.get("source") == "ros", h):
        return 1
    http("POST", "/operator", {"operator_id": "ENG-1042"})
    code, insp = http("POST", "/inspections", {"mode": "csk_depth", "label": "P3-CSK"})
    check("CSK inspection created", code == 200, insp)
    st = wait(lambda: (s := state())["detector"]["ready"] and s, 60)
    check("hole_detector ready on the tagged topics", bool(st), state()["detector"])
    wait(lambda: p.nodes("hole_detector") == 1, 10)
    check("exactly one hole_detector, no rivet_detector", (p.nodes("hole_detector"), p.nodes("rivet_detector")) == (1, 0))
    http("PATCH", f"/inspections/{insp['uuid']}/next-expected-count", {"count": 3})
    check("calibration", p.call("calibration/start"))
    check("READY", bool(wait(lambda: state()["screen"] == "ready", 20)), state()["not_ready_reasons"])

    # ---- one recorded scan ----
    n0, s0 = len(p.frames), len(p.sessions)
    t0 = time.monotonic()
    check("scan/start (the switch)", p.call("scan/start"))
    check("SCANNING with a recorded scan", bool(wait(lambda: (s := state())["screen"] == "scanning"
                                                     and s["current_scan"] and s, 5)))
    time.sleep(2.0)
    check("scan/stop (release)", p.call("scan/stop"))
    rate = (len(p.frames) - n0) / (time.monotonic() - t0)
    st = wait(lambda: (s := state())["screen"] in ("results", "analysis_failed") and s, 30)
    cur = (st or state())["current_scan"] or {}
    check("analysis COMPLETE from the detector's finalization (0 features in the simulator)",
          st and st["screen"] == "results" and cur.get("analysis") == "complete", cur.get("analysis_error"))
    with p.lock:
        ids = [m.scan_id for m in p.sessions[s0:]]
    check("scan recorded under the controller's scan UUID", cur.get("scan_id") in ids and len(set(ids)) == 1, ids)
    check("frame rate with tagged frames + detector (sim 120 Hz)", rate >= 110, f"{rate:.0f} frames/s")
    check("count mismatch 0/3 flagged, measurement not invented",
          cur.get("detected_target_count") == 0 and cur.get("count_mismatch") is True)
    raw = cur.get("raw") or {}
    check("raw recording linked by stamp and verified (replay-eligible)",
          raw.get("state") == "closed" and cur.get("replay_eligible") and "verified" in (raw.get("note") or ""), raw)
    check("raw frame count = frames the scan published", raw.get("frame_count") == cur.get("frame_count")
          and cur.get("frame_count", 0) > 0, (raw.get("frame_count"), cur.get("frame_count")))
    out = os.path.join(DATA, "detector-output", insp["uuid"], cur.get("scan_id", "?"), "summary.csv")
    check("detector folder named by the scan UUID, made although nothing was found", os.path.isfile(out), out)
    r = http("POST", f"/scans/{cur['scan_id']}/decision", {"decision": "accept"})
    check("Accept (with warning) recorded; back to READY", r[0] == 200 and r[1].get("status") == "accepted_with_warning"
          and wait(lambda: state()["screen"] == "ready", 5), r)

    # ---- quick second press: two scans, nothing crosses ----
    s0 = len(p.sessions)
    p.call("scan/start"); time.sleep(0.6); p.call("scan/stop")
    p.call("scan/start"); time.sleep(0.6); p.call("scan/stop")
    wait(lambda: all(s["analysis"] != "pending" for s in scans_db()) and len(scans_db()) >= 3, 30)
    rows = scans_db()[-2:]
    with p.lock:
        ids = list(dict.fromkeys(m.scan_id for m in p.sessions[s0:]))
    check("rapid presses: two scans recorded with the controller's two UUIDs", [r["scan_id"] for r in rows] == ids, ids)
    check("rapid presses: both analyses complete", all(r["analysis"] == "complete" for r in rows),
          [(r["analysis"], r["analysis_error"]) for r in rows])
    check("rapid presses: separate raw folders, both verified",
          rows[0]["raw_dir"] != rows[1]["raw_dir"] and all(r["replay_eligible"] for r in rows),
          [(r["raw_dir"], r["raw_note"]) for r in rows])
    check("the earlier of the two stays UNDECIDED (not auto-decided)",
          http("GET", f"/scans/{rows[0]['scan_id']}")[1].get("status") == "undecided")
    http("POST", f"/scans/{rows[0]['scan_id']}/decision", {"decision": "rescan"})
    http("POST", f"/scans/{rows[1]['scan_id']}/decision", {"decision": "accept"})

    # ---- detector crash during an inspection ----
    pid = (detector_procs("hole_detector") or [None])[0]
    os.kill(pid, signal.SIGKILL)
    check("crash: NOT READY with the exit reason", bool(wait(lambda: "exited with code" in " ".join(
        state()["not_ready_reasons"]), 10)), state()["not_ready_reasons"])
    check("explicit restart, ready again", http("POST", "/detector/restart")[0] == 200 and bool(
        wait(lambda: state()["detector"]["ready"], 60)))

    # ---- scan without an inspection: unassigned, never attributed ----
    check("end inspection", http("POST", f"/inspections/{insp['uuid']}/end")[0] == 200)
    check("detector stopped with the inspection", bool(wait(lambda: not detector_procs("hole_detector"), 30)))
    p.call("scan/start"); time.sleep(0.4); p.call("scan/stop")
    time.sleep(1.0)
    last = scans_db()[-1]
    check("a scan with no inspection is stored UNASSIGNED with the reason",
          last["inspection_uuid"] is None and last["unassigned_reason"] == "no open inspection", last["unassigned_reason"])

    # ---- the other mode ----
    code, insp2 = http("POST", "/inspections", {"mode": "rivet_flushness", "label": "P3-RIV"})
    check("rivet inspection: exactly one rivet_detector, ready", bool(wait(lambda: state()["detector"]["ready"]
          and state()["detector"]["mode"] == "rivet_flushness", 60)) and detector_procs("rivet_detector")
          and not detector_procs("hole_detector"))
    print(f"\n{'ALL PASSED' if not FAILS else f'{len(FAILS)} FAILED: ' + ', '.join(FAILS)}", flush=True)
    return len(FAILS)


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    os._exit(code)
