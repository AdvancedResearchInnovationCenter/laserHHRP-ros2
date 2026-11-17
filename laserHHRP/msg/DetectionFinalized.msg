# The selected detector's terminal acknowledgement: exactly one per scan, after its last result.
std_msgs/Header header
string scan_id
string detector_mode         # csk_depth | rivet_flushness
uint32 frame_count           # frames processed (equals the stream's frame_count on success)
uint32 emitted_result_count  # tagged results published for this scan (result_seq 0 .. count-1)
uint32 detected_target_count # of them, targets (CSK: countersinks; rivets: rivets)
bool success
string error
