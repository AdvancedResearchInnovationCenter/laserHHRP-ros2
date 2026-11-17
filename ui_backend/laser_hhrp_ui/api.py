"""HTTP + WebSocket API v1 (plan section 7).

All endpoints are `async def` so they run on the loop thread with the engine (single-threaded state).
Request bodies are parsed and validated by hand (models.py), so the code does not depend on the
pydantic major version (laptop pip: v2, Ubuntu 24.04 apt: v1).

No endpoint starts or stops the physical scan. The /api/v1/dev/mock/* routes exist only when the
server runs with the mock source, never with the live ROS bridge.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from . import models
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
               watchdog_period_s: float = 0.5) -> FastAPI:
    tasks: list[asyncio.Task] = []

    @asynccontextmanager
    async def lifespan(app):
        loop = asyncio.get_running_loop()
        ingress.bind(loop)
        tasks.append(loop.create_task(ingress.run()))
        tasks.append(loop.create_task(watchdog(engine, watchdog_period_s)))
        if mock is not None:
            tasks.append(loop.create_task(mock.run()))
        engine.start()
        try:
            yield
        finally:
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
        from fastapi.staticfiles import StaticFiles
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="web")

    return app


async def _read_until_closed(websocket: WebSocket) -> None:
    try:
        while True:
            await websocket.receive_text()     # clients send nothing meaningful; this detects close
    except Exception:
        return
