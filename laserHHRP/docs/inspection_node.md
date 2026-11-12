# inspection_node

Layer 5 (inspection algorithms). **Empty skeleton to be developed.** It declares the inspection
parameters, subscribes to the point cloud and owns the result publisher; `inspect()` does nothing
yet.

Hole and rivet detection is a separate package, [`hole_detector`](../../hole_detector/README.md), started by
`hhrp.launch.py` (`holes:=true`). It publishes one `inspection/result` per hole or rivet (`mode`).

Source: `src/inspection_node.cpp`.

## Interfaces

| Kind | Name | Type | Notes |
| --- | --- | --- | --- |
| sub | `scan/cloud` | `sensor_msgs/PointCloud2` | from `point_cloud_node` (x lateral, y scan axis, z distance) |
| pub | `inspection/result` | `laser_hhrp/InspectionResult` | one per feature; `recorder_node` writes them to `results.json` |

| Parameter | Default |
| --- | --- |
| `inspection.flushness_tolerance_m` | 0.00005 |
| `inspection.countersink_tolerance_m` | 0.00005 |
| `inspection.fastener_type` | rivet |
| `inspection.fastener_diameter_m` | 0.005 |
| `inspection.surface_deviation_limit_m` | 0.0001 |

## Processing

```
scan/cloud → inspect(cloud):
    (nothing yet)
```

Planned steps (architecture document), each producing one `InspectionResult`:

```
1. detect the rivet regions                     → feature_type, position_m
2. select the reference surface around each
3. fit a local plane to the reference surface
4. estimate the rivet head height over the plane
5. flushness_m = head height − plane (positive = proud)
6. countersink_depth_m, surface_deviation_m
7. passed = |flushness| ≤ flushness_tolerance and … ≤ the other limits
```

The simulator draws one rivet head 20 µm proud at y = 20 mm, radius 2.5 mm, on a flat surface at
z = 52.5 mm — a ready test case for the algorithm.

## Where in the code

`InspectionNode::inspect`.
