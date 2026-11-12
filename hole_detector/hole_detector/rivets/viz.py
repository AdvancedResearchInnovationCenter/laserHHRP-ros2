"""What RViz shows for rivets: a copy of the hole viz (same STYLE, namespaces, colours), with rivet labels.
Kept separate so the hole detector can change without moving this."""

import numpy as np
from geometry_msgs.msg import Point, Pose
from scipy.spatial.transform import Rotation
from sensor_msgs.msg import PointField
from sensor_msgs_py import point_cloud2
from visualization_msgs.msg import Marker

from hole_detector.rivets.detector import circle_points

STYLE = {
    # Colours (r, g, b) by status.
    "complete_rgb": (0.20, 0.80, 0.35),   # whole edge scanned: the measurement is trustworthy
    "partial_rgb": (1.00, 0.70, 0.10),    # part of the edge fell in an unscanned band
    "rejected_rgb": (0.95, 0.20, 0.20),   # a head candidate that failed a check (dashed ring + reason)
    "band_rgba": (0.95, 0.20, 0.20, 0.35),  # unscanned band (scan lines the data dropped)
    "decided_rgb": (0.60, 0.60, 0.60),    # line: everything behind it is final
    "laser_rgb": (0.20, 0.85, 1.00),      # line: the scan frontier (where the laser is)
    "distance_rgb": (0.85, 0.85, 0.85),   # line + mm label from a rivet to its nearest earlier rivet (pitch)
    "ruler_rgb": (0.75, 0.75, 0.40),      # ruler along the scan, in cloud-frame y (matches cy_mm)
    "ruler_tick": 0.010,      # m, small tick spacing
    "ruler_label": 0.050,     # m, labelled tick spacing
    "ruler_offset": 0.006,    # m, ruler distance beside the part (toward -x)
    "label_rgb": (1.0, 1.0, 1.0),
    "ring_width": 0.0004,     # m, line width of the top rim
    "centre_size": 0.0015,    # m, sphere diameter at the centre
    "label_size": 0.004,      # m, text height
    "label_lift": 0.006,      # m, label distance above the surface along the normal
    "surface_max_points": 50000,  # per /rivets/surface message (evenly thinned above this)
}

FIELDS = [PointField(name=n, offset=4 * i, datatype=PointField.FLOAT32, count=1)
          for i, n in enumerate(("x", "y", "z", "rivet_id"))]
SURFACE_FIELDS = [PointField(name=n, offset=4 * i, datatype=PointField.FLOAT32, count=1)
                  for i, n in enumerate(("x", "y", "z", "deviation_mm"))]


def surface_cloud(header, points, depth):
    """The decided strip with each point's depth below the fitted surface, in mm (+ = deeper)."""
    step = max(1, -(-len(points) // STYLE["surface_max_points"]))      # ceil division
    data = np.column_stack((points[::step], depth[::step] * 1000)).astype(np.float32)
    return point_cloud2.create_cloud(header, SURFACE_FIELDS, data)


def rivet_label(rivet):
    """Two lines: id, head diameter, height / fit quality (and the scanned share when partial)."""
    first = "#%d  Ø%.2f  rivet" % (rivet.id, 2000 * rivet.radius)
    second = "h %.2f  rms %.2f  cov %.0f%%" % (rivet.height * 1000, rivet.rms * 1000, rivet.coverage * 100)
    if rivet.partial:
        second += "  partial %.0f%%" % (rivet.scanned_share * 100)
    return first + "\n" + second


def rivet_markers(header, rivet):
    """Head edge, centre and label of one rivet, coloured by status."""
    hid, rgb = rivet.id, STYLE["partial_rgb"] if rivet.partial else STYLE["complete_rgb"]
    label = point_marker(header, "label", hid, Marker.TEXT_VIEW_FACING,
                         rivet.centre - STYLE["label_lift"] * rivet.normal, STYLE["label_size"], STYLE["label_rgb"])
    label.text = rivet_label(rivet)
    return [line_marker(header, "top", hid, circle_points(rivet), rgb),
            point_marker(header, "centre", hid, Marker.SPHERE, rivet.centre, STYLE["centre_size"], rgb),
            label]


REJECTED_NS = ("rejected", "rejected_reason")


def rejected_markers(header, gap):
    """Dashed red ring and the reason, for a head candidate that failed a check."""
    ring = circle_points(gap, n=72)
    dashes = point_marker(header, REJECTED_NS[0], gap.id, Marker.LINE_LIST, (0, 0, 0), STYLE["ring_width"],
                          STYLE["rejected_rgb"])
    dashes.points = [point_msg(p) for p in ring]          # LINE_LIST pairs 0-1, 2-3, ...: every other segment
    reason = point_marker(header, REJECTED_NS[1], gap.id, Marker.TEXT_VIEW_FACING,
                          gap.centre - STYLE["label_lift"] * gap.surface["normal"], STYLE["label_size"],
                          STYLE["rejected_rgb"])
    reason.text = "✗ " + gap.reason
    return [dashes, reason]


def nearest(rivet, others):
    """The rivet among `others` (not itself) closest to `rivet`, or None."""
    others = [o for o in others if o is not rivet]
    return min(others, key=lambda o: np.linalg.norm(o.centre - rivet.centre)) if others else None


def distance_markers(header, rivet, other):
    """Line from the rivet to another rivet, labelled with the centre distance (pitch) in mm."""
    line = point_marker(header, "distance", rivet.id, Marker.LINE_LIST, (0, 0, 0), STYLE["ring_width"] / 2,
                        STYLE["distance_rgb"])
    line.points = [point_msg(rivet.centre), point_msg(other.centre)]
    middle = (rivet.centre + other.centre) / 2 - STYLE["label_lift"] / 2 * rivet.normal
    label = point_marker(header, "distance_label", rivet.id, Marker.TEXT_VIEW_FACING, middle,
                         0.75 * STYLE["label_size"], STYLE["distance_rgb"])
    label.text = "%.2f mm" % (np.linalg.norm(rivet.centre - other.centre) * 1000)
    return [line, label]


def ruler_markers(header, start_y, end_y, x, z):
    """Ruler along y at (x, z): small ticks every ruler_tick, labels every ruler_label (cloud-frame y, mm)."""
    tick, every = STYLE["ruler_tick"], STYLE["ruler_label"]
    line = point_marker(header, "ruler", 1, Marker.LINE_LIST, (0, 0, 0), STYLE["ring_width"], STYLE["ruler_rgb"])
    line.points = [point_msg((x, start_y, z)), point_msg((x, end_y, z))]
    labels = []
    for k in range(int(np.ceil(start_y / tick)), int(np.floor(end_y / tick)) + 1):
        y, major = k * tick, k % round(every / tick) == 0
        length = 2 * tick / 5 if major else tick / 5
        line.points += [point_msg((x, y, z)), point_msg((x - length, y, z))]
        if major:
            label = point_marker(header, "ruler_label", k, Marker.TEXT_VIEW_FACING, (x - 2 * length, y, z),
                                 STYLE["label_size"], STYLE["ruler_rgb"])
            label.text = "%.0f" % (y * 1000)
            labels.append(label)
    return [line] + labels


def band_markers(header, bands):
    """A translucent slab on the surface over each unscanned band."""
    markers = []
    for i, c in enumerate(bands, 1):
        m = point_marker(header, "bands", i, Marker.TRIANGLE_LIST, (0, 0, 0), 1.0, STYLE["band_rgba"][:3],
                         STYLE["band_rgba"][3])
        m.points = [point_msg(c[k]) for k in (0, 1, 2, 0, 2, 3)]
        markers.append(m)
    return markers


def progress_markers(header, points, done_y, frontier, status):
    """Lines across the part at done_y (final behind it) and at the frontier, and the status text."""
    markers, top = [], None
    for mid, y, rgb in ((1, done_y, STYLE["decided_rgb"]), (2, frontier, STYLE["laser_rgb"])):
        near = points[np.abs(points[:, 1] - y) < 0.0005]
        if len(near) == 0:
            continue
        z = np.median(near[:, 2])
        line = point_marker(header, "progress", mid, Marker.LINE_LIST, (0, 0, 0), 2 * STYLE["ring_width"], rgb)
        line.points = [point_msg((near[:, 0].min(), y, z)), point_msg((near[:, 0].max(), y, z))]
        markers.append(line)
        top = (near[:, 0].mean(), y, z - 2 * STYLE["label_lift"])   # z = range: smaller is toward the sensor
    if top is not None:
        text = point_marker(header, "status", 1, Marker.TEXT_VIEW_FACING, top, STYLE["label_size"], STYLE["laser_rgb"])
        text.text = status
        markers.append(text)
    return markers


def delete(header, ns, mid):
    return Marker(header=header, ns=ns, id=mid, action=Marker.DELETE)


def id_cloud(header, points, hid):
    """PointCloud2 with x, y, z and a rivet_id channel (RViz colours by it)."""
    return point_cloud2.create_cloud(header, FIELDS, with_id(points, hid))


def with_id(points, hid):
    return np.column_stack((points, np.full(len(points), hid))).astype(np.float32)


def pose(centre, normal):
    """z = surface normal, x = scan direction (+y) projected onto the surface."""
    x = np.array([0.0, 1.0, 0.0]) - normal[1] * normal
    x /= np.linalg.norm(x)
    qx, qy, qz, qw = Rotation.from_matrix(np.column_stack((x, np.cross(normal, x), normal))).as_quat()
    p = Pose()
    p.position.x, p.position.y, p.position.z = (float(v) for v in centre)
    p.orientation.x, p.orientation.y, p.orientation.z, p.orientation.w = qx, qy, qz, qw
    return p


def point_marker(header, ns, hid, kind, position, size, rgb, alpha=1.0):
    m = Marker(header=header, ns=ns, id=hid, type=kind, action=Marker.ADD)
    m.pose.position.x, m.pose.position.y, m.pose.position.z = (float(v) for v in position)
    m.pose.orientation.w = 1.0
    m.scale.x = m.scale.y = m.scale.z = size
    m.color.r, m.color.g, m.color.b, m.color.a = (*rgb, alpha)
    return m


def line_marker(header, ns, hid, points, rgb):
    """Closed line through the points."""
    m = point_marker(header, ns, hid, Marker.LINE_STRIP, (0, 0, 0), STYLE["ring_width"], rgb)
    m.points = [point_msg(p) for p in np.vstack((points, points[:1]))]
    return m


def point_msg(p):
    return Point(x=float(p[0]), y=float(p[1]), z=float(p[2]))
