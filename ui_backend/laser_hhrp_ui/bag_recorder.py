"""One `ros2 bag record -a` per scan, from the screens' "Record bags" switch (state.Engine._bag_sync decides when).

The bag runs in its own process group, started through bash after sourcing the ROS and colcon workspace setup
files (the workspace's own message types, e.g. laser_hhrp/msg/ScanFrame, must be known to record them). Stopped
with SIGINT so rosbag2 closes the file cleanly (SIGTERM, then SIGKILL, if it does not exit). A monitor thread
measures the bag's size and the free space: below `min_free_bytes` it stops the bag and reports why, so the
scans' own recording (frames.bin) never runs out of space.

Bags are played back with `ros2 bag play --read-ahead-queue-size 12000 <bag>` (large -a bags stall with the
default queue).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

READY_MARKERS = ("Recording", "Subscribed to topic", "Listening for topics")


def _die_with_parent() -> None:
    """In the child before exec: SIGINT (a clean rosbag2 close) when the backend's thread that started it dies,
    even if the backend is killed hard."""
    try:
        import ctypes
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGINT)     # PR_SET_PDEATHSIG
    except Exception:      # noqa: BLE001 - not Linux: the explicit shutdown still closes it
        pass


def safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "bag"


def setup_files() -> list[str]:
    """The ROS distro's setup.bash and the colcon workspace holding laser_hhrp (its install/setup.bash)."""
    files = []
    distro = os.environ.get("ROS_DISTRO")
    if distro and Path(f"/opt/ros/{distro}/setup.bash").is_file():
        files.append(f"/opt/ros/{distro}/setup.bash")
    try:
        from ament_index_python.packages import get_package_prefix
        ws = Path(get_package_prefix("laser_hhrp")).parent / "setup.bash"
        if ws.is_file():
            files.append(str(ws))
    except Exception:      # noqa: BLE001 - no ament index: rely on the inherited environment
        pass
    return files


class NullBag:
    """No bag recording (tests without a fake, --no-ros)."""
    available = False

    def status(self) -> dict:
        return {"state": "idle", "name": None, "path": None, "bytes": 0, "error": None}

    def free_bytes(self) -> int | None:
        return None

    def start(self, name: str) -> None:
        raise RuntimeError("bag recording is not available")

    def stop(self) -> None:
        pass


class BagRecorder:
    available = True

    def __init__(self, root: str | Path, min_free_bytes: float = 5e9, sources: list[str] | None = None,
                 log_file: Path | None = None, command: list[str] | None = None):
        self.root = Path(root).expanduser()
        self.min_free = min_free_bytes
        self.sources = setup_files() if sources is None else sources
        self.log_file = log_file
        self.command = command          # tests: replaces `ros2 bag record -a -o <path>`
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.state, self.name, self.path, self.bytes, self.error = "idle", None, None, 0, None
        self.closing = False            # the backend is stopping: no new bag
        threading.Thread(target=self._monitor, name="bag-monitor", daemon=True).start()

    # ---- queries (engine thread) ----
    def status(self) -> dict:
        with self.lock:
            return {"state": self.state, "name": self.name, "path": str(self.path) if self.path else None,
                    "bytes": self.bytes, "error": self.error}

    def free_bytes(self) -> int | None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            return shutil.disk_usage(self.root).free
        except OSError:
            return None

    # ---- commands (engine thread; never block) ----
    def start(self, name: str) -> None:
        with self.lock:
            if self.state != "idle":
                raise RuntimeError(f"a bag is {self.state}")
            if self.closing:
                raise RuntimeError("the software is stopping")
            free = self.free_bytes()
            if free is not None and free < self.min_free:
                raise RuntimeError(f"less than {self.min_free / 1e9:.0f} GB free")
            self.root.mkdir(parents=True, exist_ok=True)
            base = safe_name(name)
            path, n = self.root / base, 1
            while path.exists():            # never overwrite or delete an earlier bag
                n += 1
                path = self.root / f"{base}_{n}"
            cmd = self.command + [str(path)] if self.command else ["ros2", "bag", "record", "-a", "-o", str(path)]
            script = 'set +u; for f in "$@"; do source "$f" > /dev/null; done; exec ' + " ".join(
                f"'{c}'" for c in cmd)
            self.proc = subprocess.Popen(["bash", "-c", script, "bag"] + self.sources, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True,
                                         text=True, bufsize=1, preexec_fn=_die_with_parent)
            self.state, self.name, self.path, self.bytes, self.error = "starting", path.name, path, 0, None
            threading.Thread(target=self._read_output, args=(self.proc,), daemon=True).start()
        log.info("bag %s: started", path)

    def stop(self) -> None:
        with self.lock:
            proc = self.proc
            if proc is None or self.state in ("idle", "stopping"):
                return
            self.state = "stopping"
        threading.Thread(target=self._stop, args=(proc,), daemon=True).start()

    def shutdown(self, timeout: float = 25.0) -> None:
        """The backend is stopping: close the running bag and wait until it is written (metadata included)."""
        with self.lock:
            self.closing = True
            proc = self.proc
        if proc is None:
            return
        self.stop()
        try:
            proc.wait(timeout)
        except subprocess.TimeoutExpired:
            log.warning("bag still open at shutdown")
        end = time.monotonic() + 5
        while self.status()["state"] != "idle" and time.monotonic() < end:
            time.sleep(0.05)

    # ---- workers ----
    def _stop(self, proc: subprocess.Popen) -> None:
        for sig, wait in ((signal.SIGINT, 15), (signal.SIGTERM, 5), (signal.SIGKILL, 5)):
            try:
                os.killpg(proc.pid, sig)
            except ProcessLookupError:
                break
            try:
                proc.wait(wait)
                break
            except subprocess.TimeoutExpired:
                log.warning("bag did not stop on %s", sig.name)
        with self.lock:
            if self.proc is proc:
                self.bytes = self._size()
                self.proc, self.state = None, "idle"
        log.info("bag %s: closed", self.path)

    def _read_output(self, proc: subprocess.Popen) -> None:
        out = open(self.log_file, "a") if self.log_file else None
        try:
            for line in proc.stdout:
                if out:
                    out.write(line)
                    out.flush()
                if any(m in line for m in READY_MARKERS):
                    with self.lock:
                        if self.proc is proc and self.state == "starting":
                            self.state = "recording"
        finally:
            if out:
                out.close()

    def _size(self) -> int:
        try:
            return sum(f.stat().st_size for f in self.path.rglob("*") if f.is_file()) if self.path else 0
        except OSError:
            return self.bytes

    def _monitor(self) -> None:
        while True:
            time.sleep(1.0)
            with self.lock:
                proc, state = self.proc, self.state
            if proc is None:
                continue
            size = self._size()
            code = proc.poll()
            with self.lock:
                if self.proc is not proc:
                    continue
                self.bytes = size
                if code is not None and state in ("starting", "recording"):
                    self.error = f"ros2 bag exited ({code}) - see the bag log"
                    self.proc, self.state = None, "idle"
                    continue
            free = self.free_bytes()
            if free is not None and free < self.min_free and state in ("starting", "recording"):
                with self.lock:
                    self.error = f"stopped: less than {self.min_free / 1e9:.0f} GB free"
                self.stop()


class FakeBag:
    """The mock source's and the tests' recorder: records nothing, follows the same states (recording at once)."""
    available = True

    def __init__(self, free_bytes: float = 30e9, min_free_bytes: float = 5e9):
        self.min_free, self.free = min_free_bytes, free_bytes
        self.state, self.name, self.bytes, self.error = "idle", None, 0, None
        self.started: list[str] = []

    def status(self) -> dict:
        if self.state == "recording":
            self.bytes += 7_000_000            # ~ a few MB per status, like a real -a bag
        return {"state": self.state, "name": self.name, "path": None, "bytes": self.bytes, "error": self.error}

    def free_bytes(self) -> float:
        return self.free

    def start(self, name: str) -> None:
        if self.state != "idle":
            raise RuntimeError(f"a bag is {self.state}")
        if self.free < self.min_free:
            raise RuntimeError(f"less than {self.min_free / 1e9:.0f} GB free")
        self.state, self.name, self.bytes, self.error = "recording", safe_name(name), 0, None
        self.started.append(self.name)

    def stop(self) -> None:
        self.state = "idle"
