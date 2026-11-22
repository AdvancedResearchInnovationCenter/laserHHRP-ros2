#!/usr/bin/env python3
"""Exports recorded scans in the legacy per-feature layout (~/data.legacy/data, the robot datasets): for every
target feature one folder with each laser line near it as an ASCII PCD, all lines in a JSON, and the merged cloud.

    export_legacy.py OUT [--db ~/.local/share/laser_hhrp/ui/ui.db] [--inspection INS-20261122-001]
                         [--scan SCAN_ID] [--half-width-mm 3.0] [--all-features]

Needs a sourced ROS workspace with laser_hhrp (to read frames.bin). Reads the operator UI's database (read-only)
for the scans, their raw recordings and their features; nothing on the device is changed.

OUT/<inspection>/Scan-<NNN>/
    scan.json                       operator, decision, counts, every feature with its measurement, export settings
    Hole-<id>/                      (Rivet-<id>/ in Rivet Flushness)
        Hole-<id>_<dy>mm.pcd        one laser line: x y z intensity, metres, y = 0, valid points only
        Hole-<id>_profiles.json     {"profile_001": {"dy": mm, "points": [{"intensity", "x", "y", "z"}], ...}}
        Hole-<id>_3d.pcd            every line above, y = dy / 1000
dy = the line's encoder position minus the feature's centre (mm), lines with |dy| <= half width, the full line
across. Unlike the robot's fixed steps the spacing follows the hand (0.05-0.5 mm); where the roller stood still
several lines share a dy: their files get _2, _3, ... Each JSON profile also carries the raw per-line record:
frame_index, time_s, encoder_count, position_mm, direction, accel_mps2, gyro_radps, imu_valid, flags. The laser's
intensity is not recorded: 1.0 (as in the legacy files).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from read_session import read_frames  # noqa: E402

PREFIX = {"csk_depth": "Hole", "rivet_flushness": "Rivet"}


def pcd_ascii(path: Path, cols: np.ndarray, fields: str) -> None:
    n = len(cols)
    k = len(fields.split())
    head = (f"VERSION 0.7\nFIELDS {fields}\nSIZE {' '.join(['4'] * k)}\nTYPE {' '.join(['F'] * k)}\n"
            f"COUNT {' '.join(['1'] * k)}\nWIDTH {n}\nHEIGHT 1\nVIEWPOINT 0.0 0.0 0.0 1.0 0.0 0.0 0.0\n"
            f"POINTS {n}\nDATA ascii\n")
    with open(path, "w") as f:
        f.write(head)
        np.savetxt(f, cols, fmt="%.10f")


def dy_name(dy: float) -> str:
    v = round(dy, 2)
    return repr(v + 0.0 if v != 0 else 0.0)        # legacy style: -3.0, -0.06, 0.0, 0.12


def load_frames(raw_dir: Path):
    """(position_mm, per-frame record, x, z) of every frame, valid points only."""
    out = []
    for f in read_frames(raw_dir):
        valid = np.asarray(f.valid, bool)
        x = np.asarray(f.x_m, np.float64)[valid]
        z = np.asarray(f.z_m, np.float64)[valid]
        rec = {"frame_index": int(f.index), "time_s": f.header.stamp.sec + f.header.stamp.nanosec * 1e-9,
               "encoder_count": int(f.encoder_count), "position_mm": f.position_m * 1e3, "direction": int(f.direction),
               "accel_mps2": [float(a) for a in f.accel_mps2], "gyro_radps": [float(g) for g in f.gyro_radps],
               "imu_valid": bool(f.imu_valid), "flags": int(f.flags)}
        out.append((f.position_m * 1e3, rec, x, z))
    return out


def export_feature(folder: Path, name: str, feature: dict, frames, half: float) -> int:
    near = [(pos - feature["y_mm"], rec, x, z) for pos, rec, x, z in frames if abs(pos - feature["y_mm"]) <= half]
    near.sort(key=lambda t: (t[0], t[1]["frame_index"]))
    folder.mkdir(parents=True, exist_ok=True)
    profiles, used, merged = {}, {}, []
    for i, (dy, rec, x, z) in enumerate(near, 1):
        base = f"{name}_{dy_name(dy)}mm"
        used[base] = used.get(base, 0) + 1
        fname = base + (f"_{used[base]}" if used[base] > 1 else "") + ".pcd"
        cols = np.column_stack((x, np.zeros_like(x), z, np.ones_like(x)))
        pcd_ascii(folder / fname, cols, "x y z intensity")
        merged.append(np.column_stack((x, np.full_like(x, dy / 1e3), z)))
        profiles[f"profile_{i:03d}"] = {
            "dy": round(dy, 4), "file": fname, **rec,
            "points": [{"intensity": 1.0, "x": float(a), "y": 0.0, "z": float(c)} for a, c in zip(x, z)]}
    with open(folder / f"{name}_profiles.json", "w") as f:
        json.dump(profiles, f, indent=4)
    if merged:
        pcd_ascii(folder / f"{name}_3d.pcd", np.vstack(merged), "x y z")
    return len(near)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("out")
    ap.add_argument("--db", default=str(Path.home() / ".local/share/laser_hhrp/ui/ui.db"))
    ap.add_argument("--inspection", help="label (INS-...) or uuid; default every inspection")
    ap.add_argument("--scan", help="one scan_id")
    ap.add_argument("--half-width-mm", type=float, default=3.0, help="lines within this of the centre (default 3.0)")
    ap.add_argument("--all-features", action="store_true", help="also non-target features (default: targets)")
    a = ap.parse_args()

    db = sqlite3.connect(f"file:{Path(a.db).expanduser()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    sql = ("SELECT s.*, i.label FROM scans s JOIN inspections i ON i.uuid = s.inspection_uuid "
           "WHERE s.raw_dir IS NOT NULL AND s.raw_dir != ''")
    args: list = []
    if a.inspection:
        sql += " AND (i.label = ? OR i.uuid = ?)"
        args += [a.inspection, a.inspection]
    if a.scan:
        sql += " AND s.scan_id = ?"
        args.append(a.scan)
    scans = db.execute(sql + " ORDER BY s.started_at", args).fetchall()
    if not scans:
        print("no scans with a raw recording found", file=sys.stderr)
        return 1
    out = Path(a.out).expanduser()
    for s in scans:
        raw = Path(s["raw_dir"])
        dest = out / (s["label"] or s["inspection_uuid"]) / f"Scan-{s['scan_number']:03d}"
        if not (raw / "frames.bin").is_file():
            print(f"{dest}: skipped, no frames.bin in {raw}")
            continue
        feats = [dict(r) for r in db.execute("SELECT * FROM features WHERE scan_id = ? ORDER BY y_mm", (s["scan_id"],))]
        frames = load_frames(raw)
        prefix = PREFIX.get(s["mode"], "Feature")
        meta = {k: s[k] for k in ("scan_id", "scan_number", "operator_id", "mode", "expected_count",
                                  "detected_target_count", "analysis", "decision", "decided_by", "decided_at",
                                  "started_at", "ended_at", "clock_quality", "raw_dir", "raw_frame_count")}
        meta.update(inspection=s["label"], inspection_uuid=s["inspection_uuid"], half_width_mm=a.half_width_mm,
                    frames=len(frames), features=[])
        lines = 0
        for f in feats:
            entry = {k: f[k] for k in ("feature_id", "kind", "is_target", "x_mm", "y_mm", "z_mm", "diameter_mm", "partial")}
            entry.update(quality=json.loads(f["quality_json"]), measurement=json.loads(f["measurement_json"]))
            if f["is_target"] or a.all_features:
                name = f"{prefix}-{f['feature_id']}"
                entry["folder"] = name
                entry["lines"] = export_feature(dest / name, name, f, frames, a.half_width_mm)
                lines += entry["lines"]
            meta["features"].append(entry)
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "scan.json").write_text(json.dumps(meta, indent=2))
        print(f"{dest}: {sum('folder' in e for e in meta['features'])} features, {lines} lines "
              f"({s['operator_id']}, {s['decision'] or 'undecided'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
