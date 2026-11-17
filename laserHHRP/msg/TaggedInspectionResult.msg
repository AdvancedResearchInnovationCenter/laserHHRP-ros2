# One detector result with the identity of its scan (operator UI mode). The same result is also
# published untagged on inspection/result.
string scan_id
uint32 result_seq      # 0, 1, 2 ... per scan, in publication order
string detector_mode   # csk_depth | rivet_flushness
uint32 feature_id      # the result's detail.id
InspectionResult result
