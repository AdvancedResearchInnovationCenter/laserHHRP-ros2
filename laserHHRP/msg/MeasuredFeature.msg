# One target feature's measurement (inspection_node), inside MeasurementFinalized.
uint32 feature_id    # the detector's id (summary.csv id = TaggedInspectionResult.feature_id)
bool measured
float64 value_m      # when measured: CSK depth (positive into the part) or flushness (positive = head proud)
string reason        # when not measured: too_few_points | surface_not_flat | unexpected_shape | scan_distorted |
                     # partial (only partly scanned, not measured) | failed
float64 elapsed_s    # time this feature's measurement took
