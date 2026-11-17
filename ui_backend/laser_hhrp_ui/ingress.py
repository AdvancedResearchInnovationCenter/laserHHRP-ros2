"""ROS thread -> asyncio loop hand-off (plan section 3).

`submit()` is safe from any thread and never blocks: ROS callbacks only enqueue immutable events.
The queue is bounded; overflow is counted and reported to the engine, which fails pending analyses
visibly instead of silently losing detections.
"""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger("laser_hhrp_ui.ingress")


class Ingress:
    def __init__(self, engine, maxsize: int = 20000):
        self.engine = engine
        self.maxsize = maxsize
        self.loop: asyncio.AbstractEventLoop | None = None
        self.queue: asyncio.Queue | None = None
        self.dropped = 0
        self._unreported = 0

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.queue = asyncio.Queue(self.maxsize)

    def submit(self, event) -> None:
        """Thread-safe, non-blocking."""
        loop = self.loop
        if loop is None or loop.is_closed():
            self.dropped += 1
            return
        loop.call_soon_threadsafe(self._put, event)

    def _put(self, event) -> None:
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            self.dropped += 1
            self._unreported += 1

    def _drain_one(self, event) -> None:
        if self._unreported:
            n, self._unreported = self._unreported, 0
            self.engine.ingress_overflow(n)
        try:
            self.engine.handle(event)
        except Exception as e:  # keep consuming; make the fault visible
            log.exception("event %s failed", type(event).__name__)
            self.engine._alert("event_error", f"Internal error handling {type(event).__name__}: {e}")

    async def run(self) -> None:
        while True:
            self._drain_one(await self.queue.get())

    async def drain(self) -> None:
        """Process everything queued so far (tests)."""
        await asyncio.sleep(0)
        while not self.queue.empty():
            self._drain_one(self.queue.get_nowait())
            await asyncio.sleep(0)


async def watchdog(engine, period_s: float = 0.5) -> None:
    while True:
        await asyncio.sleep(period_s)
        try:
            engine.tick()
        except Exception:
            log.exception("watchdog tick failed")
