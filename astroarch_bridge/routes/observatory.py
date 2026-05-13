"""Route /api/observatory: dome, weather, dust cap, flat panel.

Espone qualsiasi device con DOME_*, WEATHER_*, FLAT_LIGHT_*.
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends

from ..auth import require_token
from ..deps import Bridge, get_bridge

router = APIRouter(prefix="/api/observatory", tags=["observatory"],
                   dependencies=[Depends(require_token)])


@router.get("/status")
async def status(bridge: Bridge = Depends(get_bridge)) -> dict:
    devices = await bridge.state.list_devices()
    out: dict = {
        "weather": [], "dome": [], "dust_cap": [], "flat_panel": [],
        "candidates": {"weather": [], "dome": [], "dust_cap": [], "flat_panel": []},
    }
    for dev in devices:
        # Stato connessione del driver
        conn = await bridge.state.get_property(dev, "CONNECTION")
        is_conn = False
        if conn:
            for e in conn.get("elements", []):
                if e["name"] == "CONNECT" and e.get("value"):
                    is_conn = True
                    break

        # --- Weather ---
        wp = await bridge.state.get_property(dev, "WEATHER_PARAMETERS")
        if wp:
            out["weather"].append({
                "device": dev,
                "parameters": [
                    {"name": e["name"], "label": e.get("label"), "value": e.get("value")}
                    for e in wp.get("elements", [])
                ],
                "state": wp.get("state"),
                "connected": is_conn,
            })
        else:
            # Heuristic: nome contiene weather/sky/cloud
            dl = dev.lower()
            if any(k in dl for k in ("weather", "sky", "cloud", "watcher")):
                out["candidates"]["weather"].append({"device": dev, "connected": is_conn})

        # --- Dome ---
        # 1) standard DOME_SHUTTER
        ds = await bridge.state.get_property(dev, "DOME_SHUTTER")
        # 2) scripting gateway / roll-off: DOME_PARK + DOME_MOTION
        dp = await bridge.state.get_property(dev, "DOME_PARK")
        dm = await bridge.state.get_property(dev, "DOME_MOTION")
        dome_match = ds or dp or dm
        if dome_match:
            shutter_state = None
            if ds:
                shutter_state = _selected_switch(ds)
            elif dp:
                # Roll-off: PARK=closed, UNPARK=open
                if _is_on(dp, "PARK"):
                    shutter_state = "PARKED"
                elif _is_on(dp, "UNPARK"):
                    shutter_state = "UNPARKED"
            motion_state = None
            if dm:
                motion_state = _selected_switch(dm)
            out["dome"].append({
                "device": dev,
                "shutter": shutter_state,
                "motion": motion_state,
                "has_shutter": ds is not None,
                "has_park": dp is not None,
                "state": (ds or dp or dm).get("state"),
                "connected": is_conn,
            })
        else:
            dl = dev.lower()
            if any(k in dl for k in ("dome", "roll", "shutter", "roof")):
                out["candidates"]["dome"].append({"device": dev, "connected": is_conn})

        # --- Dust cap ---
        dc = await bridge.state.get_property(dev, "CAP_PARK")
        if dc:
            out["dust_cap"].append({
                "device": dev,
                "parked": _is_on(dc, "PARK"),
                "connected": is_conn,
            })
        else:
            dl = dev.lower()
            if any(k in dl for k in ("flip", "cap", "dust")):
                out["candidates"]["dust_cap"].append({"device": dev, "connected": is_conn})

        # --- Flat panel ---
        fl = await bridge.state.get_property(dev, "FLAT_LIGHT_CONTROL")
        if fl:
            br = await bridge.state.get_property(dev, "FLAT_LIGHT_INTENSITY") or {}
            out["flat_panel"].append({
                "device": dev,
                "on": _is_on(fl, "FLAT_LIGHT_ON"),
                "intensity": _first(br, "FLAT_LIGHT_INTENSITY_VALUE"),
                "connected": is_conn,
            })
        else:
            dl = dev.lower()
            if any(k in dl for k in ("flat", "panel", "alnitak")):
                out["candidates"]["flat_panel"].append({"device": dev, "connected": is_conn})

    return out


@router.post("/dome/shutter")
async def dome_shutter(
    payload: dict = Body(..., example={"device": "Dome Simulator", "open": True}),
    bridge: Bridge = Depends(get_bridge),
) -> dict:
    """Apri/chiudi dome shutter. Gestisce sia DOME_SHUTTER (standard)
    che DOME_PARK (scripting/roll-off) come fallback.
    """
    dev = payload["device"]
    open_ = bool(payload.get("open", True))
    # Prova DOME_SHUTTER
    ds = await bridge.state.get_property(dev, "DOME_SHUTTER")
    if ds:
        await bridge.indi.send_switch(dev, "DOME_SHUTTER", {
            "SHUTTER_OPEN": open_, "SHUTTER_CLOSE": not open_,
        })
        return {"ok": True, "via": "DOME_SHUTTER"}
    # Fallback: DOME_PARK (scripting/roll-off — open=UNPARK, close=PARK)
    dp = await bridge.state.get_property(dev, "DOME_PARK")
    if dp:
        await bridge.indi.send_switch(dev, "DOME_PARK", {
            "PARK": not open_, "UNPARK": open_,
        })
        return {"ok": True, "via": "DOME_PARK"}
    raise HTTPException(status_code=503,
                        detail="Driver dome non espone DOME_SHUTTER né DOME_PARK")


@router.post("/dome/abort")
async def dome_abort(
    payload: dict = Body(..., example={"device": "Dome"}),
    bridge: Bridge = Depends(get_bridge),
) -> dict:
    dev = payload["device"]
    await bridge.indi.send_switch(dev, "DOME_ABORT_MOTION", {"ABORT": True})
    return {"ok": True}


@router.post("/dust_cap")
async def dust_cap(
    payload: dict = Body(..., example={"device": "FlipFlat", "park": False}),
    bridge: Bridge = Depends(get_bridge),
) -> dict:
    dev = payload["device"]
    park = bool(payload.get("park", False))
    await bridge.indi.send_switch(dev, "CAP_PARK", {"PARK": park, "UNPARK": not park})
    return {"ok": True}


@router.post("/flat_panel")
async def flat_panel(
    payload: dict = Body(..., example={"device": "FlipFlat", "on": True, "intensity": 100}),
    bridge: Bridge = Depends(get_bridge),
) -> dict:
    dev = payload["device"]
    on = bool(payload.get("on", True))
    await bridge.indi.send_switch(dev, "FLAT_LIGHT_CONTROL", {
        "FLAT_LIGHT_ON": on, "FLAT_LIGHT_OFF": not on,
    })
    if "intensity" in payload:
        await bridge.indi.send_number(dev, "FLAT_LIGHT_INTENSITY", {
            "FLAT_LIGHT_INTENSITY_VALUE": float(payload["intensity"]),
        })
    return {"ok": True}


def _selected_switch(prop: dict) -> str | None:
    for e in prop.get("elements", []):
        if e.get("value") is True:
            return e["name"]
    return None


def _is_on(prop: dict, name: str) -> bool:
    for e in prop.get("elements", []):
        if e["name"] == name:
            return bool(e.get("value"))
    return False


def _first(prop: dict, name: str):
    for e in prop.get("elements", []):
        if e["name"] == name:
            return e.get("value")
    return None
