#!/usr/bin/env bash
# Legacy bag adapter check: writes a small bag in the CURRENT format (two scans of the detector tests' synthetic
# plate on /scan/frame, untagged, like an old bag), runs hole_detector in tagged mode and replays the bag
# through legacy_bag_adapter.py. Expected: both scans finalized OK (full plate: 7 holes, 3 countersinks).
#   HHRP_WS=<dir with build/ install/> tools/ui_verify/phase3_bag_adapter_check.sh [OUT_DIR]
set -o pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
OUT="${1:-$(mktemp -d -t hhrp_bagadapter.XXXXXX)}"
export HHRP_WS="${HHRP_WS:?set HHRP_WS to a built workspace (build/ install/)}"
mkdir -p "$OUT/home"
DISTRO="${ROS_DISTRO:-$(ls /opt/ros | head -1)}"
# shellcheck disable=SC1090
source "/opt/ros/$DISTRO/setup.bash"
# shellcheck disable=SC1091
source "$HHRP_WS/install/setup.bash"
export PYTHONUSERBASE="${PYTHONUSERBASE:-$HOME/.local}"
export HOME="$OUT/home" ROS_DOMAIN_ID=93 ROS_LOCALHOST_ONLY=0 ROS_LOG_DIR="$OUT/home/roslog"
export CYCLONEDDS_URI="file://$REPO/tools/ui_baseline/cyclonedds_loopback.xml"

python3 - "$OUT/bag" "$REPO" <<'EOF'
import importlib.util, sys
from pathlib import Path
import numpy as np, rosbag2_py
from rclpy.serialization import serialize_message
from builtin_interfaces.msg import Time
from laser_hhrp.msg import ScanFrame
bag, repo = Path(sys.argv[1]), Path(sys.argv[2])
spec = importlib.util.spec_from_file_location("td", repo / "hole_detector/test/test_detector.py")
sys.path.insert(0, str(repo / "hole_detector/test")); td = importlib.util.module_from_spec(spec); spec.loader.exec_module(td)
pts = td.synthetic_plate(); pts = pts[np.argsort(pts[:, 1], kind="stable")]
ys = np.unique(pts[:, 1]); b = np.searchsorted(pts[:, 1], ys); ends = list(b[1:]) + [len(pts)]
w = rosbag2_py.SequentialWriter()
w.open(rosbag2_py.StorageOptions(uri=str(bag), storage_id="mcap"), rosbag2_py.ConverterOptions("cdr", "cdr"))
w.create_topic(rosbag2_py.TopicMetadata(name="/scan/frame", type="laser_hhrp/msg/ScanFrame", serialization_format="cdr"))
t = 1_000_000_000
for scan_len in (len(ys), len(ys) // 2):                 # scan 1: whole plate, scan 2: first half
    for i in range(scan_len):
        x, z = pts[b[i]:ends[i], 0], pts[b[i]:ends[i], 2]
        f = ScanFrame(index=i, position_m=float(ys[i]), x_m=x.astype(np.float32).tolist(),
                      z_m=z.astype(np.float32).tolist(), valid=[True] * len(x))
        f.header.stamp = Time(sec=t // 1_000_000_000, nanosec=t % 1_000_000_000)
        w.write("/scan/frame", serialize_message(f), t)
        t += 8_333_333
del w
print(f"wrote {bag}")
EOF
"$HHRP_WS/install/hole_detector/lib/hole_detector/hole_detector" --ros-args \
  --params-file "$HHRP_WS/install/hole_detector/share/hole_detector/config/params.yaml" \
  -p out_dir:="$OUT/det" -p tagged:=true > "$OUT/detector.log" 2>&1 &
DET=$!
timeout 300 python3 -u "$HERE/legacy_bag_adapter.py" "$OUT/bag" 2>&1 | tee "$OUT/adapter.log"
RC=${PIPESTATUS[0]}
kill -INT "$DET" 2>/dev/null; wait "$DET" 2>/dev/null
grep -q "scan 1 .* ok, 7 results, 3 targets" "$OUT/adapter.log" || { echo "FAIL  scan 1 should give 7 holes, 3 countersinks"; RC=1; }
[ "$RC" = 0 ] && echo "PASS  legacy bag replayed through the tagged path" || echo "FAIL  (logs: $OUT)"
exit "$RC"
