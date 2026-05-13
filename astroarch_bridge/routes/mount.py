"""Route /api/mount: telescopio."""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException

from ..auth import require_token
from ..deps import Bridge, get_bridge
from ._roles import first_element, resolve_device

router = APIRouter(prefix="/api/mount", tags=["mount"], dependencies=[Depends(require_token)])


@router.get("/status")
async def status(device: str | None = None, bridge: Bridge = Depends(get_bridge)) -> dict:
    dev = await resolve_device(bridge.state, "mount", device)
    coord = await bridge.state.get_property(dev, "EQUATORIAL_EOD_COORD") or {}
    track = await bridge.state.get_property(dev, "TELESCOPE_TRACK_STATE") or {}
    park = await bridge.state.get_property(dev, "TELESCOPE_PARK") or {}
    pier = await bridge.state.get_property(dev, "TELESCOPE_PIER_SIDE") or {}
    track_mode = await bridge.state.get_property(dev, "TELESCOPE_TRACK_MODE") or {}
    return {
        "device": dev,
        "ra": first_element(coord, "RA"),
        "dec": first_element(coord, "DEC"),
        "tracking": first_element(track, "TRACK_ON", False),
        "parked": first_element(park, "PARK", False),
        "pier_side": "west" if first_element(pier, "PIER_WEST") else
                      "east" if first_element(pier, "PIER_EAST") else None,
        "track_mode": _selected_switch(track_mode),
        "raw_state": coord.get("state"),
    }


@router.post("/goto")
async def goto(
    payload: dict = Body(..., example={"ra": 0.7178, "dec": 41.269, "device": None}),
    bridge: Bridge = Depends(get_bridge),
) -> dict:
    dev = await resolve_device(bridge.state, "mount", payload.get("device"))
    ra = float(payload["ra"])
    dec = float(payload["dec"])
    # ON_COORD_SET: SLEW=Slew (no track), TRACK=Slew+track, SYNC=sync only
    action = (payload.get("action") or "track").lower()
    coord_set_map = {"slew": "SLEW", "track": "TRACK", "sync": "SYNC"}
    target_sw = coord_set_map.get(action)
    if not target_sw:
        raise HTTPException(status_code=400, detail="action must be slew|track|sync")
    try:
        await bridge.indi.send_switch(dev, "ON_COORD_SET", {
            "SLEW": target_sw == "SLEW",
            "TRACK": target_sw == "TRACK",
            "SYNC": target_sw == "SYNC",
        })
        await bridge.indi.send_number(dev, "EQUATORIAL_EOD_COORD", {"RA": ra, "DEC": dec})
    except ConnectionError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {"ok": True, "device": dev, "ra": ra, "dec": dec, "action": action}


@router.post("/park")
async def park(device: str | None = None, bridge: Bridge = Depends(get_bridge)) -> dict:
    dev = await resolve_device(bridge.state, "mount", device)
    await bridge.indi.send_switch(dev, "TELESCOPE_PARK", {"PARK": True, "UNPARK": False})
    return {"ok": True}


@router.post("/unpark")
async def unpark(device: str | None = None, bridge: Bridge = Depends(get_bridge)) -> dict:
    dev = await resolve_device(bridge.state, "mount", device)
    await bridge.indi.send_switch(dev, "TELESCOPE_PARK", {"PARK": False, "UNPARK": True})
    return {"ok": True}


@router.post("/abort")
async def abort(device: str | None = None, bridge: Bridge = Depends(get_bridge)) -> dict:
    dev = await resolve_device(bridge.state, "mount", device)
    await bridge.indi.send_switch(dev, "TELESCOPE_ABORT_MOTION", {"ABORT": True})
    return {"ok": True}


@router.post("/track")
async def set_tracking(
    payload: dict = Body(..., example={"on": True, "mode": "TRACK_SIDEREAL"}),
    bridge: Bridge = Depends(get_bridge),
) -> dict:
    dev = await resolve_device(bridge.state, "mount", payload.get("device"))
    on = bool(payload.get("on", True))
    mode = payload.get("mode")  # TRACK_SIDEREAL | TRACK_LUNAR | TRACK_SOLAR | TRACK_CUSTOM
    if mode:
        modes = {"TRACK_SIDEREAL": False, "TRACK_LUNAR": False,
                 "TRACK_SOLAR": False, "TRACK_CUSTOM": False}
        if mode not in modes:
            raise HTTPException(status_code=400, detail="invalid track mode")
        modes[mode] = True
        await bridge.indi.send_switch(dev, "TELESCOPE_TRACK_MODE", modes)
    await bridge.indi.send_switch(dev, "TELESCOPE_TRACK_STATE", {
        "TRACK_ON": on, "TRACK_OFF": not on,
    })
    return {"ok": True}


@router.post("/slew")
async def slew(
    payload: dict = Body(..., example={"direction": "N", "active": True}),
    bridge: Bridge = Depends(get_bridge),
) -> dict:
    """Slew manuale N/S/E/W. active=True parte il movimento, False lo ferma."""
    dev = await resolve_device(bridge.state, "mount", payload.get("device"))
    direction = (payload.get("direction") or "").upper()
    active = bool(payload.get("active", True))
    if direction in ("N", "S"):
        await bridge.indi.send_switch(dev, "TELESCOPE_MOTION_NS", {
            "MOTION_NORTH": active and direction == "N",
            "MOTION_SOUTH": active and direction == "S",
        })
    elif direction in ("E", "W"):
        await bridge.indi.send_switch(dev, "TELESCOPE_MOTION_WE", {
            "MOTION_WEST": active and direction == "W",
            "MOTION_EAST": active and direction == "E",
        })
    else:
        raise HTTPException(status_code=400, detail="direction must be N|S|E|W")
    return {"ok": True}


@router.post("/slew_rate")
async def slew_rate(
    payload: dict = Body(..., example={"rate": "SLEW_GUIDE"}),
    bridge: Bridge = Depends(get_bridge),
) -> dict:
    """Imposta rate di slew. Rate name dipende dal driver (es SLEW_GUIDE, SLEW_CENTERING, SLEW_FIND, SLEW_MAX)."""
    dev = await resolve_device(bridge.state, "mount", payload.get("device"))
    rate_name = payload.get("rate")
    if not rate_name:
        raise HTTPException(status_code=400, detail="missing 'rate'")
    # Costruzione: leggi prop TELESCOPE_SLEW_RATE per conoscere nomi rate
    p = await bridge.state.get_property(dev, "TELESCOPE_SLEW_RATE")
    if not p:
        raise HTTPException(status_code=503, detail="TELESCOPE_SLEW_RATE not available")
    values = {e["name"]: (e["name"] == rate_name) for e in p.get("elements", [])}
    if not any(values.values()):
        raise HTTPException(status_code=400, detail=f"unknown rate {rate_name}")
    await bridge.indi.send_switch(dev, "TELESCOPE_SLEW_RATE", values)
    return {"ok": True}


def _selected_switch(prop: dict) -> str | None:
    for e in prop.get("elements", []):
        if e.get("value") is True:
            return e["name"]
    return None
