# The recorder opened or closed a raw session (frames.bin). Correlate with ScanSessionState by
# status_stamp: the recorder does not know scan UUIDs.
uint8 OPENED=0
uint8 CLOSED=1
uint8 OPEN_FAILED=2
std_msgs/Header header
uint8 event
builtin_interfaces/Time status_stamp  # header.stamp of the scan/status message that opened / closed it
string session_id                     # directory name, unique (never reused)
string session_dir
uint32 frame_count                    # frames written so far (final on CLOSED)
bool write_failed
