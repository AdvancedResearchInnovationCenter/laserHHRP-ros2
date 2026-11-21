"""Detector supervisor with real processes and signals, fake detector executables and a fake ROS graph."""

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from laser_hhrp_ui.detector_supervisor import DetectorSupervisor
from laser_hhrp_ui.models import Mode

GOOD = """#!{py}
import json, signal, sys, time
from pathlib import Path
signal.signal(signal.SIGINT, lambda *_: sys.exit(0))     # like rclpy: handler first, then "in the graph"
out = [a.split(":=", 1)[1] for a in sys.argv if a.startswith("out_dir:=")][0]
Path(out, "argv.json").write_text(json.dumps(sys.argv))
while True:
    time.sleep(0.05)
"""
CRASH = """#!{py}
import time, sys
time.sleep(0.3)
sys.exit(3)
"""
STUBBORN = """#!{py}
import signal, sys, time
from pathlib import Path
signal.signal(signal.SIGINT, signal.SIG_IGN)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
out = [a.split(":=", 1)[1] for a in sys.argv if a.startswith("out_dir:=")][0]
Path(out, "argv.json").write_text("[]")
while True:
    time.sleep(0.05)
"""


class FakeGraph:
    """A detector node 'exists' while its supervised process is alive; extra (foreign) nodes settable."""

    def __init__(self):
        self.sup = None
        self.foreign = {}
        self.ready = True

    def node_count(self, name):
        sup = self.sup
        own = int(sup is not None and sup.proc is not None and sup.proc.poll() is None and sup.mode is not None
                  and {"hole_detector": Mode.CSK_DEPTH, "rivet_detector": Mode.RIVET_FLUSHNESS}[name] == sup.mode
                  and sup.out_dir is not None and (sup.out_dir / "argv.json").exists())   # node initialized
        return own + self.foreign.get(name, 0)

    def subscribes(self, name, topic):
        return self.ready

    def publishes(self, name, topic):
        return self.ready


class Collector:
    def __init__(self):
        self.items, self.lock = [], threading.Lock()

    def __call__(self, st):
        with self.lock:
            self.items.append(st)

    def wait(self, pred, timeout=10.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self.lock:
                for st in self.items:
                    if pred(st):
                        return st
            time.sleep(0.02)
        raise AssertionError(f"no status matched; got {self.items}")

    def last(self):
        with self.lock:
            return self.items[-1]


def script(tmp_path, name, body):
    p = tmp_path / "bin" / name
    p.parent.mkdir(exist_ok=True)
    p.write_text(body.format(py=sys.executable))
    p.chmod(0o755)
    return str(p)


@pytest.fixture
def make(tmp_path):
    made = []

    def _make(bodies=None, **kw):
        bodies = bodies or {"hole_detector": GOOD, "rivet_detector": GOOD}
        params = tmp_path / "params.yaml"
        params.write_text("x: {}\n")
        exes = {n: script(tmp_path, n, b) for n, b in bodies.items()}
        graph, got = FakeGraph(), Collector()
        kw.setdefault("settle_s", 0.2)
        sup = DetectorSupervisor(got, graph, tmp_path / "out", tmp_path / "logs",
                                 resolve=lambda n: (exes[n], str(params)), poll_s=0.05, health_period_s=0.1, **kw)
        graph.sup = sup
        made.append(sup)
        return sup, graph, got
    yield _make
    for sup in made:
        try:
            sup.shutdown(timeout=30)
        except Exception:
            pass


def test_start_ready_fixed_argv_and_one_process(make, tmp_path):
    sup, graph, got = make()
    sup.request(Mode.RIVET_FLUSHNESS, "11111111-2222")
    st = got.wait(lambda s: s.ready)
    assert st.mode == "rivet_flushness" and st.alive
    pid = sup.proc.pid
    argv = (tmp_path / "out" / "11111111-2222" / "argv.json").read_text()
    assert '"--ros-args", "--params-file"' in argv and "out_dir:=" in argv
    sup.request(Mode.RIVET_FLUSHNESS, "11111111-2222")       # same mode again: no second process
    time.sleep(0.3)
    assert sup.proc.pid == pid
    assert (tmp_path / "logs" / "detector.pid").read_text().split()[0] == str(pid)


def test_mode_change_stops_first_before_starting_second(make):
    sup, graph, got = make()
    sup.request(Mode.CSK_DEPTH, "a")
    got.wait(lambda s: s.ready and s.mode == "csk_depth")
    first = sup.proc
    sup.request(Mode.RIVET_FLUSHNESS, "b")
    got.wait(lambda s: s.ready and s.mode == "rivet_flushness")
    assert first.poll() == 0                                # exited cleanly on SIGINT
    modes = [s.mode for s in got.items]
    assert modes.index(None) > modes.index("csk_depth")     # a stop was published between the two


def test_crash_reported_and_not_restarted_automatically(make):
    sup, graph, got = make({"hole_detector": CRASH, "rivet_detector": GOOD})
    sup.request(Mode.CSK_DEPTH, "a")
    st = got.wait(lambda s: not s.alive and s.error)
    assert st.error == "hole_detector exited with code 3" and st.mode == "csk_depth"
    time.sleep(0.5)
    assert not got.last().alive                              # no silent restart
    assert not (sup.log_dir / "detector.pid").exists()


def test_stop_escalates_to_sigkill(make):
    sup, graph, got = make({"hole_detector": STUBBORN, "rivet_detector": GOOD}, stop_timeout_s=0.5)
    sup.request(Mode.CSK_DEPTH, "a")
    got.wait(lambda s: s.ready)
    proc = sup.proc
    sup.stop()
    got.wait(lambda s: s.mode is None and not s.alive)
    assert proc.poll() == -signal.SIGKILL


def test_foreign_detector_blocks_start_and_duplicates_unready(make):
    sup, graph, got = make()
    graph.foreign = {"hole_detector": 1}                    # core launched with holes:=true
    sup.request(Mode.RIVET_FLUSHNESS, "a")
    st = got.wait(lambda s: s.error)
    assert "hole_detector node is already running" in st.error and not st.alive and sup.proc is None
    graph.foreign = {}
    sup.request(Mode.CSK_DEPTH, "a")
    got.wait(lambda s: s.ready)
    graph.foreign = {"hole_detector": 1}                    # someone starts a second one later
    st = got.wait(lambda s: s.alive and not s.ready and "2 hole_detector nodes" in s.error)
    assert st.mode == "csk_depth"


def test_never_ready_reports_timeout(make):
    sup, graph, got = make(start_timeout_s=0.5)
    graph.ready = False
    sup.request(Mode.CSK_DEPTH, "a")
    st = got.wait(lambda s: s.error)
    assert "not ready after" in st.error and st.alive and not st.ready


def test_orphan_from_crashed_backend_is_stopped(make, tmp_path):
    sup, graph, got = make()
    exe = script(tmp_path, "hole_detector", GOOD)
    out = tmp_path / "orphan"
    out.mkdir()
    orphan = subprocess.Popen([exe, "--ros-args", "-p", f"out_dir:={out}"], start_new_session=True)
    end = time.monotonic() + 10
    while not (out / "argv.json").exists() and time.monotonic() < end:
        time.sleep(0.02)
    sup.pidfile.write_text(f"{orphan.pid} {exe}\n")
    sup.cleanup_orphan()
    assert orphan.wait(timeout=5) == 0 and not sup.pidfile.exists()


def test_pidfile_of_unrelated_process_is_not_killed(make, tmp_path):
    sup, graph, got = make()
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    try:
        sup.pidfile.write_text(f"{other.pid} /opt/ros/x/lib/hole_detector/hole_detector\n")
        sup.cleanup_orphan()
        assert other.poll() is None                         # cmdline does not match: left alone
    finally:
        os.killpg(other.pid, signal.SIGKILL)
        other.wait()


def test_shutdown_leaves_no_process(make):
    sup, graph, got = make()
    sup.request(Mode.CSK_DEPTH, "a")
    got.wait(lambda s: s.ready)
    proc = sup.proc
    sup.shutdown()
    assert proc.poll() == 0


def test_restart_after_crash_waits_for_the_dead_node_to_leave_the_graph(make):
    sup, graph, got = make()
    sup.request(Mode.CSK_DEPTH, "a")
    got.wait(lambda s: s.ready)
    os.killpg(sup.proc.pid, signal.SIGKILL)
    got.wait(lambda s: not s.alive and s.error)
    graph.foreign = {"hole_detector": 1}                    # the killed node still visible to DDS ...
    threading.Timer(0.5, lambda: graph.foreign.clear()).start()   # ... until its lease expires
    sup.request(Mode.CSK_DEPTH, "a")
    st = got.wait(lambda s: s.ready)
    assert st.mode == "csk_depth" and not any("outside the UI" in (x.error or "") for x in got.items)


def test_ready_only_after_the_graph_stayed_ready_for_the_settle_time(make):
    """The synchronizer may not deliver to a subscription this backend already sees (Pi, Fast DDS: ~0.6 s of
    frames lost when the scan started at once)."""
    sup, graph, got = make(settle_s=1.0)

    def after(n, pred):                                      # a status published after the first n
        end = time.monotonic() + 10
        while time.monotonic() < end:
            with got.lock:
                hit = [st for st in got.items[n:] if pred(st)]
            if hit:
                return hit[0]
            time.sleep(0.02)
        raise AssertionError(f"no status matched; got {got.items}")

    graph.ready = False
    sup.request(Mode.CSK_DEPTH, "a")
    time.sleep(0.3)
    graph.ready = True
    shown = time.monotonic()
    after(0, lambda s: s.ready)
    assert time.monotonic() - shown >= 0.95, "ready before the settle time"
    n = len(got.items)
    graph.ready = False                                      # gone from the graph: not ready, settle again
    after(n, lambda s: s.alive and not s.ready)
    n = len(got.items)
    graph.ready = True
    back = time.monotonic()
    after(n, lambda s: s.ready)
    assert time.monotonic() - back >= 0.95
