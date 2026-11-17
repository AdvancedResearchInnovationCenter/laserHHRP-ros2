"""Mock ROS scenarios end to end through ingress + engine (fast simulated time)."""

import asyncio

import pytest

from laser_hhrp_ui.clock import Clock
from laser_hhrp_ui.ingress import Ingress, watchdog
from laser_hhrp_ui.mock_ros import MockRos
from laser_hhrp_ui.repository import Repository
from laser_hhrp_ui.state import Engine


async def setup(mode="csk_depth", count=20, fail_after_s=120.0, **mock_kw):
    engine = Engine(Repository(":memory:"), Clock("synced"), source="mock", fail_after_s=fail_after_s)
    ingress = Ingress(engine)
    ingress.bind(asyncio.get_running_loop())
    mock = MockRos(ingress.submit, speed=40.0, detector_start_s=0.2, **mock_kw)
    engine.detector_control = mock
    tasks = [asyncio.create_task(ingress.run()), asyncio.create_task(mock.run()),
             asyncio.create_task(watchdog(engine, 0.05))]
    engine.set_operator("op")
    insp = engine.create_inspection(mode)
    for _ in range(100):
        await asyncio.sleep(0.02)
        if engine.detector.ready and engine.hardware_fresh():
            break
    engine.set_next_expected_count(insp["uuid"], count)
    assert engine.screen().value == "ready"
    return engine, mock, tasks


async def settle(engine, scan_ids, timeout=8.0):
    for _ in range(int(timeout / 0.02)):
        if all(engine.scan_detail(s)["analysis"] != "pending" for s in scan_ids):
            return [engine.scan_detail(s) for s in scan_ids]
        await asyncio.sleep(0.02)
    raise AssertionError([engine.scan_detail(s)["analysis"] for s in scan_ids])


def run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize("mode", ["csk_depth", "rivet_flushness"])
def test_normal_scan_completes_with_all_features(mode):
    async def go():
        engine, mock, tasks = await setup(mode)
        [sid] = await mock.scenario("normal")
        [v] = await settle(engine, [sid])
        assert v["analysis"] == "complete" and v["detected_target_count"] == 20 and not v["count_mismatch"]
        assert v["non_target_count"] == (2 if mode == "csk_depth" else 0)
        for t in tasks:
            t.cancel()
    run(go())


def test_short_scan_reports_only_scanned_features_and_partials():
    async def go():
        engine, mock, tasks = await setup()
        [sid] = await mock.scenario("short")
        [v] = await settle(engine, [sid])
        assert v["analysis"] == "complete" and 0 < v["detected_target_count"] < 20 and v["count_mismatch"]
        assert v["partial_target_count"] >= 1
        for t in tasks:
            t.cancel()
    run(go())


def test_fast_next_scans_stay_separate():
    async def go():
        engine, mock, tasks = await setup()
        a, b = await mock.scenario("fast_next")
        va, vb = await settle(engine, [a, b])
        assert va["detected_target_count"] == 20 and vb["detected_target_count"] < 20
        ids_a = {f["result_seq"] for f in va and engine.scan_detail(a)["features"]}
        assert len(ids_a) == len(engine.scan_detail(a)["features"])
        assert va["status"] == "undecided" and engine.screen().value == "results"
        for t in tasks:
            t.cancel()
    run(go())


def test_reordered_delivery_still_completes():
    async def go():
        engine, mock, tasks = await setup()
        [sid] = await mock.scenario("reordered")
        [v] = await settle(engine, [sid])
        assert v["analysis"] == "complete" and v["detected_target_count"] == 20
        for t in tasks:
            t.cancel()
    run(go())


@pytest.mark.parametrize("scenario, expect", [
    ("missing_result", "timeout"), ("detector_failure", "detector reported failure"),
    ("error_stop", "acquisition stopped by error")])
def test_faults_fail_explicitly(scenario, expect):
    async def go():
        engine, mock, tasks = await setup(fail_after_s=1.0)
        [sid] = await mock.scenario(scenario)
        [v] = await settle(engine, [sid])
        assert v["analysis"] == "failed" and expect in v["analysis_error"]
        assert engine.screen().value == "analysis_failed"
        for t in tasks:
            t.cancel()
    run(go())


def test_zero_feature_scan():
    async def go():
        engine, mock, tasks = await setup()
        [sid] = await mock.scenario("zero")
        [v] = await settle(engine, [sid])
        assert v["analysis"] == "complete" and v["detected_target_count"] == 0
        for t in tasks:
            t.cancel()
    run(go())
