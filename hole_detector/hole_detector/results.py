"""What is saved and reported: summary.csv, boundary PCDs and the /inspection/result message."""

import csv
import json
import time

import numpy as np

CSV_COLUMNS = ["id", "cx_mm", "cy_mm", "cz_mm", "top_diam_mm", "fit_rms_mm", "ring_coverage", "countersunk",
               "partial", "scanned_share", "dist_to_prev_mm"]


class ScanFolder:
    """<out_root>/scan_<time>/: summary.csv plus hole_NN.pcd. Made at the scan's first hole."""

    def __init__(self, out_root, log):
        self.root, self.log, self.path = out_root, log, None

    def new_scan(self):
        self.path = None

    def save(self, hole, previous):
        """Append the hole's row and write its raw boundary points. `previous` is the hole before it, or None."""
        if self.path is None:
            self.path = self.root / time.strftime("scan_%Y%m%d_%H%M%S")
            self.path.mkdir(parents=True, exist_ok=True)
            with (self.path / "summary.csv").open("w", newline="") as handle:
                csv.writer(handle).writerow(CSV_COLUMNS)
            self.log("saving results to %s" % self.path)
        with (self.path / "summary.csv").open("a", newline="") as handle:
            csv.writer(handle).writerow([
                hole.id, *("%.3f" % v for v in hole.centre * 1000), "%.3f" % (2000 * hole.radius),
                "%.3f" % (hole.rms * 1000), "%.2f" % hole.coverage, hole.countersunk, hole.partial,
                "%.2f" % hole.scanned_share,
                "" if previous is None else "%.3f" % (np.linalg.norm(hole.centre - previous.centre) * 1000)])
        write_pcd(self.path / ("hole_%02d.pcd" % hole.id), hole.boundary)


def write_pcd(path, points):
    """Binary xyz float32 PCD (same format as the scans, opens in Open3D / CloudCompare)."""
    points = np.asarray(points, dtype=np.float32)
    header = ("# .PCD v0.7\nVERSION 0.7\nFIELDS x y z\nSIZE 4 4 4\nTYPE F F F\nCOUNT 1 1 1\n"
              "WIDTH %d\nHEIGHT 1\nVIEWPOINT 0 0 0 1 0 0 0\nPOINTS %d\nDATA binary\n" % (len(points), len(points)))
    path.write_bytes(header.encode() + points.tobytes())


def inspection_result(msg_type, header, hole):
    """The laser_hhrp InspectionResult for one hole. recorder_node streams these floats into
    results.json, where NaN would break the JSON: -1 means "not measured" (depth, flushness)."""
    d_top = 2000 * hole.radius
    return msg_type(
        header=header, feature_type="countersink" if hole.countersunk else "hole",
        position_m=hole.centre.tolist(),
        countersink_depth_m=-1.0, flushness_m=-1.0, surface_deviation_m=hole.rms, passed=True,
        detail=json.dumps({"id": hole.id, "d_top_mm": round(d_top, 3), "rms_mm": round(hole.rms * 1000, 3),
                           "ring_coverage": round(hole.coverage, 2), "countersunk": hole.countersunk,
                           "partial": hole.partial, "scanned_share": round(hole.scanned_share, 2)}))
