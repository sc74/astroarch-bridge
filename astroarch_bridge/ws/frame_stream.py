"""WS endpoint /ws/frames -> stream JPEG dei nuovi FITS.

Protocollo per ogni frame:
1. Frame header JSON (text): {"type":"frame_meta", "size": N, ...metadata}
2. Frame body binario (bytes): JPEG payload di N bytes

Il client legge sempre alternato testo+binario.
"""
from __future__ import annotations

import json
import logging

from starlette.websockets import WebSocket, WebSocketDisconnect

from ..auth import authenticate_ws
from .hub import WsHub

log = logging.getLogger(__name__)


def make_frame_listener(hub: WsHub):
    async def _listener(meta: dict, jpeg: bytes) -> None:
        header = {"type": "frame_meta", "size": len(jpeg), **meta}
        await hub.broadcast_json(header)
        await hub.broadcast_bytes(jpeg)
    return _listener


async def frame_ws_endpoint(ws: WebSocket, hub: WsHub) -> None:
    await ws.accept()
    if not await authenticate_ws(ws):
        return
    client = await hub.add(ws)
    if client is None:
        return
    try:
        while client.alive:
            try:
                msg = await ws.receive_text()
                if msg == "ping":
                    client.enqueue("json", '{"type":"pong"}')
            except WebSocketDisconnect:
                break
    finally:
        await hub.remove(client)
