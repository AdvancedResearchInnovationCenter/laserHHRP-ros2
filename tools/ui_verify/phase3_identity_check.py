#!/usr/bin/env python3
"""Phase 3 identity layer against the simulated core (no detectors, no backend).

Checks the controller's scan/session_state, the synchronizer's scan/frame_tagged + scan/stream_closed and the
recorder's scan/recording_session for: a normal scan, pause/resume, and two scans started in the same
second. Prints PASS/FAIL per check; exit code = number of FAILs. Run by phase3_identity_check.sh.
"""

import os
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path

import rclpy
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from std_srvs.srv import Trigger

from laser_hhrp.msg import (ScanFrame, ScanRecordingSession, ScanSessionState, ScanStatus, ScanStreamClosed,
                            TaggedScanFrame)

FAILS = []
LATCHED = QoSProfile(depth=50, reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.TRANSIENT_LOCAL)


def check(name, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {name}{('  -- ' + str(detail)) if detail else ''}", flush=True)
    if not ok:
        FAILS.append(name)
    return ok


def key(t):
    return (t.sec, t.nanosec)


class Probe:
    def __init__(self):
        rclpy.init()
        n = self.node = rclpy.create_node("phase3_identity_probe")
        self.lock = threading.Lock()
        self.status_transitions = []          # (state, stamp key) of scan/status state changes
        self._last_state = None
        self.sessions = []                    # transitions only
        self.legacy = []                      # ScanFrame indices in arrival order (per scan: restarts at 0)
        self.tagged = defaultdict(list)       # scan_id -> indices
        self.closed = {}                      # scan_id -> ScanStreamClosed
        self.recordings = []
        n.create_subscription(ScanStatus, "scan/status", self.on_status, 50)
        n.create_subscription(ScanSessionState, "scan/session_state", self.on_session, LATCHED)
        n.create_subscription(ScanFrame, "scan/frame", lambda m: self._add(self.legacy, m.index), 500)
        n.create_subscription(TaggedScanFrame, "scan/frame_tagged", self.on_tagged, 500)
        n.create_subscription(ScanStreamClosed, "scan/stream_closed", self.on_closed, LATCHED)
        n.create_subscription(ScanRecordingSession, "scan/recording_session", self.on_recording, LATCHED)
        self.clients = {s: n.create_client(Trigger, s) for s in
                        ("calibration/start", "scan/start", "scan/stop", "scan/pause")}
        threading.Thread(target=rclpy.spin, args=(n,), daemon=True).start()

    def _add(self, lst, v):
        with self.lock:
            lst.append(v)

    def on_status(self, m):
        with self.lock:
            if m.state != self._last_state:
                self.status_transitions.append((m.state, key(m.header.stamp)))
                self._last_state = m.state

    def on_session(self, m):
        if m.is_transition:
            self._add(self.sessions, m)

    def on_tagged(self, m):
        with self.lock:
            self.tagged[m.scan_id].append(m.frame.index)

    def on_closed(self, m):
        with self.lock:
            self.closed[m.scan_id] = m

    def on_recording(self, m):
        self._add(self.recordings, m)

    def call(self, name):
        c = self.clients[name]
        c.wait_for_service(timeout_sec=20)
        fut = c.call_async(Trigger.Request())
        end = time.monotonic() + 10
        while not fut.done() and time.monotonic() < end:
            time.sleep(0.01)
        r = fut.result()
        return bool(r and r.success), (r.message if r else "no reply")

    def wait(self, pred, timeout):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self.lock:
                if pred():
                    return True
            time.sleep(0.05)
        return False


def main():
    p = Probe()
    p.call("calibration/start")
    time.sleep(4)

    # ---- normal scan ----
    s0 = len(p.sessions)
    check("scan/start", p.call("scan/start")[0])
    time.sleep(1.5)
    check("scan/stop", p.call("scan/stop")[0])
    p.wait(lambda: len(p.closed) >= 1, 8)
    tr = p.sessions[s0:]
    check("two session transitions (start, stop) with one new scan_id",
          len(tr) == 2 and tr[0].scan_id == tr[1].scan_id and len(tr[0].scan_id) == 36
          and [t.state for t in tr] == [ScanSessionState.SCANNING, ScanSessionState.IDLE],
          [(t.state, t.scan_id[:8]) for t in tr])
    starts = {k for st, k in p.status_transitions if st == ScanStatus.SCANNING}
    stops = {k for st, k in p.status_transitions if st == ScanStatus.IDLE}
    check("status_stamp equals the exact scan/status stamp of each transition",
          key(tr[0].status_stamp) in starts and key(tr[1].status_stamp) in stops)
    check("transition_seq increments by 1", tr[1].transition_seq == tr[0].transition_seq + 1)
    a = tr[0].scan_id
    with p.lock:
        tagged, closed = list(p.tagged[a]), p.closed.get(a)
        legacy_n = len(p.legacy)
    check("tagged frames carry the scan_id, indices 0..n-1 in order", tagged == list(range(len(tagged))) and tagged,
          f"{len(tagged)} frames")
    check("tagged frame count equals the legacy frames of the scan", len(tagged) == legacy_n, f"{len(tagged)} vs {legacy_n}")
    check("stream_closed: frame_count = tagged frames, stop IDLE, no error",
          closed is not None and closed.frame_count == len(tagged) and closed.stop_state == ScanStatus.IDLE
          and closed.error == "", closed and (closed.frame_count, closed.stop_state, closed.error))
    rec = [r for r in p.recordings if key(r.status_stamp) == key(tr[0].status_stamp)]
    recc = [r for r in p.recordings if key(r.status_stamp) == key(tr[1].status_stamp)]
    check("recorder OPENED with the start's status stamp", len(rec) == 1 and rec[0].event == ScanRecordingSession.OPENED)
    check("recorder CLOSED with the stop's status stamp, same session, frame count = legacy frames",
          len(recc) == 1 and recc[0].event == ScanRecordingSession.CLOSED and recc[0].session_id == rec[0].session_id
          and recc[0].frame_count == legacy_n and not recc[0].write_failed,
          recc and (recc[0].frame_count, legacy_n))
    check("session folder holds frames.bin", (Path(rec[0].session_dir) / "frames.bin").is_file() if rec else False)

    # ---- pause / resume: one scan ----
    s0, n0 = len(p.sessions), len(p.closed)
    p.call("scan/start")
    time.sleep(0.6)
    check("pause", p.call("scan/pause")[0])
    time.sleep(0.5)
    check("resume", p.call("scan/pause")[0])
    time.sleep(0.6)
    p.call("scan/stop")
    p.wait(lambda: len(p.closed) > n0, 8)
    tr = p.sessions[s0:]
    ids = {t.scan_id for t in tr}
    b = tr[0].scan_id if tr else ""
    with p.lock:
        tb, cb = list(p.tagged[b]), p.closed.get(b)
    check("pause/resume keeps one scan_id over 4 transitions", len(tr) == 4 and len(ids) == 1 and b != a,
          [t.state for t in tr])
    check("pause/resume: tagged indices continuous, closed count matches",
          tb == list(range(len(tb))) and cb is not None and cb.frame_count == len(tb), len(tb))

    # ---- two scans within one second ----
    while time.time() % 1 > 0.15:
        time.sleep(0.01)
    s0, r0 = len(p.sessions), len(p.recordings)
    p.call("scan/start"); time.sleep(0.25); p.call("scan/stop")
    p.call("scan/start"); time.sleep(0.25); p.call("scan/stop")
    p.wait(lambda: len(p.recordings) >= r0 + 4, 8)
    tr = p.sessions[s0:]
    ids = [t.scan_id for t in tr if t.state == ScanSessionState.SCANNING]
    opened = [r for r in p.recordings[r0:] if r.event == ScanRecordingSession.OPENED]
    check("same-second scans: two distinct scan_ids", len(ids) == 2 and ids[0] != ids[1])
    check("same-second scans: two distinct raw session folders",
          len(opened) == 2 and opened[0].session_dir != opened[1].session_dir,
          [o.session_id for o in opened])
    sizes = [(Path(o.session_dir) / "frames.bin").stat().st_size if (Path(o.session_dir) / "frames.bin").exists() else -1
             for o in opened]
    check("same-second scans: neither frames.bin truncated by the other", all(sz > 0 for sz in sizes), sizes)
    with p.lock:
        ok = all(p.closed.get(i) is not None and p.closed[i].frame_count == len(p.tagged[i]) for i in ids)
    check("same-second scans: each stream closed with its own frame count", ok)

    print(f"\n{'ALL PASSED' if not FAILS else f'{len(FAILS)} FAILED: ' + ', '.join(FAILS)}", flush=True)
    return len(FAILS)


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    os._exit(code)
