"""Operator device actions (device.py): gyro calibration and power off from the screens.

Deviation from plan section 3, approved 2026-11-21: the device runs without a terminal. Both actions are refused
during a scan; power off also while an analysis is pending. Scan start / stop stays on the physical switch.
"""

import pytest

from harness import Harness
from laser_hhrp_ui.clock import Clock
from laser_hhrp_ui.device import NoDevice, RosDevice, run_power_off
from laser_hhrp_ui.state import CommandError

P = "/api/v1"


class FakeDevice:
    can_calibrate = True
    can_power_off = True


def harness():
    h = Harness()
    h.engine.device = FakeDevice()
    return h


def refused(fn, code):
    with pytest.raises(CommandError) as e:
        fn()
    assert e.value.code == code, e.value.code
    return e.value


# ---- engine rules ----
def test_calibrating_is_its_own_not_ready_reason():
    h = harness()
    h.ready_inspection()
    h.hw(calibrated=False, calibrating=True)
    ready, reasons, _ = h.engine.readiness()
    assert not ready and "Calibrating: keep the device still" in reasons and "Calibration required" not in reasons
    h.hw(calibrated=False)
    assert "Calibration required" in h.engine.readiness()[1]
    assert h.engine.snapshot()["hardware"]["calibrating"] is False


def test_calibration_allowed_only_with_the_device_idle():
    h = harness()
    h.ready_inspection()
    h.engine.check_calibrate()                                        # idle: allowed
    h.hw(scan_state="SCANNING")
    refused(h.engine.check_calibrate, "scanning")
    h.hw()
    sid = h.press()                                                   # controller says IDLE, a scan is recorded open
    h.hw(scan_state="IDLE")
    refused(h.engine.check_calibrate, "scanning")
    h.release(sid)
    h.hw(calibrating=True, calibrated=False)
    refused(h.engine.check_calibrate, "calibrating")
    h.hw()
    h.clock.advance(10.0)                                             # hardware status stale
    refused(h.engine.check_calibrate, "no_hardware")


def test_calibration_without_a_device_or_refused_by_the_node():
    h = Harness()                                                     # NoDevice
    h.ready_inspection()
    refused(h.engine.check_calibrate, "unavailable")
    h.engine.device = FakeDevice()
    refused(lambda: h.engine.record_calibration(False, "calibration already running"), "calibration_refused")
    assert h.engine.record_calibration(True, "calibrating")["started"]
    assert len(h.repo.audit_events("calibration_requested")) == 2      # refused and started, both audited


def test_power_off_refused_during_a_scan_and_a_pending_analysis():
    h = harness()
    h.ready_inspection()
    sid = h.press()
    refused(h.engine.begin_power_off, "scanning")
    h.results(sid, 3)
    h.release(sid)
    refused(h.engine.begin_power_off, "analyzing")                     # analysis pending
    h.close(sid)
    h.finalize(sid, emitted=3, targets=3)
    assert h.engine.snapshot()["screen"] == "results"
    assert h.engine.begin_power_off() is True                         # results on screen, nothing pending: allowed
    assert h.engine.snapshot()["device"]["powering_off"] is True


def test_power_off_once_and_failure_is_reported():
    h = harness()
    h.ready_inspection()
    assert h.engine.begin_power_off() is True
    assert h.engine.begin_power_off() is False                        # a repeated tap does not run it twice
    refused(h.engine.check_calibrate, "powering_off")
    h.engine.power_off_failed("Could not turn off: denied")
    s = h.engine.snapshot()
    assert s["device"]["powering_off"] is False
    assert any(a["code"] == "power_off_failed" and "denied" in a["message"] for a in s["alerts"])


def test_power_off_not_offered_without_a_command():
    h = Harness()
    h.engine.device = RosDevice(lambda: None, power_off_command=None)
    assert h.engine.snapshot()["device"] == {"can_calibrate": True, "can_power_off": False, "powering_off": False,
                                             "can_service": False, "switching": None}
    refused(h.engine.begin_power_off, "unavailable")
    h.engine.device = NoDevice()
    assert h.engine.snapshot()["device"]["can_calibrate"] is False


def test_power_off_command_result():
    assert run_power_off(["true"]) == (True, "Turning off")
    ok, msg = run_power_off(["sh", "-c", "echo 'Access denied' >&2; exit 1"])
    assert not ok and msg == "Could not turn off: Access denied"
    ok, msg = run_power_off(["/nonexistent/poweroff"])
    assert not ok and msg.startswith("Could not turn off:")
    assert run_power_off(None)[0] is False
    assert RosDevice(lambda: None).calibrate() == (False, "ROS is not running")


# ---- over HTTP with the mock ----
httpx = pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from laser_hhrp_ui.server import build  # noqa: E402
from test_api import ready, state, wait_for  # noqa: E402


@pytest.fixture
def app_parts(tmp_path):
    app, engine, hub, ingress, mock = build(tmp_path / "ui.db", mock=True, mock_speed=25.0,
                                            clock=Clock("synced"), fail_after_s=8.0)
    mock.detector_start_s = 0.2
    with TestClient(app) as client:
        yield client, engine, mock


def test_calibrate_over_http(app_parts):
    client, engine, mock = app_parts
    ready(client)
    assert state(client)["device"]["can_calibrate"] is True
    r = client.post(P + "/device/calibrate")
    assert r.status_code == 200 and r.json()["started"], r.text
    s = wait_for(lambda: (lambda s: s if s["hardware"]["calibrating"] else None)(state(client)))
    assert s["screen"] == "not_ready" and "Calibrating: keep the device still" in s["not_ready_reasons"]
    assert client.post(P + "/device/calibrate").json()["error"]["code"] == "calibrating"
    s = wait_for(lambda: (lambda s: s if s["screen"] == "ready" else None)(state(client)))
    assert s["hardware"]["calibrated"] and s["hardware"]["message"] == "gyro calibrated"


def test_failed_calibration_keeps_the_old_values_and_says_why(app_parts):
    client, engine, mock = app_parts
    ready(client)
    mock.inject("calibration_failure")
    assert client.post(P + "/device/calibrate").status_code == 200
    wait_for(lambda: state(client)["hardware"]["calibrating"])
    s = wait_for(lambda: (lambda s: s if not s["hardware"]["calibrating"] else None)(state(client)))
    assert s["hardware"]["calibrated"] and s["hardware"]["message"].startswith("calibration failed")


def test_calibrate_and_power_off_refused_while_scanning(app_parts):
    client, engine, mock = app_parts
    ready(client)
    client.post(P + "/dev/mock/press")
    wait_for(lambda: state(client)["screen"] == "scanning")
    for path, code in (("/device/calibrate", "scanning"), ("/device/power-off", "scanning")):
        r = client.post(P + path)
        assert r.status_code == 409 and r.json()["error"]["code"] == code, r.text
    assert mock.power_off_requests == 0
    client.post(P + "/dev/mock/release")
    wait_for(lambda: state(client)["screen"] in ("results", "analysis_failed"), timeout=20)


def test_power_off_over_http(app_parts):
    client, engine, mock = app_parts
    ready(client)
    r = client.post(P + "/device/power-off")
    assert r.status_code == 200 and r.json() == {"powering_off": True}
    assert client.post(P + "/device/power-off").status_code == 200
    assert mock.power_off_requests == 1                               # ran once
    assert state(client)["device"]["powering_off"] is True


def test_live_backend_without_ros_offers_neither_action(tmp_path):
    app, engine, *_ = build(tmp_path / "ui.db", mock=False, clock=Clock("synced"))
    with TestClient(app) as client:
        assert state(client)["device"] == {"can_calibrate": False, "can_power_off": False, "powering_off": False,
                                           "can_service": False, "switching": None}
        for path in ("/device/calibrate", "/device/power-off"):
            r = client.post(P + path)
            assert r.status_code == 409 and r.json()["error"]["code"] == "unavailable"


def test_power_off_failure_over_http(tmp_path):
    app, engine, *_ = build(tmp_path / "ui.db", mock=False, clock=Clock("synced"))
    engine.device = RosDevice(lambda: None, power_off_command=["false"])
    with TestClient(app) as client:
        r = client.post(P + "/device/power-off")
        assert r.status_code == 500 and r.json()["error"]["code"] == "power_off_failed"
        s = state(client)
        assert s["device"]["powering_off"] is False and s["alerts"][-1]["code"] == "power_off_failed"


# ---- service menu (developers, PIN) ----
from laser_hhrp_ui.device import KioskControl, Service, ServiceAccess, ServiceError  # noqa: E402


def test_service_pin_rules(tmp_path):
    pin = tmp_path / "pin"
    now = [100.0]
    acc = ServiceAccess(pin, max_tries=3, lock_s=60.0, monotonic=lambda: now[0])
    assert not acc.available                                          # no file: no service menu
    pin.write_text("12a4\n")
    assert not acc.available                                          # not digits
    pin.write_text("2580\n")
    assert acc.available
    acc.check("2580")
    for bad in ("0000", "", None, 2580, "25800"):
        with pytest.raises(ServiceError) as e:
            acc.check(bad)
        assert e.value.code in ("wrong_pin", "locked")
    with pytest.raises(ServiceError) as e:                            # locked after 3 wrong in a row ...
        acc.check("2580")
    assert e.value.code == "locked" and e.value.status == 429
    now[0] += 61
    acc.check("2580")                                                 # ... for 60 s
    pin.write_text("4711\n")                                         # changed over SSH: used at once
    with pytest.raises(ServiceError):
        acc.check("2580")
    acc.check("4711")


def test_kiosk_request_file(tmp_path):
    c = KioskControl(tmp_path / "kiosk")
    assert c.request("desktop") == (True, "desktop")
    assert (tmp_path / "kiosk" / "request").read_text() == "desktop\n"
    assert c.request("rm -rf")[0] is False
    (tmp_path / "ro").mkdir()
    (tmp_path / "ro").chmod(0o500)
    try:
        assert KioskControl(tmp_path / "ro" / "x").request("restart")[0] is False
    finally:
        (tmp_path / "ro").chmod(0o700)


def test_switch_refused_during_a_scan_or_a_pending_analysis_and_once_only():
    h = harness()
    h.ready_inspection()
    sid = h.press()
    refused(lambda: h.engine.begin_switch("desktop"), "scanning")
    h.results(sid, 2)
    h.release(sid)
    refused(lambda: h.engine.begin_switch("restart"), "analyzing")
    h.close(sid)
    h.finalize(sid, emitted=2, targets=2)
    assert h.engine.begin_switch("desktop") is True
    assert h.engine.begin_switch("desktop") is False
    assert h.engine.snapshot()["device"]["switching"] == "desktop"
    assert len(h.repo.audit_events("service_desktop")) == 1
    h.engine.switch_failed("Could not ask the kiosk session")
    s = h.engine.snapshot()
    assert s["device"]["switching"] is None and s["alerts"][-1]["code"] == "service_failed"


def test_service_over_http(app_parts, tmp_path):
    client, engine, mock = app_parts
    ready(client)
    assert state(client)["device"]["can_service"] is True
    r = client.post(P + "/device/service/unlock", json={"pin": "0000"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "wrong_pin"
    r = client.post(P + "/device/service/desktop", json={"pin": "0000"})
    assert r.status_code == 403 and not (tmp_path / "kiosk" / "request").exists()   # nothing without the PIN
    r = client.post(P + "/device/service/unlock", json={"pin": "2580"})
    assert r.status_code == 200 and r.json()["network"][0]["connection"] == "mock-wifi"
    assert client.post(P + "/device/service/reboot-into-anything", json={"pin": "2580"}).status_code == 404
    r = client.post(P + "/device/service/restart", json={"pin": "2580"})
    assert r.status_code == 200 and r.json() == {"switching": "restart"}
    assert (tmp_path / "kiosk" / "request").read_text() == "restart\n"
    assert state(client)["device"]["switching"] == "restart"
    assert len(engine.repo.audit_events("service_pin_refused")) == 2


def test_service_refused_while_scanning_over_http(app_parts, tmp_path):
    client, engine, mock = app_parts
    ready(client)
    client.post(P + "/dev/mock/press")
    wait_for(lambda: state(client)["screen"] == "scanning")
    r = client.post(P + "/device/service/desktop", json={"pin": "2580"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "scanning"
    assert not (tmp_path / "kiosk" / "request").exists()
    client.post(P + "/dev/mock/release")
    wait_for(lambda: state(client)["screen"] in ("results", "analysis_failed"), timeout=20)


def test_no_service_menu_without_kiosk_setup(tmp_path):
    app, engine, *_ = build(tmp_path / "ui.db", mock=False, clock=Clock("synced"), service_pin_file=tmp_path / "pin")
    with TestClient(app) as client:
        assert state(client)["device"]["can_service"] is False
        r = client.post(P + "/device/service/unlock", json={"pin": "2580"})
        assert r.status_code == 409 and r.json()["error"]["code"] == "unavailable"
