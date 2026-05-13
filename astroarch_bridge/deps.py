"""Container condiviso e dependency provider per FastAPI."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from fastapi import Request

from .indi.client import IndiClient
from .phd2.client import Phd2Client
from .state import StateManager
from .ws.hub import WsHub


@dataclass
class Bridge:
    state: StateManager
    indi: IndiClient
    phd2: Phd2Client
    state_hub: WsHub
    frames_hub: WsHub
    images_dir: str


def get_bridge(request: Request) -> Bridge:
    return request.app.state.bridge
