"""Route /api/filter_wheel."""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends

from ..auth import require_token
from ..deps import Bridge, get_bridge
from ._roles import first_element, resolve_device

router = APIRouter(prefix="/api/filter_wheel", tags=["filter_wheel"],
                   dependencies=[Depends(require_token)])


@router.get("/status")
async def status(device: str | None = None, bridge: Bridge = Depends(get_bridge)) -> dict:
    dev = await resolve_device(bridge.state, "filter_wheel", device)
    slot = await bridge.state.get_property(dev, "FILTER_SLOT") or {}
    names = await bridge.state.get_property(dev, "FILTER_NAME") or {}
    current = first_element(slot, "FILTER_SLOT_VALUE")
    name_list = [(e["name"], e.get("value")) for e in names.get("elements", [])]
    return {
        "device": dev,
        "current_slot": int(current) if current is not None else None,
        "moving": slot.get("state") == "Busy",
        "filters": [{"slot": i + 1, "name": v} for i, (_, v) in enumerate(name_list)],
        "max_slot": _max_for(slot, "FILTER_SLOT_VALUE"),
    }


@router.post("/select")
async def select(
    payload: dict = Body(..., example={"slot": 2}),
    bridge: Bridge = Depends(get_bridge),
) -> dict:
    dev = await resolve_device(bridge.state, "filter_wheel", payload.get("device"))
    slot = int(payload["slot"])
    await bridge.indi.send_number(dev, "FILTER_SLOT", {"FILTER_SLOT_VALUE": slot})
    return {"ok": True}


@router.post("/rename")
async def rename(
    payload: dict = Body(..., example={"names": ["L", "R", "G", "B", "Ha", "OIII", "SII"]}),
    bridge: Bridge = Depends(get_bridge),
) -> dict:
    dev = await resolve_device(bridge.state, "filter_wheel", payload.get("device"))
    names = list(payload["names"])
    p = await bridge.state.get_property(dev, "FILTER_NAME") or {}
    elt_names = [e["name"] for e in p.get("elements", [])]
    values = {n: names[i] for i, n in enumerate(elt_names) if i < len(names)}
    await bridge.indi.send_text(dev, "FILTER_NAME", values)
    return {"ok": True}


def _max_for(prop: dict, name: str):
    for e in prop.get("elements", []):
        if e["name"] == name:
            return e.get("max")
    return None
