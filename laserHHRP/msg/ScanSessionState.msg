# Scan identity companion of scan/status (operator UI metadata; never an acquisition command:
# scan/status alone drives the synchronizer, point cloud and recorder).
# One message per scan transition (is_transition true) plus a 1 Hz snapshot of the latest state.
uint8 IDLE=0
uint8 SCANNING=1
uint8 PAUSED=2
uint8 ERROR=3
std_msgs/Header header                # publish time
string scan_id                        # UUID allocated on a successful start; empty before the first scan
string controller_instance_id         # UUID of this scan_controller process
uint64 transition_seq                 # +1 on every scan transition of this instance: a gap = missed transitions
bool is_transition                    # false: periodic snapshot (repeats the latest transition_seq)
uint8 state
builtin_interfaces/Time status_stamp  # header.stamp of the scan/status message published for this transition
