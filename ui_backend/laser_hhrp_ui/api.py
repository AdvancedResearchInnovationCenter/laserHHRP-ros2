"""HTTP + WebSocket API v1 (plan section 7).

All endpoints are `async def` so they run on the loop thread with the engine (single-threaded state).
Request bodies are parsed and validated by hand (models.py), so the code does not depend on the
pydantic major version (laptop pip: v2, Ubuntu 24.04 apt: v1).

No endpoint starts or stops the physical scan. The /api/v1/dev/mock/* routes exist only when the
server runs with the mock source, never with the live ROS bridge. /api/v1/device/* are the operator's gyro
calibration and power off (device.py): refused during a scan, power off also while an analysis is pending.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response

from . import models
from .export import ExportError, UsbExporter, inspection_csv
from .ingress import watchdog
from .mock_ros import SCENARIOS
from .state import CommandError

log = logging.getLogger("laser_hhrp_ui.api")
P = "/api/v1"


def _error(status: int, code: str, message: str, field=None) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message, "field": field}}, status_code=status)


async def _body(request: Request) -> dict:
    try:
        data = await request.json()
    except Exception:
        raise models.ValidationError("body", "a JSON object is required") from None
    if not isinstance(data, dict):
        raise models.ValidationError("body", "a JSON object is required")
    return data


async def _run(fn, *args, **kwargs) -> JSONResponse:
    try:
        return JSONResponse(fn(*args, **kwargs))
    except models.ValidationError as e:
        return _error(400, "invalid", e.message, e.field)
    except CommandError as e:
        return _error(e.status, e.code, e.message, e.field)


def create_app(engine, hub, ingress, mock=None, static_dir: str | Path | None = None,
               watchdog_period_s: float = 0.5, services=(), usb: UsbExporter | None = None) -> FastAPI:
    """`services`: objects with blocking start()/stop() (ROS bridge, detector supervisor), started in order
    after the ingress is bound and stopped in reverse order at shutdown. `usb`: the removable-drive
    exporter (default: /media and /run/media)."""
    usb = usb or UsbExporter()
    tasks: list[asyncio.Task] = []

    @asynccontextmanager
    async def lifespan(app):
        loop = asyncio.get_running_loop()
        ingress.bind(loop)
        tasks.append(loop.create_task(ingress.run()))
        tasks.append(loop.create_task(watchdog(engine, watchdog_period_s)))
        if mock is not None:
            tasks.append(loop.create_task(mock.run()))
        started = []
        for svc in services:
            await loop.run_in_executor(None, svc.start)
            started.append(svc)
        engine.start()
        try:
            yield
        finally:
            for svc in reversed(started):
                try:
                    await loop.run_in_executor(None, svc.stop)
                except Exception:
                    log.exception("stopping %s failed", type(svc).__name__)
            for t in tasks + list(getattr(mock, "tasks", ())):
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            tasks.clear()

    app = FastAPI(title="Laser roller operator UI", version="0.1.0", lifespan=lifespan)

    # ---- state ----
    @app.get(P + "/health")
    async def health():
        db = engine.repo.health()
        return {"api": "ok", "source": engine.source, "database": db,
                "hardware_fresh": engine.hardware_fresh(),
                "detector": {"mode": engine.detector.mode, "alive": engine.detector.alive,
                             "ready": engine.detector.ready},
                "ingress_dropped": ingress.dropped, "event_seq": hub.seq}

    @app.get(P + "/state")
    async def state():
        return {**engine.snapshot(), "event_seq": hub.seq}

    # ---- commands ----
    @app.post(P + "/operator")
    async def operator(request: Request):
        try:
            body = await _body(request)
        except models.ValidationError as e:
            return _error(400, "invalid", e.message, e.field)
        return await _run(engine.set_operator, body.get("operator_id"))

    @app.post(P + "/operator/logout")
    async def logout():
        return await _run(engine.logout)

    @app.post(P + "/inspections")
    async def create_inspection(request: Request):
        try:
            body = await _body(request)
        except models.ValidationError as e:
            return _error(400, "invalid", e.message, e.field)
        return await _run(engine.create_inspection, body.get("mode"), body.get("label"), body.get("request_id"))

    @app.get(P + "/inspections")
    async def inspections(limit: int = 50):
        return engine.inspections(max(1, min(limit, 500)))

    @app.post(P + "/inspections/{uuid}/end")
    async def end_inspection(uuid: str):
        return await _run(engine.end_inspection, uuid)

    @app.patch(P + "/inspections/{uuid}/next-expected-count")
    async def next_expected_count(uuid: str, request: Request):
        try:
            body = await _body(request)
        except models.ValidationError as e:
            return _error(400, "invalid", e.message, e.field)
        return await _run(engine.set_next_expected_count, uuid, body.get("count"))

    @app.post(P + "/detector/restart")
    async def restart_detector():
        return await _run(engine.restart_detector)

    @app.get(P + "/inspections/{uuid}/scans")
    async def inspection_scans(uuid: str):
        return await _run(engine.scans, uuid)

    @app.get(P + "/scans/{scan_id}")
    async def scan(scan_id: str):
        return await _run(engine.scan_detail, scan_id)

    @app.post(P + "/scans/{scan_id}/decision")
    async def decision(scan_id: str, request: Request):
        try:
            body = await _body(request)
        except models.ValidationError as e:
            return _error(400, "invalid", e.message, e.field)
        return await _run(engine.decide, scan_id, body.get("decision"))

    @app.post(P + "/scans/{scan_id}/failure-action")
    async def failure_action(scan_id: str, request: Request):
        try:
            body = await _body(request)
        except models.ValidationError as e:
            return _error(400, "invalid", e.message, e.field)
        return await _run(engine.failure_action, scan_id, body.get("action"))

    # ---- device actions (device.py): the Engine checks, a worker thread carries them out ----
    @app.post(P + "/device/calibrate")
    async def calibrate():
        try:
            engine.check_calibrate()
        except CommandError as e:
            return _error(e.status, e.code, e.message, e.field)
        started, message = await asyncio.get_running_loop().run_in_executor(None, engine.device.calibrate)
        return await _run(engine.record_calibration, started, message)

    async def _power(action: str):
        try:
            first = engine.begin_power_off(action)
        except CommandError as e:
            return _error(e.status, e.code, e.message, e.field)
        if first:
            run = engine.device.power_off if action == "off" else engine.device.reboot
            ok, message = await asyncio.get_running_loop().run_in_executor(None, run)
            if not ok:
                log.error("%s failed: %s", action, message)
                engine.power_off_failed(message)
                return _error(500, "power_off_failed", message)
            log.warning("%s requested by the operator", "power off" if action == "off" else "reboot")
        return {"powering_off": True, "action": action}

    @app.post(P + "/device/power-off")
    async def power_off():
        return await _power("off")

    @app.post(P + "/device/reboot")
    async def reboot():
        return await _power("reboot")

    # ---- service menu (developers, PIN): network view, desktop mode, restart of the scanner software ----
    async def _service(request: Request):
        """The PIN from the body, checked; returns the service or an error response."""
        from .device import ServiceError
        svc = engine.service
        if svc is None or not svc.available:
            return None, _error(409, "unavailable", "The service menu is not set up on this device")
        try:
            body = await _body(request)
            svc.access.check(body.get("pin"))
        except models.ValidationError as e:
            return None, _error(400, "invalid", e.message, e.field)
        except ServiceError as e:
            engine._audit("service_pin_refused", code=e.code)
            return None, _error(e.status, e.code, e.message, "pin")
        return svc, None

    @app.post(P + "/device/service/unlock")
    async def service_unlock(request: Request):
        svc, err = await _service(request)
        if err:
            return err
        network = await asyncio.get_running_loop().run_in_executor(None, svc.network)
        return {"network": network}

    @app.post(P + "/device/service/{kind}")
    async def service_action(kind: str, request: Request):
        if kind not in ("desktop", "restart"):
            return _error(404, "not_found", f"unknown service action {kind}")
        svc, err = await _service(request)
        if err:
            return err
        try:
            first = engine.begin_switch(kind)
        except CommandError as e:
            return _error(e.status, e.code, e.message, e.field)
        if first:
            ok, message = await asyncio.get_running_loop().run_in_executor(None, svc.control.request, kind)
            if not ok:
                engine.switch_failed(message)
                return _error(500, "service_failed", message)
            log.warning("service: %s requested", kind)
        return {"switching": kind}

    # ---- export (plan sections 7, 8) ----
    @app.get(P + "/inspections/{uuid}/export.csv")
    async def export_csv(uuid: str):
        """Download only: builds the file in memory, writes nothing on the server."""
        try:
            name, text = inspection_csv(engine, uuid)
        except CommandError as e:
            return _error(e.status, e.code, e.message)
        return Response(text, media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.get(P + "/exports/usb")
    async def usb_targets():
        """Read-only list of approved mounted drives."""
        targets = await asyncio.get_running_loop().run_in_executor(None, usb.targets)
        return {"targets": [t.view() for t in targets], "roots": [str(r) for r in usb.roots]}

    @app.post(P + "/inspections/{uuid}/exports/usb")
    async def export_usb(uuid: str, request: Request):
        """Explicit server-side write of the inspection CSV to a listed USB drive; audited either way."""
        try:
            body = await _body(request)
            name, text = inspection_csv(engine, uuid)
        except models.ValidationError as e:
            return _error(400, "invalid", e.message, e.field)
        except CommandError as e:
            return _error(e.status, e.code, e.message)
        rows = text.count("\r\n") - 1
        mount = body.get("mount")
        try:
            path = await asyncio.get_running_loop().run_in_executor(None, usb.write, mount, name[:-4], text)
        except ExportError as e:
            engine.record_export(uuid, False, str(mount), e.message, rows)
            return _error(e.status, e.code, e.message, "mount")
        engine.record_export(uuid, True, path, None, rows)
        return {"path": path, "rows": rows}

    # ---- live events ----
    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        client = hub.subscribe()          # subscribe first: nothing between snapshot and stream is lost
        reader = asyncio.ensure_future(_read_until_closed(websocket))
        try:
            await websocket.send_json(hub.envelope("snapshot", engine.snapshot()))
            while True:
                getter = asyncio.ensure_future(client.queue.get())
                done, _ = await asyncio.wait({getter, reader}, return_when=asyncio.FIRST_COMPLETED)
                if reader in done:
                    getter.cancel()
                    break
                env = getter.result()
                if env is None:            # fell behind: reconnect for a fresh snapshot
                    await websocket.close(code=1013)
                    break
                await websocket.send_json(env)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            reader.cancel()
            hub.unsubscribe(client)

    # ---- mock-only developer controls ----
    if mock is not None:
        @app.get(P + "/dev/mock")
        async def mock_state():
            return mock.state()

        @app.post(P + "/dev/mock/press")
        async def mock_press():
            return {"scan_id": mock.press()}

        @app.post(P + "/dev/mock/release")
        async def mock_release():
            return {"scan_id": mock.release()}

        @app.post(P + "/dev/mock/fault")
        async def mock_fault(request: Request):
            try:
                mock.inject((await _body(request)).get("fault"))
            except (models.ValidationError, ValueError) as e:
                return _error(400, "invalid", str(e), "fault")
            return mock.state()

        @app.post(P + "/dev/mock/hardware")
        async def mock_hardware(request: Request):
            try:
                mock.set_hardware(**(await _body(request)))
            except (models.ValidationError, ValueError, TypeError) as e:
                return _error(400, "invalid", str(e))
            return mock.state()

        @app.post(P + "/dev/mock/power-on")
        async def mock_power_on():
            """The mock turns nothing off: end its 'turning off' so a check can go on."""
            engine.powering_off = False
            engine.power_action = "off"
            engine.switching = None
            engine._emit_system()
            return engine.device_view()

        @app.post(P + "/dev/mock/layout")
        async def mock_layout(request: Request):
            try:
                body = await _body(request)
            except models.ValidationError as e:
                return _error(400, "invalid", e.message, e.field)
            for key in ("targets", "non_targets", "rows"):
                if key in body:
                    v = body[key]
                    if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 500:
                        return _error(400, "invalid", f"{key} must be an integer 0..500", key)
                    setattr(mock, key, v)
            mock.rows = max(1, mock.rows)
            return mock.state()

        @app.post(P + "/dev/mock/scenario")
        async def mock_scenario(request: Request):
            try:
                name = (await _body(request)).get("name")
            except models.ValidationError as e:
                return _error(400, "invalid", e.message, e.field)
            if name not in SCENARIOS:
                return _error(400, "invalid", f"name must be one of {list(SCENARIOS)}", "name")
            task = asyncio.get_running_loop().create_task(mock.scenario(name))
            mock.tasks.add(task)
            task.add_done_callback(mock.tasks.discard)
            return {"started": name}

    if static_dir and Path(static_dir).is_dir():
        app.mount("/", _web_files(static_dir), name="web")

    return app


# Built frontend caching. Without a Cache-Control header the browser guesses that its saved page is still
# fresh and does not ask again, so an updated UI did not appear in the kiosk (whose profile survives
# reboots). The page is revalidated on every load (a 304 when unchanged); Vite puts a content hash in every
# name under assets/, so those files never change and are kept for good.
PAGE_CACHE = "no-cache"
ASSET_CACHE = "public, max-age=31536000, immutable"


def _web_files(directory):
    from fastapi.staticfiles import StaticFiles

    class WebFiles(StaticFiles):
        async def get_response(self, path, scope):
            response = await super().get_response(path, scope)
            if response.status_code in (200, 304):
                hashed = path.replace("\\", "/").startswith("assets/")
                response.headers["Cache-Control"] = ASSET_CACHE if hashed else PAGE_CACHE
            return response

    return WebFiles(directory=str(directory), html=True)


async def _read_until_closed(websocket: WebSocket) -> None:
    try:
        while True:
            await websocket.receive_text()     # clients send nothing meaningful; this detects close
    except Exception:
        return
