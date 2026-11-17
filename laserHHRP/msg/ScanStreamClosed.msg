# The synchronizer will publish no more tagged frames for this scan.
std_msgs/Header header
string scan_id
uint32 frame_count     # tagged frames actually published for this scan (indices 0 .. frame_count-1)
uint8 stop_state       # ScanStatus state that ended the scan: IDLE or ERROR
string error           # non-empty: the tagged stream is incomplete (e.g. identity buffer overflow)
