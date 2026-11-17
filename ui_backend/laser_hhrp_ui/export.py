"""Inspection CSV export (plan sections 7, 8).

- `inspection_csv()` builds the file in memory: the GET download has no server-side disk side effect.
- `UsbExporter` writes it to a mounted removable drive, only below allowlisted mount roots, never
  overwriting a file, and only on an explicit POST. The engine audits every attempt.

One row per detected feature; a scan without features still gets one row (feature columns empty), so
every scan of the inspection, including rescans and failed scans, appears. Measurements are exported
as recorded: v0.1 has no validated metrology, so `measurement_status` is `not_measured` and the value
is empty. There is no pass/fail column.
"""

from __future__ import annotations

import csv
import io
import os
import re
from dataclasses import dataclass
from pathlib import Path

COLUMNS = [
    "inspection_label", "inspection_uuid", "mode", "scan_number", "scan_id", "scan_status", "decision",
    "decided_by", "decided_at", "operator_id", "started_at", "ended_at", "clock_quality", "expected_count",
    "detected_target_count", "count_mismatch", "analysis", "analysis_error", "frame_count",
    "feature_id", "kind", "is_target", "x_mm", "y_mm", "z_mm", "diameter_mm", "partial", "coverage",
    "scanned_share", "rms_mm", "measurement_name", "measurement_status", "measurement_value_mm",
]

DEFAULT_USB_ROOTS = ("/media", "/run/media")
SAFE_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _text(value) -> str:
    """Free text: never let a spreadsheet read it as a formula."""
    s = "" if value is None else str(value)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def _num(value) -> str:
    return "" if value is None else repr(value) if isinstance(value, float) else str(value)


def _bool(value) -> str:
    return "" if value is None else "true" if value else "false"


def inspection_csv(engine, inspection_uuid: str) -> tuple[str, str]:
    """(file name, CSV text) for one inspection. Raises CommandError 404 for an unknown inspection."""
    from .state import CommandError

    insp = engine.repo.inspection(inspection_uuid)
    if not insp:
        raise CommandError(404, "not_found", "No such inspection")
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\r\n")
    w.writerow(COLUMNS)
    for s in engine.repo.scans_of(inspection_uuid):
        v = engine.scan_view(s)
        scan_cols = [
            _text(insp["label"]), insp["uuid"], insp["mode"], _num(v["scan_number"]), v["scan_id"], v["status"],
            v["decision"] or "", _text(v["decided_by"]), v["decided_at"] or "", _text(v["operator_id"]),
            v["started_at"] or "", v["ended_at"] or "", v["clock_quality"], _num(v["expected_count"]),
            _num(v["detected_target_count"]), _bool(v["count_mismatch"]), v["analysis"],
            _text(v["analysis_error"]), _num(v["frame_count"]),
        ]
        features = engine.repo.features_of(s["scan_id"])
        if not features:
            w.writerow(scan_cols + [""] * (len(COLUMNS) - len(scan_cols)))
        for f in features:
            q, m = f["quality"], f["measurement"]
            w.writerow(scan_cols + [
                _num(f["feature_id"]), f["kind"], _bool(f["is_target"]), _num(f["x_mm"]), _num(f["y_mm"]),
                _num(f["z_mm"]), _num(f["diameter_mm"]), _bool(f["partial"]), _num(q.get("coverage")),
                _num(q.get("scanned_share")), _num(q.get("rms_mm")), m["name"], m["status"],
                _num(m["value_mm"]),
            ])
    return f"{insp['label']}.csv", out.getvalue()


class ExportError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


@dataclass
class UsbTarget:
    path: str
    name: str
    writable: bool
    free_mb: float | None
    reason: str

    def view(self) -> dict:
        return {"path": self.path, "name": self.name, "writable": self.writable, "free_mb": self.free_mb,
                "reason": self.reason}


class UsbExporter:
    """Writes export files to removable drives mounted below allowlisted roots (udisks mounts USB
    sticks at /media/<user>/<label> or /run/media/<user>/<label>). `is_mount` is injectable for tests."""

    def __init__(self, roots=DEFAULT_USB_ROOTS, is_mount=os.path.ismount):
        self.roots = [Path(r).resolve() for r in roots]
        self.is_mount = is_mount

    def _candidates(self):
        for root in self.roots:
            if not root.is_dir():
                continue
            # /media/<label> and /media/<user>/<label>
            for depth1 in sorted(p for p in root.iterdir() if p.is_dir()):
                if self.is_mount(str(depth1)):
                    yield depth1
                    continue
                try:
                    for depth2 in sorted(p for p in depth1.iterdir() if p.is_dir()):
                        if self.is_mount(str(depth2)):
                            yield depth2
                except OSError:
                    continue

    @staticmethod
    def _inspect(path: Path) -> UsbTarget:
        try:
            st = os.statvfs(path)
        except OSError as e:
            return UsbTarget(str(path), path.name, False, None, f"not readable: {e.strerror}")
        free = st.f_bavail * st.f_frsize / 1e6
        if st.f_flag & os.ST_RDONLY:
            return UsbTarget(str(path), path.name, False, free, "mounted read-only")
        if not os.access(path, os.W_OK):
            return UsbTarget(str(path), path.name, False, free, "no write permission")
        return UsbTarget(str(path), path.name, True, round(free, 1), "")

    def targets(self) -> list[UsbTarget]:
        return [self._inspect(p) for p in self._candidates()]

    def _allowed(self, raw: str) -> Path:
        if not isinstance(raw, str) or not raw:
            raise ExportError(400, "invalid", "mount must be the path of a listed USB drive")
        path = Path(raw).resolve()
        if not any(path != root and root in path.parents for root in self.roots):
            raise ExportError(403, "not_allowed", "Not an approved USB location")
        if not self.is_mount(str(path)):
            raise ExportError(409, "not_mounted", "The USB drive is not mounted")
        return path

    def write(self, mount: str, base_name: str, data: str) -> str:
        """Write `data` as <base_name>.csv (or <base_name>-2.csv ...) on the drive; returns the path.
        Never overwrites: the name is claimed with O_EXCL, the content goes through a temporary file
        and is fsynced before the rename."""
        if not SAFE_NAME.match(base_name):
            raise ExportError(400, "invalid", "unsafe file name")
        target = self._inspect(self._allowed(mount))
        if not target.writable:
            raise ExportError(409, "not_writable", f"USB drive {target.name}: {target.reason}")
        payload = data.encode("utf-8")
        if target.free_mb is not None and target.free_mb * 1e6 < len(payload) + 1_000_000:
            raise ExportError(507, "no_space", f"USB drive {target.name} is full")
        folder = Path(target.path)
        for n in range(1, 1000):
            final = folder / (f"{base_name}.csv" if n == 1 else f"{base_name}-{n}.csv")
            try:
                fd = os.open(final, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            except FileExistsError:
                continue
            os.close(fd)
            tmp = folder / f".{final.name}.part"
            try:
                with open(tmp, "wb") as f:
                    f.write(payload)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, final)
                dfd = os.open(folder, os.O_RDONLY)
                try:
                    os.fsync(dfd)
                except OSError:
                    pass                      # some file systems (vfat) refuse a directory fsync
                finally:
                    os.close(dfd)
            except OSError as e:
                for p in (tmp, final):
                    try:
                        p.unlink()
                    except OSError:
                        pass
                raise ExportError(500, "write_failed", f"Writing to {target.name} failed: {e.strerror}") from e
            return str(final)
        raise ExportError(409, "name_collision", "Too many exports with this name on the drive")
