"""HTTP + WebSocket API driven by the mock ROS source (plan sections 7, 11)."""

import re
import time

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from laser_hhrp_ui.clock import Clock  # noqa: E402
from laser_hhrp_ui.server import build  # noqa: E402

P = "/api/v1"


def wait_for(fn, timeout=10.0, period=0.02):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = fn()
        if value:
            return value
        time.sleep(period)
    raise AssertionError("condition not reached")


@pytest.fixture
def app_parts(tmp_path):
    app, engine, hub, ingress, mock = build(tmp_path / "ui.db", mock=True, mock_speed=25.0,
                                            clock=Clock("synced"), fail_after_s=8.0)
    mock.detector_start_s = 0.2
    with TestClient(app) as client:
        yield client, engine, mock


def state(client):
    return client.get(P + "/state").json()


def ready(client):
    client.post(P + "/operator", json={"operator_id": "ENG-1042"})
    insp = client.post(P + "/inspections", json={"mode": "csk_depth", "request_id": "req-1"}).json()
    wait_for(lambda: state(client)["detector"]["ready"])
    r = client.patch(P + f"/inspections/{insp['uuid']}/next-expected-count", json={"count": 20})
    assert r.status_code == 200, r.text
    wait_for(lambda: state(client)["screen"] == "ready")
    return insp


def test_health_and_initial_state(app_parts):
    client, *_ = app_parts
    h = client.get(P + "/health").json()
    assert h["api"] == "ok" and h["source"] == "mock" and h["database"]["ok"]
    s = state(client)
    assert s["screen"] == "identify" and "event_seq" in s


def test_validation_and_conflict_status_codes(app_parts):
    client, *_ = app_parts
    r = client.post(P + "/operator", json={"operator_id": "bad id"})
    assert r.status_code == 400 and r.json()["error"]["field"] == "operator_id"
    assert client.post(P + "/operator", content="nope").status_code == 400
    assert client.post(P + "/inspections", json={"mode": "csk_depth"}).status_code == 409   # no operator
    client.post(P + "/operator", json={"operator_id": "op"})
    assert client.post(P + "/inspections", json={"mode": "depth"}).status_code == 400
    insp = client.post(P + "/inspections", json={"mode": "csk_depth", "request_id": "x"}).json()
    again = client.post(P + "/inspections", json={"mode": "csk_depth", "request_id": "x"})
    assert again.status_code == 200 and again.json()["uuid"] == insp["uuid"]          # double tap
    assert client.post(P + "/inspections", json={"mode": "rivet_flushness"}).status_code == 409
    r = client.patch(P + f"/inspections/{insp['uuid']}/next-expected-count", json={"count": 0})
    assert r.status_code == 400
    assert client.get(P + "/scans/nope").status_code == 404
    assert client.post(P + "/scans/nope/decision", json={"decision": "accept"}).status_code == 404


def test_full_workflow_over_http_and_websocket(app_parts):
    client, engine, mock = app_parts
    insp = ready(client)
    with client.websocket_connect("/ws") as ws:
        first = ws.receive_json()
        assert first["type"] == "snapshot" and first["payload"]["screen"] == "ready"
        assert client.post(P + "/dev/mock/scenario", json={"name": "normal"}).status_code == 200
        # bounded wait on HTTP state (a blocking WebSocket read could hang if an event never comes)
        s = wait_for(lambda: (st := state(client))["screen"] in ("results", "analysis_failed") and st, timeout=20)
        assert s["screen"] == "results", s["current_scan"]["analysis_error"]
        assert s["current_scan"]["detected_target_count"] == 20
        assert all(f["measurement"]["status"] == "not_measured" for f in s["current_scan"]["features"])
        scan_id = s["current_scan"]["scan_id"]
        r = client.post(P + f"/scans/{scan_id}/decision", json={"decision": "accept"})
        assert r.status_code == 200 and r.json()["status"] == "accepted"
        seen = []                                          # scan_decision is now certain to arrive
        while not seen or seen[-1] != "scan_decision":
            env = ws.receive_json()
            assert env["version"] == 1 and env["event_seq"] > first["event_seq"]
            seen.append(env["type"])
        order = [t for t in seen if t in ("scan_acquisition_ended", "analysis_complete", "scan_decision")]
        assert order == ["scan_acquisition_ended", "analysis_complete", "scan_decision"]
        late = seen[seen.index("scan_acquisition_ended"):seen.index("analysis_complete")].count("feature_detected")
        assert late > 0                                    # detections after release
    assert client.post(P + f"/scans/{scan_id}/decision", json={"decision": "accept"}).status_code == 200
    assert client.post(P + f"/scans/{scan_id}/decision", json={"decision": "rescan"}).status_code == 409
    assert state(client)["screen"] == "ready"
    hist = client.get(P + f"/inspections/{insp['uuid']}/scans").json()
    assert [h["status"] for h in hist] == ["accepted"]
    detail = client.get(P + f"/scans/{scan_id}").json()
    assert len(detail["features"]) == 22                    # 20 countersinks + 2 plain holes kept
    assert client.post(P + f"/inspections/{insp['uuid']}/end").status_code == 200
    assert state(client)["screen"] == "setup"
    assert client.post(P + "/operator/logout").status_code == 200


def test_reconnect_gets_fresh_snapshot_mid_analysis(app_parts):
    client, engine, mock = app_parts
    ready(client)
    client.post(P + "/dev/mock/fault", json={"fault": "stall"})       # analysis never finalizes
    client.post(P + "/dev/mock/scenario", json={"name": "normal"})
    wait_for(lambda: state(client)["screen"] == "finishing_analysis", timeout=15)
    with client.websocket_connect("/ws") as ws:
        snap = ws.receive_json()
        assert snap["type"] == "snapshot" and snap["payload"]["screen"] == "finishing_analysis"
        scan = snap["payload"]["current_scan"]
        assert scan["analysis"] == "pending" and scan["features"]
    with client.websocket_connect("/ws") as ws:               # reload: same authoritative state
        again = ws.receive_json()["payload"]["current_scan"]
        assert again["scan_id"] == scan["scan_id"] and len(again["features"]) >= len(scan["features"])
    assert len({f["result_seq"] for f in again["features"]}) == len(again["features"])  # no duplicates


def test_failure_action_over_http(app_parts):
    client, engine, mock = app_parts
    ready(client)
    client.post(P + "/dev/mock/scenario", json={"name": "detector_failure"})
    s = wait_for(lambda: (st := state(client))["screen"] == "analysis_failed" and st, timeout=15)
    sid = s["current_scan"]["scan_id"]
    assert client.post(P + f"/scans/{sid}/decision", json={"decision": "accept"}).status_code == 409
    assert client.post(P + f"/scans/{sid}/failure-action", json={"action": "retry"}).status_code == 409
    r = client.post(P + f"/scans/{sid}/failure-action", json={"action": "keep_incomplete"})
    assert r.status_code == 200 and r.json()["status"] == "incomplete_retained"


def test_no_scan_control_without_mock(tmp_path):
    app, engine, *_ = build(tmp_path / "ui.db", mock=False, clock=Clock("synced"))
    with TestClient(app) as client:
        assert client.post(P + "/dev/mock/press").status_code in (404, 405)
        paths = {getattr(r, "path", "") for r in app.routes}
        scan_control = re.compile(r"press|release|scan[^/]*/[^/]*/?(start|stop|pause)|/scan/(start|stop|pause)")
        assert not [p for p in paths if scan_control.search(p)]
        client.post(P + "/operator", json={"operator_id": "op"})
        client.post(P + "/inspections", json={"mode": "csk_depth"})
        s = state(client)
        assert s["screen"] == "not_ready" and "ROS bridge not running" in s["not_ready_reasons"]
