"""Route /api/indi: clone esatto della INDI Control Panel.

Espone:
- GET  /api/indi/devices  -> elenco device
- GET  /api/indi/devices/{device}/properties  -> tutte le property del device
- GET  /api/indi/devices/{device}/properties/{name}  -> singola
- POST /api/indi/devices/{device}/properties/{name}  -> set valori
- POST /api/indi/refresh  -> richiedi getProperties
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException

from ..auth import require_token
from ..deps import Bridge, get_bridge

router = APIRouter(prefix="/api/indi", tags=["indi"], dependencies=[Depends(require_token)])


@router.get("/devices")
async def list_devices(bridge: Bridge = Depends(get_bridge)) -> dict:
    return {"devices": await bridge.state.list_devices()}


@router.get("/devices/{device}/properties")
async def device_properties(device: str, bridge: Bridge = Depends(get_bridge)) -> dict:
    props = await bridge.state.get_device_properties(device)
    if not props:
        # device potrebbe non essere ancora arrivato; rispondiamo lista vuota (200)
        # piuttosto che 404, così la UI può comunque renderizzare stato "loading"
        return {"device": device, "properties": []}
    return {"device": device, "properties": props}


@router.get("/devices/{device}/properties/{name}")
async def get_property(device: str, name: str, bridge: Bridge = Depends(get_bridge)) -> dict:
    p = await bridge.state.get_property(device, name)
    if not p:
        raise HTTPException(status_code=404, detail="property not found")
    return p


@router.post("/devices/{device}/properties/{name}")
async def set_property(
    device: str,
    name: str,
    payload: dict[str, Any] = Body(..., example={"values": {"ON": True, "OFF": False}}),
    bridge: Bridge = Depends(get_bridge),
) -> dict:
    """Body: {"values": {elt_name: value, ...}}.
    Tipo determinato leggendo lo stato corrente della property.
    """
    p = await bridge.state.get_property(device, name)
    if not p:
        raise HTTPException(status_code=404, detail="property not found")
    values = payload.get("values")
    if not isinstance(values, dict):
        raise HTTPException(status_code=400, detail="missing 'values' dict")
    ptype = p.get("type")
    try:
        if ptype == "Switch":
            await bridge.indi.send_switch(device, name, {k: bool(v) for k, v in values.items()})
        elif ptype == "Number":
            await bridge.indi.send_number(device, name, {k: float(v) for k, v in values.items()})
        elif ptype == "Text":
            await bridge.indi.send_text(device, name, {k: str(v) for k, v in values.items()})
        else:
            raise HTTPException(status_code=400, detail=f"property type {ptype} not writable")
    except ConnectionError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {"ok": True}


@router.post("/refresh")
async def refresh(
    device: str = "",
    name: str = "",
    bridge: Bridge = Depends(get_bridge),
) -> dict:
    try:
        await bridge.indi.request_properties(device=device, name=name)
    except ConnectionError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {"ok": True}


@router.post("/devices/{device}/connect")
async def connect_device(device: str, bridge: Bridge = Depends(get_bridge)) -> dict:
    """Connette un driver INDI (Equivalente del bottone Connect in Ekos)."""
    try:
        await bridge.indi.send_switch(device, "CONNECTION", {"CONNECT": True, "DISCONNECT": False})
    except ConnectionError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {"ok": True}


@router.post("/devices/{device}/disconnect")
async def disconnect_device(device: str, bridge: Bridge = Depends(get_bridge)) -> dict:
    try:
        await bridge.indi.send_switch(device, "CONNECTION", {"CONNECT": False, "DISCONNECT": True})
    except ConnectionError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {"ok": True}
