"""Hole detection (top rim only) on a growing, stitched scan (numpy + scipy only).

Ported from OneDrive_2_10-28-2026/hole_segmentation.py (v2: a hole is a gap in the
top surface). Holes may be plain or countersunk, through or blind; scan lines the
stitched cloud dropped are treated as unknown, not as hole. `Tracker` adds the
incremental part: it looks only at the newly scanned strip and reports each
hole once, as soon as the hole and its surface ring have been fully scanned.
All lengths are metres. The scan advances along +y of the cloud frame.
"""

from dataclasses import dataclass

import numpy as np
from scipy import ndimage

DEFAULTS = {
    "voxel": 0.0003,          # downsample grid
    "surface_tol": 0.0005,    # "on the top surface" band; the top diameter is measured at this depth
    "grid": 0.0003,           # pixel size of the 2D surface image
    "min_diam": 0.002,
    "max_diam": 0.030,
    "max_depth": 0.0085,      # deeper points are floor seen through the hole (10 mm bar - 1.5 mm)
    "ring": 0.005,            # surface ring the hole test compares against (detection)
    "boundary_margin": 0.005,  # width of the saved boundary cut-out around the top rim (output only)
    "guard": 0.003,           # gaps this close to a crop end are left for the next callback
    "dedupe_tol": 0.002,      # same hole if centres and top diameters agree within this
    "max_normal_angle": 18.0,  # degrees between the top-surface normal and the cloud z axis
    "min_hole_depth": 0.001,  # a gap with a middle that is neither empty nor this deep is a dent
    "min_rim_coverage": 0.6,  # share of the scanned rim sectors that must hold rim points
    "max_roundness": 0.08,    # rim-fit RMS / radius
    "min_scanned_share": 0.6,  # share of the rim that must lie outside unscanned bands
    "hole_type": "any",       # any | countersunk | plain
}
UNITLESS = {"max_normal_angle", "min_rim_coverage", "max_roundness", "min_scanned_share", "hole_type"}
MIN_PLANE_SHARE = 0.30        # the top surface must hold this share of the crop
GRID_FOR_TRIM = 0.0003        # rim points within 2 x this of the circle are always kept
RIM_SECTORS = 24
UNSCANNED_SHARE = 0.2         # a scan line with fewer points than this x the median is unscanned
MIN_BAND_WIDTH = 0.001        # narrower runs of unscanned lines are bridged by the image closing
EMPTY_DENSITY = 0.2           # a hole middle under this x the surface point density is empty
MIN_DEEP_POINTS = 30          # a non-empty hole middle needs this many points...
MIN_DEEP_SHARE = 0.1          # ...and this share of its points deeper than min_hole_depth
MIN_CONE_SPREAD = 0.15        # countersink: the cone wall spans this share of the radius...
MAX_CONE_CORR = -0.6          # ...and its depth falls steadily with radius (cones -0.75..-1, plain -0.1..-0.35)
FOOT_HEIGHT = 0.00005         # mirrored cone: its foot (the top rim) is where it sinks below this above the skin
FOOT_BIN = 0.0001             # radial bin of the foot search


@dataclass
class Hole:
    """One accepted hole. Lengths in metres, in the cloud frame unless noted."""
    centre: np.ndarray        # top-rim centre on the surface (3D)
    centre_2d: np.ndarray     # the same in the surface's (u, v) coordinates
    radius: float             # top-rim radius
    rms: float                # rim-fit RMS
    coverage: float           # share of the scanned rim sectors that hold rim points
    scanned_share: float      # share of the rim outside unscanned bands
    partial: bool             # part of the rim fell in an unscanned band
    countersunk: bool
    normal: np.ndarray        # surface normal at the centre, toward the sensor
    surface: dict             # the fitted surface the hole was measured on
    boundary: np.ndarray      # raw, untouched input points within radius + ring
    id: int = 0               # 1, 2, ... in publish order; set by the Tracker


@dataclass
class Rejected:
    """A round gap that failed a check, with the reason (for display)."""
    centre: np.ndarray
    centre_2d: np.ndarray
    radius: float
    surface: dict
    reason: str
    id: int = 0


def voxel_down(points, voxel):
    """One point (the mean) per occupied voxel."""
    keys = np.floor(points / voxel).astype(np.int64)
    keys -= keys.min(axis=0)
    span = keys.max(axis=0) + 1
    # One int64 per voxel: a 1D unique is ~10x faster than unique(axis=0) on rows.
    packed = (keys[:, 0] * span[1] + keys[:, 1]) * span[2] + keys[:, 2]
    _, index, counts = np.unique(packed, return_inverse=True, return_counts=True)
    sums = np.column_stack([np.bincount(index, points[:, k], len(counts)) for k in range(3)])
    return sums / counts[:, None]


def fit_plane_ransac(points, tol, max_angle_deg, iterations=200, sample=5000):
    """Largest plane whose normal is within max_angle of z. Fixed seed: same result every run."""
    if len(points) < 3:
        return None
    rng = np.random.default_rng(0)
    pts = points[rng.choice(len(points), min(sample, len(points)), replace=False)]
    min_cos = np.cos(np.radians(max_angle_deg))
    best_normal, best_count, best_d = None, 0, 0.0
    for _ in range(iterations):
        a, b, c = pts[rng.choice(len(pts), 3, replace=False)]
        normal = np.cross(b - a, c - a)
        length = np.linalg.norm(normal)
        if length < 1e-12 or abs(normal[2]) / length < min_cos:
            continue
        normal /= length
        d = -normal @ a
        count = np.count_nonzero(np.abs(pts @ normal + d) < tol)
        if count > best_count:
            best_normal, best_count, best_d = normal, count, d
    if best_normal is None:
        return None
    inliers = np.abs(points @ best_normal + best_d) < tol
    return best_normal, inliers


def quadratic_terms(uv, scale):
    """Columns 1, u, v, u*u, u*v, v*v of the curved-surface fit (u, v scaled to about +-1)."""
    u, v = uv[:, 0] / scale, uv[:, 1] / scale
    return np.column_stack((np.ones(len(u)), u, v, u * u, u * v, v * v))


def find_top_surface(points, p):
    """Top surface = biggest z-facing plane, refined to a quadratic.

    Returns the surface frame and, for every point, its 2D position (column 1 is the
    scan direction) and its depth below the surface (positive = away from the sensor).
    """
    found = fit_plane_ransac(points, p["surface_tol"], p["max_normal_angle"])
    if found is None or found[1].sum() < MIN_PLANE_SHARE * len(points):
        return None
    normal, inliers = found
    origin = points[inliers].mean(axis=0)
    if np.dot(-origin, normal) < 0:          # the sensor sits at the origin of the cloud frame
        normal = -normal
    # Fixed axes (not PCA): column 1 follows the scan direction, so short crops keep their meaning.
    along = np.array([0.0, 1.0, 0.0]) - normal[1] * normal
    along /= np.linalg.norm(along)
    axes = np.vstack((np.cross(along, normal), along))
    surface = {"origin": origin, "axes": axes, "normal": normal,
               "scale": max(np.ptp(points[inliers] @ axes.T, axis=0).max() / 2, 1e-3)}
    uv, height = (points - origin) @ axes.T, (points - origin) @ normal
    use = inliers
    for _ in range(3):
        coeffs = np.linalg.lstsq(quadratic_terms(uv[use], surface["scale"]), height[use], rcond=None)[0]
        depth = quadratic_terms(uv, surface["scale"]) @ coeffs - height
        use = np.abs(depth) < 3 * p["surface_tol"]
    surface["coeffs"] = coeffs
    return surface, uv, depth


def surface_depth(points, surface):
    """2D position and depth below the fitted surface for any points."""
    uv = (points - surface["origin"]) @ surface["axes"].T
    height = (points - surface["origin"]) @ surface["normal"]
    return uv, quadratic_terms(uv, surface["scale"]) @ surface["coeffs"] - height


def find_unscanned(surface, pixels, grid):
    """Scan lines (image axis 1 = v, the scan direction) the stitched cloud dropped.

    Counts points of any depth per line; a hole only empties part of a line.
    """
    counts = np.bincount(pixels[:, 1], minlength=surface.shape[1])
    inside = np.flatnonzero(surface.any(axis=0))
    unscanned = np.zeros(surface.shape[1], dtype=bool)
    if len(inside):
        lo, hi = inside[0], inside[-1] + 1
        line = counts[lo:hi]
        unscanned[lo:hi] = line < UNSCANNED_SHARE * np.median(line[line > 0])
    runs, count = ndimage.label(unscanned)
    narrow = np.flatnonzero(np.bincount(runs)[1:] * grid < MIN_BAND_WIDTH) + 1
    unscanned[np.isin(runs, narrow)] = False
    return unscanned


def fill_unscanned(surface, unscanned):
    """Fill each unscanned line from the nearest scanned lines on both sides: surface where
    either side is surface, gap where both are. A hole crossing a band stays one gap, and a
    band neither splits the part nor joins a hole to the outside."""
    known = np.flatnonzero(~unscanned)
    cols = np.flatnonzero(unscanned)
    if len(known) == 0 or len(cols) == 0:
        return surface
    pos = np.searchsorted(known, cols)
    left, right = known[np.maximum(pos - 1, 0)], known[np.minimum(pos, len(known) - 1)]
    filled = surface.copy()
    filled[:, cols] = surface[:, left] | surface[:, right]
    return filled


def find_openings(uv, on_surface, p):
    """Draw the surface as a 2D image and return its round gaps (v2 steps 3-4).

    Also returns the pixel of every point and the unscanned scan lines (v columns).
    """
    grid, pad = p["grid"], p["max_diam"]
    corner = uv[on_surface].min(axis=0) - pad
    shape = tuple(np.ceil((uv[on_surface].max(axis=0) + pad - corner) / grid).astype(int) + 1)
    pixels = np.clip(np.floor((uv - corner) / grid).astype(int), 0, np.array(shape) - 1)
    surface = np.zeros(shape, dtype=bool)
    surface[pixels[on_surface, 0], pixels[on_surface, 1]] = True
    unscanned = find_unscanned(surface, pixels, grid)
    surface = fill_unscanned(ndimage.binary_closing(surface, iterations=2), unscanned)
    radius_px = (p["max_diam"] / 2 + 1e-3) / grid
    grown = ndimage.distance_transform_edt(~surface) <= radius_px
    part = ndimage.binary_fill_holes(ndimage.distance_transform_edt(grown) > radius_px)
    r = p["min_diam"] / 2 / grid
    core = ndimage.distance_transform_edt(part & ~surface) > r
    labels, count = ndimage.label(ndimage.distance_transform_edt(~core) <= r)
    openings = []
    for label in range(1, count + 1):
        region = labels == label
        diameter = 2 * np.sqrt(region.sum() * grid * grid / np.pi)
        if p["min_diam"] <= diameter <= p["max_diam"]:
            openings.append(region)
    # Lines next to a band count as unscanned too: their rim points are dropped as band edges.
    near_band = ndimage.binary_dilation(unscanned, iterations=2)
    return openings, pixels, {"corner": corner, "grid": grid, "unscanned": near_band, "bands": unscanned}


def fit_circle_kasa(xy):
    """Fit a 2D circle with Kasa least squares; return centre and radius."""
    if len(xy) < 3:
        return None
    matrix = np.column_stack((xy[:, 0], xy[:, 1], np.ones(len(xy))))
    rhs = -(xy[:, 0] ** 2 + xy[:, 1] ** 2)
    a, b, c = np.linalg.lstsq(matrix, rhs, rcond=None)[0]
    centre = np.array([-a / 2.0, -b / 2.0])
    radius_squared = np.dot(centre, centre) - c
    return (centre, np.sqrt(radius_squared)) if radius_squared > 0 else None


def fit_rim(rim_xy):
    """Fit, drop rim points far from the circle, refit (v2). Returns centre, radius, rms, kept points."""
    fit = None
    for _ in range(3):
        fit = fit_circle_kasa(rim_xy)
        if fit is None:
            return None
        residual = np.abs(np.linalg.norm(rim_xy - fit[0], axis=1) - fit[1])
        rim_xy = rim_xy[residual <= max(3 * np.median(residual), 2 * GRID_FOR_TRIM)]
    fit = fit_circle_kasa(rim_xy)
    if fit is None:
        return None
    rms = np.sqrt(np.mean((np.linalg.norm(rim_xy - fit[0], axis=1) - fit[1]) ** 2))
    return fit[0], fit[1], rms, rim_xy


def scanned_at(v, image):
    """True where the scan line through v (metres along the scan) was scanned."""
    col = np.floor((np.asarray(v) - image["corner"][1]) / image["grid"]).astype(int)
    return ~image["unscanned"][np.clip(col, 0, len(image["unscanned"]) - 1)]


def rim_coverage(rim_xy, centre, radius, image):
    """Rim sectors: covered share of the scanned ones, scanned share of all, and whether
    both scan-direction ends hold rim points (or were never scanned)."""
    angle = np.arctan2(rim_xy[:, 1] - centre[1], rim_xy[:, 0] - centre[0])
    covered = set(np.floor((angle + np.pi) / (2 * np.pi) * RIM_SECTORS).astype(int) % RIM_SECTORS)
    middle = -np.pi + (np.arange(RIM_SECTORS) + 0.5) * 2 * np.pi / RIM_SECTORS
    u_lo, u_hi = image.get("u_range", (-np.inf, np.inf))     # the laser's field across the scan
    u = centre[0] + radius * np.cos(middle)
    scanned = set(np.flatnonzero(scanned_at(centre[1] + radius * np.sin(middle), image) & (u > u_lo) & (u < u_hi)))
    # Sector k spans angle -pi + k*15deg; +v (scan forward) is +90deg, -v is -90deg.
    ends = {RIM_SECTORS // 4 - 1, RIM_SECTORS // 4}, {3 * RIM_SECTORS // 4 - 1, 3 * RIM_SECTORS // 4}
    both_ends = all(e & covered or not e & scanned for e in ends)
    return len(covered & scanned) / max(len(scanned), 1), len(scanned) / RIM_SECTORS, both_ends


def scanned_area(centre, r_in, r_out, image):
    """Area of the ring r_in..r_out around centre that lies on scanned lines."""
    step = image["grid"] / 2
    u, v = np.meshgrid(np.arange(-r_out, r_out, step), np.arange(-r_out, r_out, step))
    r = np.hypot(u, v)
    ring = (r >= r_in) & (r < r_out)
    return np.count_nonzero(scanned_at(centre[1] + v[ring], image)) * step * step


def is_hole(r, depth, centre, radius, image, p):
    """A hole's middle is (nearly) empty, or holds enough points deeper than min_hole_depth.
    A dent is shallow all over. Densities count only scanned lines."""
    core = r < 0.8 * radius
    ring = (r >= radius) & (r < radius + p["ring"]) & (np.abs(depth) < p["surface_tol"])
    core_area, ring_area = scanned_area(centre, 0, 0.8 * radius, image), scanned_area(
        centre, radius, radius + p["ring"], image)
    if core_area <= 0 or ring_area <= 0:
        return False
    if core.sum() / core_area < EMPTY_DENSITY * ring.sum() / ring_area:
        return True
    deep = core & (depth > p["min_hole_depth"]) & (depth < p["max_depth"])
    return deep.sum() >= MIN_DEEP_POINTS and deep.sum() >= MIN_DEEP_SHARE * core.sum()


def mirrored_cone(r, depth, radius, p):
    """A countersink seen mirrored, as a cone standing above the skin (a laser artefact of long exposures):
    the wall test of is_countersunk on the height above the skin."""
    return is_countersunk(r, -depth, radius, p)


def cone_foot(uv, depth, centre, radius, p):
    """Top rim of a mirrored cone: per rim sector, walking out from the gap, the first radial bin whose
    median height above the skin is below FOOT_HEIGHT. Returns those points (2D), one per sector found."""
    rel = uv - centre
    r = np.hypot(rel[:, 0], rel[:, 1])
    near = (r > 0.8 * radius) & (r < radius + p["ring"])
    r, depth = r[near], depth[near]
    angle = np.arctan2(rel[near, 1], rel[near, 0])
    sector = np.floor((angle + np.pi) / (2 * np.pi) * RIM_SECTORS).astype(int) % RIM_SECTORS
    feet = []
    for k in range(RIM_SECTORS):
        mine = sector == k
        if mine.sum() < 10:
            continue
        bins = np.floor((r[mine] - 0.8 * radius) / FOOT_BIN).astype(int)
        heights = [np.median(-depth[mine][bins == b]) if np.any(bins == b) else np.nan for b in range(bins.max() + 1)]
        low = np.flatnonzero(np.array(heights) < FOOT_HEIGHT)
        if len(low) == 0:
            continue
        foot = 0.8 * radius + low[0] * FOOT_BIN
        middle = -np.pi + (k + 0.5) * 2 * np.pi / RIM_SECTORS
        feet.append(centre + foot * np.array([np.cos(middle), np.sin(middle)]))
    return np.array(feet)


def is_countersunk(r, depth, radius, p):
    """A cone wall inside the rim: wall points spread over the radius, deeper toward the centre.
    A plain hole's wall shows as scattered depths with no trend, a blind hole's bottom is flat."""
    wall = (r > 0.5 * radius) & (r < 0.95 * radius) & (depth > p["surface_tol"]) & (depth < p["max_depth"])
    if wall.sum() < MIN_DEEP_POINTS:
        return False
    spread = np.subtract(*np.percentile(r[wall], [90, 10]))
    return spread >= MIN_CONE_SPREAD * radius and np.corrcoef(r[wall], depth[wall])[0, 1] <= MAX_CONE_CORR


def to_3d(centre_2d, extra_depth, surface):
    """3D point on (or extra_depth below) the fitted surface above a 2D position."""
    height = quadratic_terms(centre_2d[None, :], surface["scale"]) @ surface["coeffs"]
    return (surface["origin"] + centre_2d @ surface["axes"] +
            (height[0] - extra_depth) * surface["normal"])


def local_normal(centre_2d, surface):
    """Surface normal (toward the sensor) at a 2D position, from the quadratic's slope."""
    _, c1, c2, c3, c4, c5 = surface["coeffs"]
    s = surface["scale"]
    u, v = centre_2d / s
    dh_du = (c1 + 2 * c3 * u + c4 * v) / s
    dh_dv = (c2 + c4 * u + 2 * c5 * v) / s
    n = surface["normal"] - dh_du * surface["axes"][0] - dh_dv * surface["axes"][1]
    return n / np.linalg.norm(n)


def detect(points, p, lo=-np.inf, hi=np.inf, log=print, lo_is_scan_start=False, down=None, known=()):
    """Find every hole whose top rim plus ring lies inside [lo + guard, hi - guard] along y.

    `points` is the full-resolution crop (N x 3). The guard is dropped at `lo` when that is
    the real start of the scan, not an open crop edge. Returns (holes, rejected, view), or None
    when the crop has no usable top surface. Gaps cut by the crop ends are in neither list:
    they are retried on the next crop. `view` is for display: the downsampled crop ("points"),
    its depth below the fitted surface ("depth") and the unscanned bands ("bands", 4 corners each).
    `down`: the crop already downsampled (the Tracker caches it), else it is computed here. `known`: (centre,
    radius) of holes already reported; a gap that matches one (within dedupe_tol) is skipped, not re-measured.
    """
    if len(points) < 3:
        return None
    if down is None:
        down = voxel_down(points, p["voxel"])
    found = find_top_surface(down, p)
    if found is None:
        log("no top surface in this crop")
        return None
    lo_limit = lo if lo_is_scan_start else lo + p["guard"]
    surface, uv, depth = found
    on_surface = np.abs(depth) < p["surface_tol"]
    openings, pixels, image = find_openings(uv, on_surface, p)
    image["u_range"] = (uv[:, 0].min(), uv[:, 0].max())
    ys = points[:, 1]
    holes, rejected = [], []
    for region in openings:
        rim_image = ndimage.binary_dilation(region, iterations=2) & ~region
        # Band edges are straight cuts, not rim.
        rim = on_surface & rim_image[pixels[:, 0], pixels[:, 1]] & ~image["unscanned"][pixels[:, 1]]
        fitted = fit_rim(uv[rim])
        if fitted is None:
            continue
        centre_2d, radius, rms, rim_xy = fitted
        centre = to_3d(centre_2d, 0.0, surface)
        reach = radius + p["ring"]
        if centre[1] - reach < lo_limit or centre[1] + reach > hi - p["guard"]:
            continue                                   # not fully scanned (or at the crop's open end)
        if any(np.linalg.norm(centre - c) < p["dedupe_tol"] and abs(radius - r) * 2 < p["dedupe_tol"] for c, r in known):
            continue                                   # reported already

        def reject(reason):
            log("skip gap at y=%.1f mm: %s" % (centre[1] * 1000, reason))
            rejected.append(Rejected(centre, centre_2d, radius, surface, reason))

        # The checks, cheapest first. Each failure is reported with its reason.
        if rms > p["max_roundness"] * radius:
            reject("not round")
            continue
        coverage, scanned_share, both_ends = rim_coverage(rim_xy, centre_2d, radius, image)
        if coverage < p["min_rim_coverage"] or scanned_share < p["min_scanned_share"] or not both_ends:
            reject("rim %.0f%% of %.0f%% scanned" % (coverage * 100, scanned_share * 100))
            continue
        # Full-resolution points only around this gap: every test below stays within the refitted radius
        # (<= 1.5 x radius) + ring or + boundary_margin; 4 mm more covers the surface tilt and the gap's depth.
        box = 1.5 * radius + max(p["ring"], p["boundary_margin"]) + 0.004
        near_points = points[np.abs(ys - centre[1]) < box]
        near_points = near_points[np.abs(near_points[:, 0] - centre[0]) < box]
        full_uv, full_depth = surface_depth(near_points, surface)
        r = np.linalg.norm(full_uv - centre_2d, axis=1)
        # The cone tests use the skin just outside the ring as zero: the crop's surface fit can be off by
        # a tenth of a millimetre on a long, bent panel.
        outer = (r > radius + p["ring"] / 2) & (r < radius + p["ring"]) & (np.abs(full_depth) < p["surface_tol"])
        local = full_depth - (np.median(full_depth[outer]) if outer.any() else 0.0)
        mirrored = mirrored_cone(r, local, radius, p)
        if not mirrored and not is_hole(r, full_depth, centre_2d, radius, image, p):
            reject("shallow dent")
            continue
        if mirrored:     # the cone stands above the skin: the top rim is its foot, not the 0.5 mm level
            feet = cone_foot(full_uv, local, centre_2d, radius, p)
            refit = fit_rim(feet) if len(feet) >= 6 else None
            if refit is not None and radius <= refit[1] <= radius + p["ring"] / 2 and refit[2] <= p["max_roundness"] * refit[1]:
                centre_2d, radius, rms = refit[:3]
                centre = to_3d(centre_2d, 0.0, surface)
                r = np.linalg.norm(full_uv - centre_2d, axis=1)
        countersunk = mirrored or is_countersunk(r, full_depth, radius, p)
        if p["hole_type"] != "any" and countersunk != (p["hole_type"] == "countersunk"):
            reject("not " + p["hole_type"])
            continue
        # Raw, untouched points in the cylinder top rim + ring; only the floor seen through the hole is cut.
        # shortcut: a hole is accepted once rim + ring + guard (8 mm) is in the crop, so a wider cut-out
        # can be clipped at the crop ends (and anything past the first scanned line is simply not there);
        # widen the crop overlap if wider complete cut-outs are needed.
        boundary = (r <= radius + p["boundary_margin"]) & (np.abs(full_depth) < p["max_depth"])
        holes.append(Hole(centre, centre_2d, radius, rms, coverage, scanned_share, bool(scanned_share < 1),
                          bool(countersunk), local_normal(centre_2d, surface), surface, near_points[boundary]))
    return holes, rejected, {"points": down, "depth": depth, "bands": band_corners(image, uv[on_surface], surface)}


def band_corners(image, surface_uv, surface):
    """Each unscanned band as the 4 corners (3D, on the surface) of a slab across the part."""
    labels, _ = ndimage.label(image["bands"])
    u_lo, u_hi = surface_uv[:, 0].min(), surface_uv[:, 0].max()
    corners = []
    for (cols,) in ndimage.find_objects(labels):
        v_lo, v_hi = image["corner"][1] + np.array([cols.start, cols.stop]) * image["grid"]
        corners.append(np.array([to_3d(np.array(uv), 0.0, surface)
                                 for uv in ((u_lo, v_lo), (u_hi, v_lo), (u_hi, v_hi), (u_lo, v_hi))]))
    return corners


def circle_points(hole, n=180):
    """Top circle of a Hole or Rejected on its surface, as N x 3 points."""
    angle = np.linspace(0, 2 * np.pi, n, endpoint=False)
    unit = np.column_stack((np.cos(angle), np.sin(angle)))
    return np.array([to_3d(hole.centre_2d + hole.radius * d, 0.0, hole.surface) for d in unit])


def mirror(points):
    """(x, y, z) -> (-x, -y, z): a 180 degree turn about z, its own inverse and exact in floating point."""
    return points * np.array([-1.0, -1.0, 1.0])


def mirror_back(items):
    """Results found on a mirrored cloud, in the cloud frame again (each surface turned once)."""
    surfaces = {}
    for item in items:
        key = id(item.surface)
        if key not in surfaces:
            f = item.surface
            surfaces[key] = dict(f, origin=mirror(f["origin"]), axes=mirror(f["axes"]), normal=mirror(f["normal"]))
        item.surface = surfaces[key]
        item.centre = mirror(item.centre)
        if hasattr(item, "normal"):
            item.normal, item.boundary = mirror(item.normal), mirror(item.boundary)
    return items


def mirror_view(view):
    return dict(view, points=mirror(view["points"]), bands=[mirror(b) for b in view["bands"]])


class Tracker:
    """Feeds cumulative clouds to `detect` strip by strip; reports each hole once.

    `holes` and `rejected` hold the current scan. A rejected gap keeps its id and shows its
    latest verdict while overlapping crops re-examine it; it is dropped if it later passes.
    """

    def __init__(self, params=None, log=print):
        self.p = dict(DEFAULTS, **(params or {}))
        self.log = log
        self.reset()

    def reset(self):
        self.done_y = -np.inf      # everything before this has been decided
        self.frontier = -np.inf
        self.start_y = np.inf      # where this scan began (a trimmed cloud no longer shows it)
        self.stamp = -np.inf
        self.sign = 1              # scan direction: +1 = +y; -1 = -y (worked on mirrored, see feed)
        self.y_range = None        # raw y extent of the previous cloud
        self.holes = []
        self.rejected = []
        self.strip = self.view = None
        self.bands = []            # unscanned bands behind the laser, 4 corners each (id = index + 1)
        self.shown_y = -np.inf     # the deviation view has been published up to here
        self.rejections = 0        # ids stay unique when a rejection later turns into a hole
        self.voxels = np.empty((0, 3))  # downsampled voxel rows already complete (worked-on frame)
        self.voxel_row = -np.inf   # rows below this index are in `voxels`

    def feed(self, points, stamp):
        """Process one cumulative cloud. Returns (was_reset, new_holes).

        Afterwards `strip` holds the surface scanned since the last strip, up to `guard` behind the
        frontier (downsampled points and their depth below the fitted surface), or None; `view`
        holds the whole crop's display data (see `detect`), or None.
        """
        p = self.p
        self.strip = self.view = None
        points = points[np.isfinite(points).all(axis=1)]
        if len(points) == 0:
            return False, []
        raw_y = points[:, 1]
        top = raw_y.max() if self.sign > 0 else -raw_y.min()    # the frontier, in the worked-on frame
        lo, hi = raw_y.min(), raw_y.max()
        # A new scan that runs back the other way starts at the old frontier: the cloud shrinks to a short
        # stretch at one end. FrameCloud.trim also shrinks it, but never below ~70 mm (guard + reach + 10 mm).
        turned_back = self.y_range is not None and hi - lo < 0.030 and (
            lo > self.y_range[0] + 0.010 or hi < self.y_range[1] - 0.010)
        was_reset = stamp < self.stamp or top < self.frontier - 0.010 or turned_back
        if was_reset:
            self.log("new scan detected: reset")
            self.reset()
        self.turn(lo, hi)
        s = self.sign
        if s < 0:   # a -y scan is worked on mirrored, (x, y) -> (-x, -y): exact, and the logic below stays +y
            points = mirror(points)
        y = points[:, 1]
        # shortcut: max y as the frontier assumes no stray points ahead of the scan; use a percentile if they appear.
        frontier = y.max()
        self.stamp, self.frontier = stamp, frontier
        self.start_y = min(self.start_y, y.min())
        overlap = p["max_diam"] / 2 + p["ring"] + p["guard"]
        crop_lo = self.done_y - overlap
        crop = points[y > crop_lo]
        found = detect(crop, p, crop[:, 1].min(), frontier, self.log, lo_is_scan_start=crop_lo < self.start_y,
                       down=self.downsample(crop, frontier),
                       known=[(h.centre * [s, s, 1], h.radius) for h in self.holes]) if len(crop) > 1000 else None
        if found is None:
            return was_reset, []         # nothing decided: keep done_y, so this strip is retried
        holes, rejected, self.view = found
        if s < 0:
            holes, rejected, self.view = mirror_back(holes), mirror_back(rejected), mirror_view(self.view)
        down, depth = self.view["points"], self.view["depth"]
        new = [hole for hole in holes if not self.seen(hole, self.holes)]
        for hole in new:
            hole.id = len(self.holes) + 1
            self.holes.append(hole)
            # A gap rejected on an earlier crop can pass on a later one: it is a hole now.
            self.rejected = [old for old in self.rejected if not self.seen(old, [hole])]
        # Any hole not yet accepted still lies wholly behind this line plus the overlap next time.
        self.done_y = max(self.done_y, frontier - p["guard"] - p["max_diam"] - p["ring"])
        # Points behind the frontier never change (no back-fill), so the view need not wait for done_y.
        shown, self.shown_y = self.shown_y, max(self.shown_y, frontier - p["guard"])
        new_band = (s * down[:, 1] >= shown) & (s * down[:, 1] < self.shown_y) & (np.abs(depth) < p["max_depth"])
        self.strip = down[new_band], depth[new_band]
        for band in self.view["bands"]:
            if (s * band[:, 1]).max() < self.shown_y:      # wholly behind the laser: it will not fill any more
                self.add_band(band)
        for gap in rejected:
            if self.seen(gap, self.holes):
                continue
            same = [i for i, old in enumerate(self.rejected) if self.seen(gap, [old])]
            if same:
                gap.id = self.rejected[same[0]].id
                self.rejected[same[0]] = gap          # latest verdict
            else:
                self.rejections += 1
                gap.id = self.rejections
                self.rejected.append(gap)
        return was_reset, new

    def downsample(self, crop, frontier):
        """voxel_down of the crop, reusing voxel rows (along y) that are complete: profiles arrive in scan
        order, so a row behind the frontier gets no more points. Only the newest rows, and the crop's first row
        (cut by the crop), are averaged again. Same points in the same order as voxel_down(crop): the surface
        fit samples by index, so a different order would move the fit."""
        voxel = self.p["voxel"]
        row = np.floor(crop[:, 1] / voxel)
        first = row.min()
        cached = np.floor(self.voxels[:, 1] / voxel)
        self.voxels = self.voxels[cached > first]                    # the crop's first row is redone below
        redo = (row >= self.voxel_row) | (row == first)
        fresh = voxel_down(crop[redo], voxel)
        done = np.floor(frontier / voxel)                            # rows below this are complete
        fresh_row = np.floor(fresh[:, 1] / voxel)
        self.voxels = np.vstack((self.voxels, fresh[(fresh_row < done) & (fresh_row > first)]))
        self.voxel_row = max(self.voxel_row, done)
        down = np.vstack((self.voxels, fresh[(fresh_row >= done) | (fresh_row == first)]))
        # voxel_down's order: by voxel index x, then y, then z
        return down[np.lexsort(np.floor(down / voxel).T[::-1])]

    def turn(self, lo, hi):
        """Follow the scan direction: the end of the raw cloud that grows is the laser's side. A -y scan
        (the low end grows by 2 mm more than the high end) restarts the progress, mirrored; what was
        already found stays and is deduplicated as usual."""
        if self.y_range is not None:
            grow_lo, grow_hi = self.y_range[0] - lo, hi - self.y_range[1]
            sign = -1 if grow_lo > grow_hi + 0.002 else 1 if grow_hi > grow_lo + 0.002 else self.sign
            if sign != self.sign:
                self.log("scan runs toward %sy" % ("+" if sign > 0 else "-"))
                self.sign = sign
                self.done_y = self.frontier = self.shown_y = -np.inf
                self.start_y = np.inf
                self.voxels, self.voxel_row = np.empty((0, 3)), -np.inf
        self.y_range = (lo, hi)

    def raw_y(self, y):
        """A progress value (done_y, frontier, start_y) as y in the cloud frame."""
        return self.sign * y

    def add_band(self, band):
        """Keep each band once; a band seen again (overlapping in y) keeps the wider of the two views."""
        lo, hi = band[:, 1].min(), band[:, 1].max()
        for i, old in enumerate(self.bands):
            if lo <= old[:, 1].max() and hi >= old[:, 1].min():
                if np.ptp(band[:, 1]) > np.ptp(old[:, 1]):
                    self.bands[i] = band
                return
        self.bands.append(band)

    def seen(self, item, others):
        """Same hole: centres and diameters agree within dedupe_tol."""
        tol = self.p["dedupe_tol"]
        return any(np.linalg.norm(item.centre - old.centre) < tol and abs(item.radius - old.radius) * 2 < tol
                   for old in others)


class FrameCloud:
    """The scan cloud built from laser_hhrp/ScanFrame profiles, as point_cloud_node does: every
    valid point becomes (x, position_m, z). Keeps only what the tracker can still use."""

    def __init__(self):
        self.chunks = []

    def add(self, x, position, z, valid):
        valid = np.asarray(valid, dtype=bool)
        self.chunks.append(np.column_stack((np.asarray(x)[valid], np.full(valid.sum(), position),
                                            np.asarray(z)[valid])))

    def cloud(self):
        if len(self.chunks) > 1:
            self.chunks = [np.vstack(self.chunks)]
        return self.chunks[0] if self.chunks else np.empty((0, 3))

    def trim(self, tracker):
        """Drop points the tracker's next crop can't reach (10 mm margin, so the crop never starts
        at the cloud's first point and is never mistaken for the scan start)."""
        p = tracker.p
        line = tracker.done_y - (p["max_diam"] / 2 + p["ring"] + p["guard"]) - 0.010
        cloud = self.cloud()
        if len(cloud) and np.isfinite(line):
            self.chunks = [cloud[tracker.sign * cloud[:, 1] >= line]]

    def clear(self):
        self.chunks = []
