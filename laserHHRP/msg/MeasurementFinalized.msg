# inspection_node: the measurements of one scan, exactly one per successfully finalized detection, after it.
# Every target feature of the scan appears once in `features` (CSK Depth: countersinks; Rivet Flushness: rivets).
std_msgs/Header header
string scan_id
string detector_mode          # csk_depth | rivet_flushness
string quantity               # csk_depth | flushness
float64 lower_limit_m         # GO range of the value, inclusive (from hhrp.yaml inspection.*)
float64 upper_limit_m
MeasuredFeature[] features
bool success                  # false: the scan folder could not be read; then `features` is empty
string error
float64 elapsed_s             # wall time for the whole scan
