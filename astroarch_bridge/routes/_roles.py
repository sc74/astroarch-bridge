"""Helper per identificare device per ruolo dal loro set di property INDI.

Dato lo stato corrente (StateManager), risolviamo "il mount" come device che
espone EQUATORIAL_EOD_COORD; "la camera primaria" come quello che espone
CCD_EXPOSURE; "il focuser" ABS_FOCUS_POSITION; "il filter wheel" FILTER_SLOT.
Se ci sono più device candidati, accettiamo override via query/body `device=`.
"""
from __future__ import annotations

from fastapi import HTTPException

from ..state import StateManager

ROLE_PROPERTY = {
    "mount": "EQUATORIAL_EOD_COORD",
    "camera": "CCD_EXPOSURE",
    "focuser": "ABS_FOCUS_POSITION",
    "filter_wheel": "FILTER_SLOT",
    "guide_camera": "CCD_GUIDE_EXPOSURE",  # alcuni driver
    "weather": "WEATHER_PARAMETERS",
    "dome": "DOME_SHUTTER",
}


async def resolve_device(state: StateManager, role: str, override: str | None = None) -> str:
    if override:
        # verifica che esista
        devs = await state.list_devices()
        if override not in devs:
            raise HTTPException(status_code=404, detail=f"device {override!r} not connected")
        return override
    prop = ROLE_PROPERTY.get(role)
    if not prop:
        raise HTTPException(status_code=400, detail=f"unknown role {role}")
    candidates = await state.find_devices_by_role(prop)
    if not candidates:
        raise HTTPException(status_code=503, detail=f"no {role} device connected")
    if len(candidates) > 1:
        # ambiguo - chiedi al chiamante di specificare
        raise HTTPException(
            status_code=409,
            detail=f"multiple {role} devices: {candidates}. Pass ?device=NAME",
        )
    return candidates[0]


def first_element(prop: dict, name: str, default=None):
    for e in prop.get("elements", []):
        if e["name"] == name:
            return e.get("value", default)
    return default
