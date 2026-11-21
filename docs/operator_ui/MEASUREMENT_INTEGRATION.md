# Measurement algorithms: what they must fit to plug in

For whoever tunes the countersink depth and rivet flushness algorithms. Follow this and the algorithm drops
in without edits to its core; the ROS wrapper, limits and operator screens are handled on our side.
Status: agreed 2026-11-20; integrated 2026-11-21 (`laserHHRP/docs/inspection_node.md`): both algorithms are
submodules in `laserHHRP/third_party/`, called from C++ by `inspection_node`.

## When it runs

Once per scan, after the detector has finished and written all its files, for every target feature:
countersinks (`countersunk = True`) in CSK Depth inspections, every rivet in Rivet Flushness inspections.
At most **50 features per scan**. Your function is called **once per feature**; the code stays loaded between
scans, so setup cost is paid once.

## Input: one feature

Each scan folder holds `summary.csv` plus one PCD per feature (`hole_NN.pcd` or `rivet_NN.pcd`, NN = `id`).
Examples: `hole_detector/hole_detector_out/scan_*/`.

From `summary.csv` you get: `id`, the centre `cx_mm, cy_mm, cz_mm`, and the diameter (`top_diam_mm` /
`head_diam_mm`, for internal filtering only). Partial features (not fully scanned) are skipped before your
function is called. Ignore the other columns.

Each PCD (binary, float32 `x y z`, **metres**) holds the raw, unfiltered points within the rim / head edge
**+ 5 mm** (the ring for the local plane), down to 8.5 mm below the surface. Same frame as the centre: scan
along +y, z = laser distance axis.

**Do not hard-code the direction of z.** It depends on the scanner configuration (`.sc1`): in the
2026-11-10 scans, hole interiors have *smaller* z than the surface. Use your `invert_z` parameter for this
and for mirrored cones.

## Output: the function to provide

```python
def measure_countersink_depth(points, centre_m, diameter_m, invert_z=False, params=None):
def measure_rivet_flushness(points, centre_m, diameter_m, invert_z=False, params=None):
    # points: numpy float32 (N, 3), metres, exactly as read from the PCD
    # centre_m: (x, y, z) metres; diameter_m: metres; params: dict of your tuning values or None (defaults)
    # returns (value_m, "") when measured, or (None, reason)
```

If the core is C++, the same signature with a result struct (`measured`, `value_m`, `reason`) and the points
as `const float* xyz, size_t n` is fine; it gets wrapped either way.

**Sign conventions** (relative to the part, not to z):
- Countersink depth: from the local surface plane, positive into the part. State in your README which point
  it is measured to.
- Rivet flushness: head top relative to the local surface plane, positive = head proud, negative = sunk.

**Reason codes** when a feature cannot be measured. Return the code only; the UI shows operator wording.

| Code | When |
| --- | --- |
| `too_few_points` | not enough data on the feature or its ring |
| `surface_not_flat` | no usable local plane |
| `unexpected_shape` | doesn't look like a countersink / rivet head |
| `scan_distorted` | the patch was bent or moved during the roll (skin not one plane, head tilted to it) |
| `failed` | anything else (an exception counts as `failed` too) |

Partly scanned features never reach your function; the wrapper reports them as `partial`.

Return a reason rather than a guess: any value returned is shown as measured. Pass / fail against the limits
is done outside your code, from the configuration.

**Rules:** metres in and out; no ROS, no file writing, no printing (use `logging`); no shared state between
calls (features may run in parallel processes); same input gives the same output.

## Budget and platform

- About **2 s per scan on the Pi 5** for 50 features: roughly **40 ms per feature** on one core.
- Runs on Pi 5, Ubuntu 24.04, Python 3.12; developed on Ubuntu 22.04, Python 3.10. Write for
  **Python ≥ 3.10** and **C++17**. numpy and scipy are available; anything else must be installed on the Pi,
  so ask first (Open3D may not be available there).

## Delivering it

As a git submodule with no ROS in it:

```
README.md          what is measured, to which point, parameters and defaults
cpp/               C++ core, if any (built with CMake)
python/<package>/  the two functions
tests/data/        a few scan folders with known depth / flushness
tests/             expected values for those folders
```

The test folders with known values are how we check the integration on the Pi reproduces your results.
