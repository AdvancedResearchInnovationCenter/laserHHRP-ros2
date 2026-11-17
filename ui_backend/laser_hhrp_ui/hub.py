"""UI event fan-out (plan section 7): versioned envelopes with a monotonic event_seq.

Runs on the asyncio loop thread only. A client that falls behind is dropped (its queue gets a None
sentinel) and reconnects for a fresh snapshot instead of receiving a gapped stream.
"""

from __future__ import annotations

import asyncio

from .clock import Clock, iso

VERSION = 1


class Client:
    def __init__(self, maxsize: int):
        self.queue: asyncio.Queue = asyncio.Queue(maxsize)
        self.lagged = False


class EventHub:
    def __init__(self, clock: Clock | None = None, client_queue: int = 2000):
        self.clock = clock or Clock()
        self.seq = 0
        self.clients: set[Client] = set()
        self.client_queue = client_queue

    def envelope(self, type_: str, payload, scan_id=None, inspection_id=None, seq=None) -> dict:
        return {"version": VERSION, "event_seq": self.seq if seq is None else seq, "type": type_,
                "timestamp": iso(self.clock.now()), "inspection_id": inspection_id, "scan_id": scan_id,
                "payload": payload}

    def publish(self, type_: str, payload, scan_id=None, inspection_id=None) -> dict:
        self.seq += 1
        env = self.envelope(type_, payload, scan_id, inspection_id, self.seq)
        for c in list(self.clients):
            if c.lagged:
                continue
            try:
                c.queue.put_nowait(env)
            except asyncio.QueueFull:
                c.lagged = True
                while not c.queue.empty():
                    c.queue.get_nowait()
                c.queue.put_nowait(None)
        return env

    def subscribe(self) -> Client:
        c = Client(self.client_queue)
        self.clients.add(c)
        return c

    def unsubscribe(self, c: Client) -> None:
        self.clients.discard(c)
