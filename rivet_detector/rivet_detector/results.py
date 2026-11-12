"""What is saved and reported for rivets: summary.csv, boundary PCDs and the /inspection/result message.
A copy of the hole results module, kept separate so the hole detector can change without moving this."""

import csv
import queue
import threading
import json
import time

import numpy as np

CSV_COLUMNS = ["id", "cx_mm", "cy_mm", "cz_mm", "head_diam_mm", "head_height_mm", "fit_rms_mm", "edge_coverage",
               "partial", "scanned_share", "dist_to_nearest_mm"]


class ScanFolder:
    """<out_root>/rivet_scan_<time>/: summary.csv plus rivet_NN.pcd. Made at the scan's first rivet.

    Rows and PCDs are written by a worker thread in save order, so detection never waits for the disk;
    close() waits until everything queued is written."""

    def __init__(self, out_root, log):
        self.root, self.log, self.path = out_root, log, None
        self.jobs = queue.Queue(maxsize=64)        # full: save() waits (the disk is far behind)
        threading.Thread(target=self.work, daemon=True).start()

    def work(self):
        while True:
            path, row, name, points = self.jobs.get()
            try:
                with (path / "summary.csv").open("a", newline="") as handle:
                    csv.writer(handle).writerow(row)
                write_pcd(path / name, points)
            except OSError as error:
                self.log("could not save %s: %s" % (name, error))
            finally:
                self.jobs.task_done()

    def close(self):
        self.jobs.join()

    def new_scan(self):
        self.path = None

    def save(self, rivet, nearest):
        """Append the rivet's row and write its raw boundary points. `nearest` is the closest earlier rivet, or None."""
        if self.path is None:
            base = self.root / time.strftime("rivet_scan_%Y%m%d_%H%M%S")
            self.root.mkdir(parents=True, exist_ok=True)
            self.path, n = base, 1
            while True:                     # two scans in one second must not share (overwrite) a folder
                try:
                    self.path.mkdir()
                    break
                except FileExistsError:
                    n += 1
                    self.path = base.with_name("%s_%d" % (base.name, n))
            with (self.path / "summary.csv").open("w", newline="") as handle:
                csv.writer(handle).writerow(CSV_COLUMNS)
            self.log("saving results to %s" % self.path)
        row = [rivet.id, *("%.3f" % v for v in rivet.centre * 1000), "%.3f" % (2000 * rivet.radius),
               "%.3f" % (rivet.height * 1000), "%.3f" % (rivet.rms * 1000), "%.2f" % rivet.coverage, rivet.partial,
               "%.2f" % rivet.scanned_share,
               "" if nearest is None else "%.3f" % (np.linalg.norm(rivet.centre - nearest.centre) * 1000)]
        self.jobs.put((self.path, row, "rivet_%02d.pcd" % rivet.id, np.array(rivet.boundary)))


def write_pcd(path, points):
    """Binary xyz float32 PCD (same format as the scans, opens in Open3D / CloudCompare)."""
    points = np.asarray(points, dtype=np.float32)
    header = ("# .PCD v0.7\nVERSION 0.7\nFIELDS x y z\nSIZE 4 4 4\nTYPE F F F\nCOUNT 1 1 1\n"
              "WIDTH %d\nHEIGHT 1\nVIEWPOINT 0 0 0 1 0 0 0\nPOINTS %d\nDATA binary\n" % (len(points), len(points)))
    path.write_bytes(header.encode() + points.tobytes())


def inspection_result(msg_type, header, rivet):
    """The laser_hhrp InspectionResult for one rivet. recorder_node streams these floats into
    results.json, where NaN would break the JSON: -1 means "not measured" (countersink depth, flushness)."""
    return msg_type(
        header=header, feature_type="rivet", position_m=rivet.centre.tolist(),
        countersink_depth_m=-1.0, flushness_m=-1.0, surface_deviation_m=float(rivet.rms), passed=True,
        detail=json.dumps({"id": rivet.id, "d_head_mm": round(2000 * rivet.radius, 3),
                           "height_mm": round(rivet.height * 1000, 3), "rms_mm": round(rivet.rms * 1000, 3),
                           "edge_coverage": round(rivet.coverage, 2), "partial": rivet.partial,
                           "scanned_share": round(rivet.scanned_share, 2)}))
