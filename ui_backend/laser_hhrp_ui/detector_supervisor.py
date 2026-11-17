"""Exactly one allowlisted detector process per inspection (plan section 6).

- Allowlist: CSK Depth -> hole_detector, Rivet Flushness -> rivet_detector; fixed argv, no shell, no
  user text in arguments (only the inspection UUID in the output path).
- Each package's own config/params.yaml, as detectors.launch.py uses, plus out_dir under the UI data root.
- Tagged mode (tagged:=true): per-scan identity, a final pass on scan close, DetectionFinalized.
- Ready = process alive AND its ROS node is the only one of that name AND it subscribes to /scan/frame_tagged
  AND publishes /inspection/result_tagged. A PID alone is never "ready".
- A detector node we did not start (e.g. the core launched with holes:=true) or the other mode's
  detector makes the state NOT READY with the reason; the supervisor never starts a duplicate.
- Crash -> alive False with the exit code; restart only on explicit request.
- Stop: SIGINT (detectors flush their result files on SIGINT), then SIGTERM, then SIGKILL.
- A pidfile lets a restarted backend stop a detector orphaned by a backend crash.

All process work runs on one worker thread (serialized); status goes out through `submit` (thread-safe).
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .events import DetectorStatus
from .models import DETECTOR, Mode

log = logging.getLogger("laser_hhrp_ui.supervisor")
FRAME_TOPIC = "/scan/frame_tagged"            # tagged mode (Phase 3): scan identity in ...
RESULT_TOPIC = "/inspection/result_tagged"    # ... tagged results and DetectionFinalized out


def resolve_detector(package: str) -> tuple[str, str]:
    """(executable, params.yaml) of an installed detector package."""
    from ament_index_python.packages import get_package_prefix, get_package_share_directory
    exe = Path(get_package_prefix(package)) / "lib" / package / package
    params = Path(get_package_share_directory(package)) / "config" / "params.yaml"
    if not exe.is_file() or not params.is_file():
        raise FileNotFoundError(f"{package}: {exe} or {params} missing")
    return str(exe), str(params)


def _running(pid: int) -> bool:
    """True while the process exists and is not a zombie (an exited child not yet reaped)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat.rsplit(")", 1)[-1].split()[0] not in ("Z", "X")


class RosGraph:
    """Graph queries through the bridge's rclpy node (a getter: the node exists once the bridge started)."""

    def __init__(self, node_getter):
        self._get = node_getter

    @property
    def node(self):
        node = self._get()
        if node is None:
            raise RuntimeError("ROS bridge not started")
        return node

    def node_count(self, name: str) -> int:
        return sum(1 for n, _ in self.node.get_node_names_and_namespaces() if n == name)

    def subscribes(self, name: str, topic: str) -> bool:
        return any(i.node_name == name for i in self.node.get_subscriptions_info_by_topic(topic))

    def publishes(self, name: str, topic: str) -> bool:
        return any(i.node_name == name for i in self.node.get_publishers_info_by_topic(topic))


class DetectorSupervisor:
    def __init__(self, submit, graph, out_root: str | Path, log_dir: str | Path, resolve=resolve_detector,
                 start_timeout_s: float = 45.0, stop_timeout_s: float = 15.0, poll_s: float = 0.25,
                 health_period_s: float = 2.0):
        self.submit = submit
        self.graph = graph
        self.out_root, self.log_dir = Path(out_root), Path(log_dir)
        self.resolve = resolve
        self.start_timeout_s, self.stop_timeout_s = start_timeout_s, stop_timeout_s
        self.poll_s, self.health_period_s = poll_s, health_period_s
        self.pidfile = self.log_dir / "detector.pid"
        self.proc: subprocess.Popen | None = None
        self.mode: Mode | None = None
        self.out_dir: Path | None = None       # output folder of the running detector
        self._crashed: tuple[str, float] | None = None  # (node name, time): its node may linger in the graph
        self.stale_graph_s = 20.0              # DDS drops a killed node after its lease (CycloneDDS ~10 s)
        self.generation = 0                    # bumps on every start/stop: stale watchers exit
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="detector-supervisor")
        self._status = DetectorStatus(mode=None, alive=False, ready=False)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.out_root.mkdir(parents=True, exist_ok=True)

    # ---- DetectorControl interface (called on the loop thread; never blocks it) ----
    def request(self, mode: Mode, inspection_uuid: str | None = None) -> None:
        self._worker.submit(self._guard, self._start, Mode(mode), inspection_uuid or "no-inspection")

    def stop(self) -> None:
        self._worker.submit(self._guard, self._stop)

    def replay(self, scan: dict) -> None:
        pass                                   # Phase 3

    def shutdown(self, timeout: float = 30.0) -> None:
        """Server exit: stop the detector and wait for it (no orphans)."""
        self._worker.submit(self._guard, self._stop).result(timeout)
        self._worker.shutdown(wait=True)

    def cleanup_orphan(self) -> None:
        """A detector left running by a crashed backend (pidfile) is stopped before anything starts."""
        self._worker.submit(self._guard, self._cleanup_orphan).result(self.stop_timeout_s + 10)

    # ---- worker-thread implementation ----
    def _guard(self, fn, *args):
        try:
            fn(*args)
        except Exception as e:  # report, never kill the worker
            log.exception("supervisor %s failed", fn.__name__)
            self._publish(DetectorStatus(mode=self.mode.value if self.mode else None,
                                         alive=self._alive(), ready=False, error=f"supervisor error: {e}"))

    def _publish(self, st: DetectorStatus) -> None:
        self._status = st
        self.submit(st)

    def _alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _start(self, mode: Mode, inspection_uuid: str) -> None:
        name = DETECTOR[mode]
        if self._alive() and self.mode == mode:
            self._publish(self._status)        # already running for this mode: re-announce
            return
        if self._alive():
            self._stop()                       # never two detectors
        other = DETECTOR[Mode.RIVET_FLUSHNESS if mode == Mode.CSK_DEPTH else Mode.CSK_DEPTH]
        self._wait_for_stale_node()
        for n in (name, other):
            if self.graph.node_count(n):
                self.mode = mode
                self._publish(DetectorStatus(mode=mode.value, alive=False, ready=False,
                                             error=f"a {n} node is already running outside the UI "
                                                   f"(start the core with holes:=false)"))
                return
        exe, params = self.resolve(name)
        out_dir = self.out_root / inspection_uuid
        out_dir.mkdir(parents=True, exist_ok=True)
        cmd = [exe, "--ros-args", "--params-file", params, "-p", f"out_dir:={out_dir}", "-p", "tagged:=true"]
        logf = open(self.log_dir / f"{name}.log", "ab")
        logf.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} start {cmd}\n".encode())
        logf.flush()
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=logf, stderr=subprocess.STDOUT,
                                     start_new_session=True, close_fds=True)
        logf.close()
        self.mode, self.out_dir = mode, out_dir
        self.generation += 1
        self.pidfile.write_text(f"{self.proc.pid} {exe}\n")
        log.info("started %s pid %d out_dir %s", name, self.proc.pid, out_dir)
        self._publish(DetectorStatus(mode=mode.value, alive=True, ready=False))
        threading.Thread(target=self._watch, args=(self.proc, mode, self.generation), daemon=True,
                         name=f"watch-{name}").start()

    def _watch(self, proc: subprocess.Popen, mode: Mode, generation: int) -> None:
        """Readiness, then health, until this process exits or is replaced."""
        name = DETECTOR[mode]
        other = DETECTOR[Mode.RIVET_FLUSHNESS if mode == Mode.CSK_DEPTH else Mode.CSK_DEPTH]
        t0, ready, last = time.monotonic(), False, None
        while proc.poll() is None and generation == self.generation:
            try:
                count = self.graph.node_count(name)
                ok = (count == 1 and self.graph.subscribes(name, FRAME_TOPIC)
                      and self.graph.publishes(name, RESULT_TOPIC))
                error = ""
                if count > 1:
                    error = f"{count} {name} nodes running (one started outside the UI?)"
                elif self.graph.node_count(other):
                    error = f"{other} is also running: only one detector may run"
                elif not ok and time.monotonic() - t0 > self.start_timeout_s:
                    error = f"{name} not ready after {self.start_timeout_s:.0f} s (see {self.log_dir / (name + '.log')})"
            except Exception as e:  # graph query failed: not ready, keep watching
                ok, error = False, f"ROS graph query failed: {e}"
            state = (ok and not error, error)
            if state != last and generation == self.generation:
                last = state
                ready = state[0]
                self._publish(DetectorStatus(mode=mode.value, alive=True, ready=ready, error=error))
            time.sleep(self.health_period_s if ready else self.poll_s)
        rc = proc.wait()
        if generation == self.generation:      # not a requested stop: a crash
            log.error("%s exited unexpectedly with code %s", name, rc)
            self._crashed = (name, time.monotonic())
            self.pidfile.unlink(missing_ok=True)
            self._publish(DetectorStatus(mode=mode.value, alive=False, ready=False,
                                         error=f"{name} exited with code {rc}"))

    def _wait_for_stale_node(self) -> None:
        """A detector killed without a clean shutdown stays in the ROS graph until DDS times it out. It was
        ours, so wait for it to disappear instead of reporting it as a foreign detector."""
        if self._crashed is None:
            return
        name, t_crash = self._crashed
        self._crashed = None
        end = t_crash + self.stale_graph_s
        while time.monotonic() < end and self.graph.node_count(name):
            time.sleep(self.poll_s)

    def _terminate(self, proc: subprocess.Popen) -> int:
        for sig, wait in ((signal.SIGINT, self.stop_timeout_s), (signal.SIGTERM, 5.0), (signal.SIGKILL, 5.0)):
            try:
                os.killpg(proc.pid, sig)
            except ProcessLookupError:
                break
            try:
                return proc.wait(timeout=wait)
            except subprocess.TimeoutExpired:
                log.warning("detector pid %d ignored %s", proc.pid, sig.name)
        return proc.wait()

    def _stop(self) -> None:
        proc, self.proc = self.proc, None
        self.generation += 1                   # the watcher of this process now ignores its exit
        if proc is not None and proc.poll() is None:
            rc = self._terminate(proc)
            log.info("stopped %s (exit %s)", DETECTOR[self.mode] if self.mode else "detector", rc)
        self.pidfile.unlink(missing_ok=True)
        self.mode = None
        self._publish(DetectorStatus(mode=None, alive=False, ready=False))

    def _cleanup_orphan(self) -> None:
        try:
            pid_text, exe = self.pidfile.read_text().split(maxsplit=1)
            pid, exe = int(pid_text), exe.strip()
        except (OSError, ValueError):
            return
        allowed = Path(exe).name in DETECTOR.values()
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        except OSError:
            cmdline = []
        if allowed and cmdline and any(exe.encode() == part for part in cmdline):
            log.warning("stopping orphaned detector pid %d (%s)", pid, exe)
            for sig, wait in ((signal.SIGINT, self.stop_timeout_s), (signal.SIGKILL, 5.0)):
                try:
                    os.killpg(pid, sig)
                except ProcessLookupError:
                    break
                end = time.monotonic() + wait
                while time.monotonic() < end and _running(pid):
                    time.sleep(0.1)
                if not _running(pid):
                    break
        self.pidfile.unlink(missing_ok=True)
