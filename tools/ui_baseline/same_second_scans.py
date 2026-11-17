"""Two scans started within one wall-clock second: does the recorder reuse (truncate) the session dir?"""
import time, rclpy
from std_srvs.srv import Trigger
from laser_hhrp.msg import ScanStatus
rclpy.init(); n = rclpy.create_node("collide"); cal = [None]
n.create_subscription(ScanStatus, "scan/status", lambda m: cal.__setitem__(0, m.calibration), 10)
c = {k: n.create_client(Trigger, k) for k in ("scan/start", "scan/stop")}
def call(k):
    c[k].wait_for_service(timeout_sec=20); f = c[k].call_async(Trigger.Request())
    rclpy.spin_until_future_complete(n, f, timeout_sec=5); r = f.result(); print(f"{time.time():.3f} {k}: {r.success} {r.message}", flush=True)
end = time.time() + 20
while cal[0] != 2 and time.time() < end: rclpy.spin_once(n, timeout_sec=0.1)
print(f"calibration state={cal[0]}", flush=True)
t = time.time() + 3                                               # let profiles and triggers flow
while time.time() < t: rclpy.spin_once(n, timeout_sec=0.05)
while time.time() % 1 > 0.2: rclpy.spin_once(n, timeout_sec=0.01)   # start early in a second
for _ in range(2):
    call("scan/start"); t = time.time() + 0.25
    while time.time() < t: rclpy.spin_once(n, timeout_sec=0.01)
    call("scan/stop")
t = time.time() + 1.5
while time.time() < t: rclpy.spin_once(n, timeout_sec=0.05)
