"""Hub WebSocket: gestione client + backpressure.

DoD:
- Client manager con add/remove
- Coda per-client con dimensione massima -> drop oldest se piena (backpressure)
- Broadcast async senza bloccare lo state manager
- send JSON o bytes (per /ws/frames)

Errori prevenuti:
- E8: lista client thread-unsafe -> tutti i metodi sono asincroni e atomici
- E11: client lento -> drop oldest invece di backpressure totale (UI può perdere update intermedi senza congelare)
- close anomalo -> rimosso automaticamente in client task
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from starlette.websockets import WebSocket, WebSocketDisconnect

log = logging.getLogger(__name__)


class _Client:
    """Singolo client WebSocket con coda di outbound."""

    def __init__(self, ws: WebSocket, queue_size: int = 32):
        self.ws = ws
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=queue_size)
        self.task: Optional[asyncio.Task] = None
        self.alive = True

    async def writer_loop(self) -> None:
        try:
            while self.alive:
                item = await self.queue.get()
                if item is None:
                    break
                kind, payload = item
                if kind == "json":
                    await self.ws.send_text(payload)
                elif kind == "bytes":
                    await self.ws.send_bytes(payload)
        except (WebSocketDisconnect, ConnectionError, asyncio.CancelledError):
            pass
        except Exception:
            log.exception("ws writer crashed")
        finally:
            self.alive = False

    def enqueue(self, kind: str, payload) -> None:
        """Mette in coda; se piena scarta il più vecchio (drop oldest)."""
        if not self.alive:
            return
        try:
            self.queue.put_nowait((kind, payload))
        except asyncio.QueueFull:
            try:
                _ = self.queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                self.queue.put_nowait((kind, payload))
            except asyncio.QueueFull:
                pass


class WsHub:
    def __init__(self, max_clients: int, queue_size: int):
        self._clients: list[_Client] = []
        self._lock = asyncio.Lock()
        self._max_clients = max_clients
        self._queue_size = queue_size

    async def add(self, ws: WebSocket) -> Optional[_Client]:
        async with self._lock:
            if len(self._clients) >= self._max_clients:
                await ws.close(code=1013, reason="too many clients")
                return None
            client = _Client(ws, self._queue_size)
            self._clients.append(client)
        client.task = asyncio.create_task(client.writer_loop())
        return client

    async def remove(self, client: _Client) -> None:
        async with self._lock:
            if client in self._clients:
                self._clients.remove(client)
        client.alive = False
        try:
            client.queue.put_nowait(None)
        except asyncio.QueueFull:
            pass
        if client.task:
            try:
                await asyncio.wait_for(client.task, timeout=2.0)
            except asyncio.TimeoutError:
                client.task.cancel()
            except Exception:
                pass

    async def broadcast_json(self, obj: dict) -> None:
        if not self._clients:
            return
        payload = json.dumps(obj, separators=(",", ":"))
        for c in list(self._clients):
            c.enqueue("json", payload)

    async def broadcast_bytes(self, data: bytes) -> None:
        if not self._clients:
            return
        for c in list(self._clients):
            c.enqueue("bytes", data)

    @property
    def client_count(self) -> int:
        return len(self._clients)
