"""Rivet head segmentation on a growing, stitched scan (numpy + scipy only).

A separate copy of the hole detector's machinery (surface fit, unscanned bands, Tracker, FrameCloud):
nothing is imported from hole_detector.detector, so the hole detector can change without moving this.
A rivet head is a round plateau standing slightly proud of the skin (panel 2: about Ø9 mm,
0.06-0.2 mm high, with a small pit in the middle). Candidates come from a matched filter on a height
image of the crop (head mean minus skin-ring mean); each head is then measured on the raw points against
the skin ring just outside it, its edge taken per sector at half the head height.
All lengths are metres. The scan advances along +y of the cloud frame.
"""

from dataclasses import dataclass

import numpy as np
from scipy import ndimage
from scipy.signal import fftconvolve

DEFAULTS = {
    "voxel": 0.0003,          # downsample grid
    "surface_tol": 0.0005,    # "on the top surface" band for the crop's surface fit
    "grid": 0.0003,           # pixel size of the 2D height image
    "rivet_diam": 0.009,      # nominal head diameter
    "diam_tol": 0.002,        # heads within rivet_diam +- this are rivets
    "min_height": 0.00004,    # a head stands at least this far proud of (or sunk into) the skin around it
    "ring": 0.002,            # skin ring outside the head that must be in the crop (and holds the skin level)
    "max_depth": 0.0085,      # boundary cut: deeper points (floor seen through a nearby hole) are left out
    "boundary_margin": 0.005,  # width of the saved boundary cut-out around the head edge (output only)
    "guard": 0.003,           # heads this close to a crop end are left for the next callback
    "dedupe_tol": 0.002,      # same rivet if centres and diameters agree within this
    "max_normal_angle": 18.0,  # degrees between the skin normal and the cloud z axis
    "min_rim_coverage": 0.6,  # share of the scanned edge sectors that must hold edge pixels
    "max_roundness": 0.08,    # edge-fit RMS / radius
    "min_scanned_share": 0.6,  # share of the edge that must lie outside unscanned bands
}
UNITLESS = {"max_normal_angle", "min_rim_coverage", "max_roundness", "min_scanned_share"}
MIN_PLANE_SHARE = 0.30        # the skin must hold this share of the crop
GRID_FOR_TRIM = 0.0003        # edge points within 2 x this of the circle are always kept
RIM_SECTORS = 24
MIN_SKIN_BAND = 0.00003       # the skin fit keeps points within max(4 x MAD, this) of the quadratic
GAP_MARGIN = 0.0015           # skin this close to a gap (hole cone, part side) is left out of measurements
EDGE_BIN = 0.0001             # radial bin of the edge profiles (raw points)
MIN_DISMISS_CROP = 0.030      # a short crop (scan start) fits the skin poorly: its flat candidates are retried
HEAD_CLEAR = 0.0005           # a gap (hole, part side) this close outside the head edge cuts the head
MIN_ON_PART = 0.9            # share of the head + skin ring that must hold skin points (holes, part sides fail)
UNSCANNED_SHARE = 0.2         # a scan line with fewer points than this x the median is unscanned
MIN_BAND_WIDTH = 0.001        # narrower runs of unscanned lines are ignored


@dataclass
class Rivet:
    """One accepted rivet head. Lengths in metres, in the cloud frame unless noted."""
    centre: np.ndarray        # head centre on top of the head (3D)
    centre_2d: np.ndarray     # the same in the surface's (u, v) coordinates
    radius: float             # head radius at half height
    height: float             # head height above the local skin ring (negative: recessed head)
    rms: float                # edge-fit RMS
    coverage: float           # share of the scanned edge sectors that hold edge pixels
    scanned_share: float      # share of the edge outside unscanned bands
    partial: bool             # part of the edge fell in an unscanned band, or the skin ring runs off the part
    normal: np.ndarray        # surface normal at the centre, toward the sensor
    surface: dict             # the fitted surface the head was measured on
    boundary: np.ndarray      # raw, untouched input points within radius + boundary_margin
    id: int = 0               # 1, 2, ... in publish order; set by the Tracker


@dataclass
class Rejected:
    """A raised blob that failed a check, with the reason (for display)."""
    centre: np.ndarray
    centre_2d: np.ndarray
    radius: float
    surface: dict
    reason: str
    id: int = 0


# --- Copied from hole_detector/detector.py (kept identical in behaviour) ---------------------------

def voxel_down(points, voxel):
    """One point (the mean) per occupied voxel."""
    keys = np.floor(points / voxel).astype(np.int64)
    keys -= keys.min(axis=0)
    span = keys.max(axis=0) + 1
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
    """Skin = biggest z-facing plane, refined to a quadratic on the skin points only. Returns the surface frame, each
    point's 2D position (column 1 is the scan direction) and depth below it (+ = away from the sensor)."""
    found = fit_plane_ransac(points, p["surface_tol"], p["max_normal_angle"])
    if found is None or found[1].sum() < MIN_PLANE_SHARE * len(points):
        return None
    normal, inliers = found
    origin = points[inliers].mean(axis=0)
    if np.dot(-origin, normal) < 0:          # the sensor sits at the origin of the cloud frame
        normal = -normal
    along = np.array([0.0, 1.0, 0.0]) - normal[1] * normal
    along /= np.linalg.norm(along)
    axes = np.vstack((np.cross(along, normal), along))
    surface = {"origin": origin, "axes": axes, "normal": normal,
               "scale": max(np.ptp(points[inliers] @ axes.T, axis=0).max() / 2, 1e-3)}
    uv, height = (points - origin) @ axes.T, (points - origin) @ normal
    use = inliers
    for i in range(4):
        coeffs = np.linalg.lstsq(quadratic_terms(uv[use], surface["scale"]), height[use], rcond=None)[0]
        depth = quadratic_terms(uv, surface["scale"]) @ coeffs - height
        if i == 0:
            use = np.abs(depth) < 3 * p["surface_tol"]
        else:
            # Unlike the hole fit: tighten to the skin itself, or the heads (0.05-0.2 mm proud) and the
            # countersink cones pull the quadratic toward them and flatten the heads in short crops.
            spread = 1.4826 * np.median(np.abs(depth[use]))
            use = np.abs(depth) < max(4 * spread, MIN_SKIN_BAND)
    surface["coeffs"] = coeffs
    return surface, uv, depth


def surface_depth(points, surface):
    """2D position and depth below the fitted surface for any points."""
    uv = (points - surface["origin"]) @ surface["axes"].T
    height = (points - surface["origin"]) @ surface["normal"]
    return uv, quadratic_terms(uv, surface["scale"]) @ surface["coeffs"] - height


def find_unscanned(occupied, pixels, grid):
    """Scan lines (image axis 1 = v, the scan direction) the data dropped. Counts points per line."""
    counts = np.bincount(pixels[:, 1], minlength=occupied.shape[1])
    inside = np.flatnonzero(occupied.any(axis=0))
    unscanned = np.zeros(occupied.shape[1], dtype=bool)
    if len(inside):
        lo, hi = inside[0], inside[-1] + 1
        line = counts[lo:hi]
        unscanned[lo:hi] = line < UNSCANNED_SHARE * np.median(line[line > 0])
    runs, _ = ndimage.label(unscanned)
    narrow = np.flatnonzero(np.bincount(runs)[1:] * grid < MIN_BAND_WIDTH) + 1
    unscanned[np.isin(runs, narrow)] = False
    return unscanned


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
    """Fit, drop edge points far from the circle, refit. Returns centre, radius, rms, kept points."""
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
    """Edge sectors: covered share of the scanned ones, scanned share of all, and whether
    both scan-direction ends hold edge points (or were never scanned)."""
    angle = np.arctan2(rim_xy[:, 1] - centre[1], rim_xy[:, 0] - centre[0])
    covered = set(np.floor((angle + np.pi) / (2 * np.pi) * RIM_SECTORS).astype(int) % RIM_SECTORS)
    middle = -np.pi + (np.arange(RIM_SECTORS) + 0.5) * 2 * np.pi / RIM_SECTORS
    scanned = set(np.flatnonzero(scanned_at(centre[1] + radius * np.sin(middle), image)))
    ends = {RIM_SECTORS // 4 - 1, RIM_SECTORS // 4}, {3 * RIM_SECTORS // 4 - 1, 3 * RIM_SECTORS // 4}
    both_ends = all(e & covered or not e & scanned for e in ends)
    return len(covered & scanned) / max(len(scanned), 1), len(scanned) / RIM_SECTORS, both_ends


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


def circle_points(item, n=180):
    """Edge circle of a Rivet or Rejected on its surface, as N x 3 points."""
    angle = np.linspace(0, 2 * np.pi, n, endpoint=False)
    unit = np.column_stack((np.cos(angle), np.sin(angle)))
    return np.array([to_3d(item.centre_2d + item.radius * d, 0.0, item.surface) for d in unit])


# --- Rivet-specific ----------------------------------------------------------------------------------

def height_image(uv, depth, p):
    """Per-pixel mean height above the fitted surface (+ = toward the sensor) of the skin-band points,
    and the unscanned bands. Pixels with no skin point are NaN."""
    grid, pad = p["grid"], p["rivet_diam"]
    skin = np.abs(depth) < p["surface_tol"]
    corner = uv[skin].min(axis=0) - pad
    shape = tuple(np.ceil((uv[skin].max(axis=0) + pad - corner) / grid).astype(int) + 1)
    pixels = np.clip(np.floor((uv - corner) / grid).astype(int), 0, np.array(shape) - 1)
    flat = np.ravel_multi_index((pixels[skin, 0], pixels[skin, 1]), shape)
    count = np.bincount(flat, minlength=shape[0] * shape[1]).reshape(shape)
    total = np.bincount(flat, -depth[skin], minlength=shape[0] * shape[1]).reshape(shape)
    occupied = count > 0
    height = np.where(occupied, total / np.maximum(count, 1), np.nan)
    unscanned = find_unscanned(occupied, pixels, grid)
    # Skin next to a gap (a hole's countersink cone, the part's sides) is not skin level: left out of every
    # head and ring measurement. Band lines are not gaps.
    # Only real gaps (over ~1 mm): single empty pixels on a sparse head are not.
    gap = ndimage.binary_opening(~occupied & ~unscanned[None, :], iterations=2)
    near_gap = ndimage.binary_dilation(gap, iterations=max(1, int(round(GAP_MARGIN / grid))))
    image = {"corner": corner, "grid": grid, "unscanned": ndimage.binary_dilation(unscanned, iterations=2),
             "bands": unscanned, "gap": gap, "near_gap": near_gap}
    return height, occupied, pixels, image


def pixel_uv(index, image):
    return image["corner"] + (np.asarray(index) + 0.5) * image["grid"]


def disc_kernel(r_in, r_out):
    """Ring (or disc, r_in = 0) of pixels r_in <= r < r_out, in pixels."""
    n = int(np.ceil(r_out))
    y, x = np.mgrid[-n:n + 1, -n:n + 1]
    r = np.hypot(x, y)
    return ((r >= r_in) & (r < r_out)).astype(float)


def find_candidates(rel, occupied, image, p):
    """Head candidates: a matched filter, mean height just inside the nominal edge minus just outside it.
    Returns (pixel index, sign) at each local extreme where the head and its ring lie on the part (holes and
    the part's sides fail that): sign +1 for a head proud of the skin, -1 for one sunk into it."""
    grid, radius = p["grid"], p["rivet_diam"] / 2
    valid = occupied & ~image["bands"][None, :] & ~image["near_gap"]
    values = np.where(valid, np.nan_to_num(rel), 0.0)
    # Two narrow annuli just inside and just outside the nominal edge: a step detector that peaks at the
    # head centre and ignores the pit, the head texture and slow skin curvature.
    head_k = disc_kernel(0.6 * radius / grid, 0.93 * radius / grid)
    ring_k = disc_kernel(1.07 * radius / grid, 1.35 * radius / grid)
    all_k = disc_kernel(0, (radius + p["ring"]) / grid)

    def mean(kernel):
        weight = fftconvolve(valid.astype(float), kernel, mode="same")
        return fftconvolve(values, kernel, mode="same") / np.maximum(weight, 1e-9)

    response = mean(head_k) - mean(ring_k)
    on_part = fftconvolve((occupied | image["bands"][None, :]).astype(float), all_k, mode="same") / all_k.sum()
    # Holes respond strongly (both ways): drop them before looking for local extremes.
    size = int(p["rivet_diam"] / grid) | 1
    found = []
    for sign in (1, -1):
        strength = np.where(on_part >= MIN_ON_PART, sign * response, 0.0)
        # The filter blurs a head whose size or centre is off: candidates at half min_height, accepted on the
        # measured height.
        peak = (strength == ndimage.maximum_filter(strength, size=size)) & (strength >= p["min_height"] / 2)
        found += [(ij, sign) for ij in np.argwhere(peak)]
    return found


def measure_head(skin_uv, skin_h, image, centre, radius, p):
    """`skin_uv` / `skin_h`: the raw skin-band points, sorted by v. Head height (head median minus the median of a skin ring just outside it) and the edge points:
    per 15-degree sector, where the radial height profile of the raw skin points (0.1 mm bins) last drops
    through half the head height. Sectors on unscanned lines give no edge point."""
    reach = p["rivet_diam"] / 2 + p["diam_tol"] + p["ring"]
    # skin_uv is sorted along the scan (v): slice the head's stretch, then box it across.
    lo, hi = np.searchsorted(skin_uv[:, 1], [centre[1] - reach, centre[1] + reach])
    near_uv, near_h = skin_uv[lo:hi], skin_h[lo:hi]
    box = np.abs(near_uv[:, 0] - centre[0]) < reach
    d, h = near_uv[box] - centre, near_h[box]
    pix = np.clip(np.floor((near_uv[box] - image["corner"]) / image["grid"]).astype(int), 0,
                  np.array(image["near_gap"].shape) - 1)
    keep = scanned_at(centre[1] + d[:, 1], image) & ~image["near_gap"][pix[:, 0], pix[:, 1]]
    d, h = d[keep], h[keep]
    r = np.hypot(d[:, 0], d[:, 1])
    head = h[(r > 0.5 * radius) & (r < 0.9 * radius)]      # the outer plateau: the middle can be domed or pitted
    skin = h[(r > radius + 0.5 * p["diam_tol"]) & (r < radius + p["ring"])]
    if len(head) < 50 or len(skin) < 50:
        return 0.0, np.empty((0, 2))
    level = np.median(skin)
    size = float(np.median(head) - level)
    if size < p["min_height"] / 2:
        return size, np.empty((0, 2))                       # flat (or the wrong way): no head edges to look for
    h = h - level
    sector = np.floor((np.arctan2(d[:, 1], d[:, 0]) + np.pi) / (2 * np.pi) * RIM_SECTORS).astype(int) % RIM_SECTORS
    r0 = 0.5 * radius
    count_bins = int((p["rivet_diam"] / 2 + p["diam_tol"] + 0.5 * p["ring"] - r0) / EDGE_BIN)
    b_idx = np.floor((r - r0) / EDGE_BIN).astype(int)
    inside = (b_idx >= 0) & (b_idx < count_bins)
    # Mean height per (sector, radial bin) in one pass: the Pi cannot afford a median per bin.
    key = sector[inside] * count_bins + b_idx[inside]
    total = np.bincount(key, h[inside], RIM_SECTORS * count_bins).reshape(RIM_SECTORS, count_bins)
    count = np.bincount(key, None, RIM_SECTORS * count_bins).reshape(RIM_SECTORS, count_bins)
    # Each sector's profile over a 45-degree wedge (with its two neighbours): low heads need the points.
    total = total + np.roll(total, 1, axis=0) + np.roll(total, -1, axis=0)
    count = count + np.roll(count, 1, axis=0) + np.roll(count, -1, axis=0)
    profiles = np.where(count >= 3, total / np.maximum(count, 1), np.nan)
    ring = np.flatnonzero((r > radius + 0.5 * p["diam_tol"]) & (r < radius + p["ring"]))
    ring = ring[np.argsort(sector[ring], kind="stable")]
    ring_by_sector = np.split(h[ring], np.searchsorted(sector[ring], np.arange(1, RIM_SECTORS)))
    edges = []
    for k in range(RIM_SECTORS):
        # Each sector against its own stretch of the skin ring: a residual skin tilt of a few hundredths
        # of a mm would otherwise keep the high side above half height.
        own = ring_by_sector[k]
        profile = profiles[k] - (np.median(own) if len(own) >= 10 else 0.0)
        # The outermost bin still above half height: texture and the pit inside the head can dip lower.
        above = np.flatnonzero(np.nan_to_num(profile, nan=-np.inf) >= size / 2)
        if len(above) == 0 or above[-1] + 1 >= len(profile) or np.isnan(profile[above[-1] + 1]):
            continue
        b = above[-1] + 1
        hi_h, lo_h = profile[b - 1], profile[b]                 # linear interpolation between bin centres
        rb = r0 + (b - 0.5) * EDGE_BIN + EDGE_BIN * (hi_h - size / 2) / max(hi_h - lo_h, 1e-12)
        mid = -np.pi + (k + 0.5) * 2 * np.pi / RIM_SECTORS
        edges.append(centre + rb * np.array([np.cos(mid), np.sin(mid)]))
    return size, np.array(edges).reshape(-1, 2)


def detect(points, p, lo=-np.inf, hi=np.inf, log=print, lo_is_scan_start=False, down=None, known=()):
    """Find every rivet head whose edge plus ring lies inside [lo + guard, hi - guard] along y.

    Same contract as the hole detector's `detect`: returns (rivets, rejected, view), or None when the
    crop has no usable skin. Heads cut by the crop ends are in neither list (retried next crop).
    `down`: the crop already downsampled (the Tracker caches it). `known`: centres of rivets already reported
    and of candidates already found to be no head; a candidate on one is skipped, not measured again. The
    candidates found to be no head while wholly inside the crop are returned in view["dismissed"].
    """
    if len(points) < 3:
        return None
    if down is None:
        down = voxel_down(points, p["voxel"])
    found = find_top_surface(down, p)
    if found is None:
        log("no skin surface in this crop")
        return None
    lo_limit = lo if lo_is_scan_start else lo + p["guard"]
    surface, uv, depth = found
    rel, occupied, pixels, image = height_image(uv, depth, p)
    ys = points[:, 1]
    rivets, rejected = [], []
    lo_d, hi_d = p["rivet_diam"] - p["diam_tol"], p["rivet_diam"] + p["diam_tol"]
    # Full-resolution points only around each candidate: the head search reaches rivet_diam / 2 + diam_tol + ring;
    # 1 mm more for the centre settling and the surface tilt. The cut-out is taken for accepted heads only.
    head_reach = p["rivet_diam"] / 2 + p["diam_tol"] + p["ring"]
    box = head_reach + 0.001
    dismissed = []
    for ij, sign in find_candidates(rel, occupied, image, p):
        centre_2d, radius, fitted = pixel_uv(ij, image), p["rivet_diam"] / 2, None
        start = to_3d(centre_2d, 0.0, surface)
        if any(np.linalg.norm(start - c) < p["rivet_diam"] / 4 for c in known):
            continue                                   # reported already
        # A nominal head + ring around this start must be in the crop: a head the laser is still crossing would
        # be measured, and fail, on every cycle until it is. A bigger head, or a first guess of the centre that
        # is off, fails the reach check below and is tried again next cycle.
        need = p["rivet_diam"] / 2 + p["ring"]
        if start[1] - need < lo_limit or start[1] + need > hi - p["guard"]:
            continue
        near_points = points[np.abs(ys - start[1]) < box]
        near_points = near_points[np.abs(near_points[:, 0] - start[0]) < box]
        full_uv, full_depth = surface_depth(near_points, surface)
        on_skin = np.abs(full_depth) < p["surface_tol"]
        skin_uv, skin_h = full_uv[on_skin], -full_depth[on_skin]
        order = np.argsort(skin_uv[:, 1], kind="stable")      # sorted along the scan for measure_head
        skin_uv, skin_h = skin_uv[order], skin_h[order]
        # A sunk head is measured on the skin turned upside down, so it is again proud.
        for _ in range(2):                                      # centre and radius settle in two passes
            height, edges = measure_head(skin_uv, sign * skin_h, image, centre_2d, radius, p)
            fitted = fit_rim(edges) if len(edges) >= 6 else None
            if fitted is None:
                break
            centre_2d, radius = fitted[0], fitted[1]
        if fitted is None or height < p["min_height"]:
            if lo_limit < start[1] - head_reach and start[1] + head_reach < hi - p["guard"] and hi - lo >= MIN_DISMISS_CROP:
                dismissed.append(start)                # wholly seen on a settled skin fit: no need to look again
            continue                                   # not a head: weak candidates are noise, not rejections
        centre_2d, radius, rms, rim_xy = fitted
        centre = to_3d(centre_2d, -sign * height, surface)
        reach = radius + p["ring"]
        if centre[1] - reach < lo_limit or centre[1] + reach > hi - p["guard"]:
            continue                                   # not fully scanned (or at the crop's open end)

        def reject(reason):
            log("skip head candidate at y=%.1f mm: %s" % (centre[1] * 1000, reason))
            rejected.append(Rejected(centre, centre_2d, radius, surface, reason))

        # The head must be whole across the strip: the edge coverage only scores scanned lines, so a head
        # clipped by the part's side (or the laser's field) would otherwise pass. A gap that reaches only the
        # skin ring (a head at the field's edge) leaves the head measurable: kept, flagged partial.
        c_px = np.floor((centre_2d - image["corner"]) / image["grid"]).astype(int)
        span = int(np.ceil(reach / image["grid"]))
        i0, j0 = np.maximum(c_px - span, 0)
        window = image["gap"][i0:c_px[0] + span + 1, j0:c_px[1] + span + 1]
        gap_uv = pixel_uv(np.argwhere(window) + [i0, j0], image)
        gap_dist = np.hypot(*(gap_uv - centre_2d).T).min() if len(gap_uv) else np.inf
        if gap_dist < radius + HEAD_CLEAR:
            reject("cut by a gap or the part side")
            continue
        if not lo_d <= 2 * radius <= hi_d:
            reject("Ø%.1f not rivet size" % (2000 * radius))
            continue
        if rms > p["max_roundness"] * radius:
            reject("not round")
            continue
        coverage, scanned_share, both_ends = rim_coverage(rim_xy, centre_2d, radius, image)
        if coverage < p["min_rim_coverage"] or scanned_share < p["min_scanned_share"] or not both_ends:
            reject("edge %.0f%% of %.0f%% scanned" % (coverage * 100, scanned_share * 100))
            continue
        # Raw, untouched points in the cylinder edge + margin; only the floor seen through a nearby hole is cut.
        # shortcut: as for holes, a margin wider than ring + guard can be clipped at the crop ends.
        cut = radius + p["boundary_margin"] + 0.002
        near_points = points[np.abs(ys - centre[1]) < cut]
        near_points = near_points[np.abs(near_points[:, 0] - centre[0]) < cut]
        cut_uv, cut_depth = surface_depth(near_points, surface)
        r = np.linalg.norm(cut_uv - centre_2d, axis=1)
        boundary = (r <= radius + p["boundary_margin"]) & (np.abs(cut_depth) < p["max_depth"])
        rivets.append(Rivet(centre, centre_2d, radius, sign * height, rms, coverage, scanned_share,
                            bool(scanned_share < 1 or gap_dist < reach), local_normal(centre_2d, surface), surface,
                            near_points[boundary]))
    skin = np.abs(depth) < p["surface_tol"]
    return rivets, rejected, {"points": down, "depth": depth, "bands": band_corners(image, uv[skin], surface),
                              "dismissed": dismissed}


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
    """Feeds cumulative clouds to `detect` strip by strip; reports each rivet once.
    A copy of the hole Tracker with the head size in place of max_diam."""

    def __init__(self, params=None, log=print):
        self.p = dict(DEFAULTS, **(params or {}))
        self.log = log
        self.reset()

    @property
    def reach(self):
        """Largest head radius plus its skin ring: what a crop must hold around a head."""
        return (self.p["rivet_diam"] + self.p["diam_tol"]) / 2 + self.p["ring"]

    def reset(self):
        self.done_y = -np.inf
        self.frontier = -np.inf
        self.start_y = np.inf
        self.stamp = -np.inf
        self.sign = 1              # scan direction: +1 = +y; -1 = -y (worked on mirrored, see feed)
        self.y_range = None        # raw y extent of the previous cloud
        self.rivets = []           # the accepted rivets of this scan
        self.rejected = []
        self.strip = self.view = None
        self.bands = []
        self.shown_y = -np.inf
        self.rejections = 0
        self.voxels = np.empty((0, 3))  # downsampled voxel rows already complete (worked-on frame)
        self.dismissed = []        # centres of candidates found to be no head (worked-on frame)
        self.voxel_row = -np.inf   # rows below this index are in `voxels`

    def feed(self, points, stamp):
        """Process one cumulative cloud. Returns (was_reset, new_rivets)."""
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
        overlap = self.reach + p["guard"]
        crop_lo = self.done_y - overlap
        crop = points[y > crop_lo]
        found = detect(crop, p, crop[:, 1].min(), frontier, self.log, lo_is_scan_start=crop_lo < self.start_y,
                       down=self.downsample(crop, frontier),
                       known=[r.centre * [s, s, 1] for r in self.rivets] + self.dismissed) if len(crop) > 1000 else None
        if found is None:
            return was_reset, []
        rivets, rejected, self.view = found
        self.dismissed = [c for c in self.dismissed if c[1] > crop_lo] + self.view["dismissed"]   # worked-on frame
        if s < 0:
            rivets, rejected, self.view = mirror_back(rivets), mirror_back(rejected), mirror_view(self.view)
        down, depth = self.view["points"], self.view["depth"]
        new = [r for r in rivets if not self.seen(r, self.rivets)]
        for rivet in new:
            rivet.id = len(self.rivets) + 1
            self.rivets.append(rivet)
            self.rejected = [old for old in self.rejected if not self.on_rivet(old, [rivet])]
        self.done_y = max(self.done_y, frontier - p["guard"] - 2 * self.reach)
        shown, self.shown_y = self.shown_y, max(self.shown_y, frontier - p["guard"])
        new_band = (s * down[:, 1] >= shown) & (s * down[:, 1] < self.shown_y) & (np.abs(depth) < p["max_depth"])
        self.strip = down[new_band], depth[new_band]
        for band in self.view["bands"]:
            if (s * band[:, 1]).max() < self.shown_y:
                self.add_band(band)
        for gap in rejected:
            if self.on_rivet(gap, self.rivets):
                continue
            same = [i for i, old in enumerate(self.rejected) if self.seen(gap, [old])]
            if same:
                gap.id = self.rejected[same[0]].id
                self.rejected[same[0]] = gap
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
                self.dismissed = []
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

    @staticmethod
    def on_rivet(gap, rivets):
        """A rejected candidate within a head diameter of an accepted head is a side view of that head
        (heads cannot overlap): not worth showing."""
        return any(np.linalg.norm(gap.centre[:2] - r.centre[:2]) < 2 * r.radius for r in rivets)

    def seen(self, item, others):
        """Same rivet: centres and diameters agree within dedupe_tol."""
        tol = self.p["dedupe_tol"]
        return any(np.linalg.norm(item.centre - old.centre) < tol and abs(item.radius - old.radius) * 2 < tol
                   for old in others)


class FrameCloud:
    """The scan cloud built from laser_hhrp/ScanFrame profiles: every valid point becomes
    (x, position_m, z). Keeps only what the tracker can still use."""

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
        """Drop points the tracker's next crop can't reach (10 mm margin)."""
        p = tracker.p
        line = tracker.done_y - (tracker.reach + p["guard"]) - 0.010
        cloud = self.cloud()
        if len(cloud) and np.isfinite(line):
            self.chunks = [cloud[tracker.sign * cloud[:, 1] >= line]]

    def clear(self):
        self.chunks = []
