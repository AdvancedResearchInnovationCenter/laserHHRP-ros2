"""Default recording for experiments and ML: after every scan, laser_hhrp's export_legacy.py writes the scan's
features in the legacy per-feature layout (Hole-<id>/ or Rivet-<id>/: one ASCII PCD per laser line, a profiles.json
with each line's encoder / IMU record, the merged _3d.pcd; scan.json with operator, decision and measurements) into
the scan's raw recording folder (~/laser_hhrp/scans/<date-time>/), next to frames.bin.

One job at a time, in its own process at the lowest CPU and disk priority (nice 19, ionice idle), so the next
scan's acquisition and detection never wait for it. Failures are logged and never touch the scan's lifecycle.
"""

from __future__ import annotations

import logging
import queue
import shutil
import subprocess
import sys
import threading
from pathlib import Path

log = logging.getLogger(__name__)


def find_script() -> Path | None:
    """export_legacy.py as installed with laser_hhrp, or None."""
    try:
        from ament_index_python.packages import get_package_prefix
        path = Path(get_package_prefix("laser_hhrp")) / "lib" / "laser_hhrp" / "export_legacy.py"
    except Exception:      # noqa: BLE001 - package not found / no ament index
        return None
    return path if path.is_file() else None


class RecordingExport:
    def __init__(self, db: str | Path, script: Path, log_file: Path | None = None):
        self.db, self.script, self.log_file = str(db), script, log_file
        self.jobs: queue.Queue = queue.Queue()
        self.prio = ["nice", "-n", "19"] + (["ionice", "-c", "3"] if shutil.which("ionice") else [])
        threading.Thread(target=self._run, name="recording-export", daemon=True).start()

    def export(self, scan: dict, meta_only: bool = False) -> None:
        if scan.get("raw_dir"):
            self.jobs.put((scan["scan_id"], scan["raw_dir"], meta_only))

    def command(self, scan_id: str, raw_dir: str, meta_only: bool) -> list[str]:
        cmd = self.prio + [sys.executable, str(self.script), raw_dir, "--db", self.db, "--scan", scan_id, "--flat"]
        return cmd + (["--meta-only"] if meta_only else [])

    def _run(self) -> None:
        while True:
            scan_id, raw_dir, meta_only = self.jobs.get()
            try:
                out = open(self.log_file, "a") if self.log_file else subprocess.DEVNULL
                try:
                    code = subprocess.call(self.command(scan_id, raw_dir, meta_only), stdout=out,
                                           stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
                finally:
                    if self.log_file:
                        out.close()
                if code:
                    log.warning("recording export of scan %s exited %d", scan_id, code)
            except Exception:      # noqa: BLE001 - never let the worker die
                log.exception("recording export of scan %s failed", scan_id)
