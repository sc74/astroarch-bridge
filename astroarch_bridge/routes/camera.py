"""Route /api/camera: camera CCD/CMOS."""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException

from ..auth import require_token
from ..deps import Bridge, get_bridge
from ._roles import first_element, resolve_device

router = APIRouter(prefix="/api/camera", tags=["camera"], dependencies=[Depends(require_token)])


@router.get("/status")
async def status(device: str | None = None, bridge: Bridge = Depends(get_bridge)) -> dict:
    dev = await resolve_device(bridge.state, "camera", device)
    exposure = await bridge.state.get_property(dev, "CCD_EXPOSURE") or {}
    temp = await bridge.state.get_property(dev, "CCD_TEMPERATURE") or {}
    cooler = await bridge.state.get_property(dev, "CCD_COOLER") or {}
    cooler_power = await bridge.state.get_property(dev, "CCD_COOLER_POWER") or {}
    binning = await bridge.state.get_property(dev, "CCD_BINNING") or {}
    offset = await bridge.state.get_property(dev, "CCD_OFFSET") or {}
    frame_type = await bridge.state.get_property(dev, "CCD_FRAME_TYPE") or {}
    # Gain: ZWO/QHY usano CCD_GAIN.GAIN, ToupTek/SVBony usano CCD_CONTROLS.Gain
    gain_val, gain_prop, gain_elt = await _resolve_gain(bridge, dev)
    # Target T: utile esporre quello che il driver ha registrato come setpoint
    # ToupTek mostra in CCD_TEMPERATURE_VALUE la temp attuale (read), e usa lo stesso
    # element come setpoint (write). Il "target" non è interrogabile separatamente
    # via INDI standard, va memorizzato lato app.
    return {
        "device": dev,
        "exposure_remaining": first_element(exposure, "CCD_EXPOSURE_VALUE"),
        "exposure_state": exposure.get("state"),
        "temperature": first_element(temp, "CCD_TEMPERATURE_VALUE"),
        "temperature_state": temp.get("state"),  # Busy = sta variando verso target
        "cooler_on": first_element(cooler, "COOLER_ON", False),
        "cooler_power": first_element(cooler_power, "COOLER_POWER"),  # FIX
        "bin_x": first_element(binning, "HOR_BIN", 1),
        "bin_y": first_element(binning, "VER_BIN", 1),
        "gain": gain_val,
        "gain_property": gain_prop,
        "gain_element": gain_elt,
        "offset": first_element(offset, "OFFSET"),
        "frame_type": _selected_switch(frame_type),
    }


async def _resolve_gain(bridge: Bridge, dev: str) -> tuple[float | None, str | None, str | None]:
    """Ritorna (value, property_name, element_name) per il gain del device.
    Cerca tra: CCD_GAIN.GAIN (ZWO/QHY) e CCD_CONTROLS.Gain (ToupTek/SVBony).
    """
    candidates = [
        ("CCD_GAIN", "GAIN"),
        ("CCD_CONTROLS", "Gain"),
        ("CCD_CONTROLS", "GAIN"),
    ]
    for prop_name, elt_name in candidates:
        p = await bridge.state.get_property(dev, prop_name)
        if not p:
            continue
        for e in p.get("elements", []):
            if e["name"].lower() == elt_name.lower():
                return e.get("value"), prop_name, e["name"]
    return None, None, None


@router.post("/expose")
async def expose(
    payload: dict = Body(..., example={"seconds": 180.0}),
    bridge: Bridge = Depends(get_bridge),
) -> dict:
    """Avvia esposizione. Configura upload mode (BOTH) e dir prima dello scatto
    in modo che il file FITS venga salvato su disco AnchE quando Ekos è collegato
    come client (mode CLIENT default toupbase = nessun salvataggio).
    """
    dev = await resolve_device(bridge.state, "camera", payload.get("device"))
    seconds = float(payload["seconds"])
    if seconds < 0 or seconds > 7200:
        raise HTTPException(status_code=400, detail="seconds out of range [0, 7200]")

    # Setup upload se non già configurato BOTH/LOCAL
    auto_upload = bool(payload.get("auto_upload_setup", True))
    if auto_upload:
        await _ensure_upload_local(bridge, dev, payload.get("upload_dir"),
                                    payload.get("upload_prefix"))

    await bridge.indi.send_number(dev, "CCD_EXPOSURE", {"CCD_EXPOSURE_VALUE": seconds})
    return {"ok": True, "device": dev, "seconds": seconds}


async def _ensure_upload_local(bridge: Bridge, dev: str,
                               upload_dir: str | None = None,
                               upload_prefix: str | None = None) -> None:
    """Configura UPLOAD_MODE=BOTH e UPLOAD_DIR su Ekos pictures dir."""
    from pathlib import Path
    from ..config import get_settings
    settings = get_settings()
    target_dir = upload_dir or str(settings.images_dir / "AstroarchInterface")
    Path(target_dir).mkdir(parents=True, exist_ok=True)
    target_prefix = upload_prefix or "IMG_XXX"

    # Property può essere "UPLOAD_MODE" (toupbase, default INDI) o "CCD_UPLOAD_MODE" (alcuni driver)
    for prop_name in ("UPLOAD_MODE", "CCD_UPLOAD_MODE"):
        p = await bridge.state.get_property(dev, prop_name)
        if not p:
            continue
        # Vedi se già in BOTH o LOCAL
        is_local_or_both = False
        for e in p.get("elements", []):
            if e["name"] in ("UPLOAD_LOCAL", "UPLOAD_BOTH") and e.get("value"):
                is_local_or_both = True
                break
        if not is_local_or_both:
            try:
                await bridge.indi.send_switch(dev, prop_name, {
                    "UPLOAD_CLIENT": False,
                    "UPLOAD_LOCAL": False,
                    "UPLOAD_BOTH": True,
                })
            except Exception:
                pass
        break

    for prop_name in ("UPLOAD_SETTINGS", "CCD_UPLOAD_SETTINGS"):
        p = await bridge.state.get_property(dev, prop_name)
        if not p:
            continue
        try:
            await bridge.indi.send_text(dev, prop_name, {
                "UPLOAD_DIR": target_dir,
                "UPLOAD_PREFIX": target_prefix,
            })
        except Exception:
            pass
        break


@router.post("/upload_setup")
async def upload_setup(
    payload: dict = Body(default={}),
    bridge: Bridge = Depends(get_bridge),
) -> dict:
    """Configura manualmente upload mode e dir/prefix per la camera."""
    dev = await resolve_device(bridge.state, "camera", payload.get("device"))
    mode = payload.get("mode", "BOTH").upper()  # CLIENT|LOCAL|BOTH
    valid = {"CLIENT", "LOCAL", "BOTH"}
    if mode not in valid:
        raise HTTPException(status_code=400, detail=f"mode must be one of {valid}")
    for prop_name in ("UPLOAD_MODE", "CCD_UPLOAD_MODE"):
        p = await bridge.state.get_property(dev, prop_name)
        if p:
            await bridge.indi.send_switch(dev, prop_name, {
                "UPLOAD_CLIENT": mode == "CLIENT",
                "UPLOAD_LOCAL": mode == "LOCAL",
                "UPLOAD_BOTH": mode == "BOTH",
            })
            break
    if payload.get("dir") or payload.get("prefix"):
        for prop_name in ("UPLOAD_SETTINGS", "CCD_UPLOAD_SETTINGS"):
            p = await bridge.state.get_property(dev, prop_name)
            if p:
                values = {}
                if payload.get("dir"):
                    values["UPLOAD_DIR"] = str(payload["dir"])
                if payload.get("prefix"):
                    values["UPLOAD_PREFIX"] = str(payload["prefix"])
                await bridge.indi.send_text(dev, prop_name, values)
                break
    return {"ok": True, "device": dev, "mode": mode}


@router.post("/transfer_format")
async def transfer_format(
    payload: dict = Body(..., example={"format": "FITS"}),
    bridge: Bridge = Depends(get_bridge),
) -> dict:
    """Formato del file trasferito: FITS / NATIVE / XISF."""
    dev = await resolve_device(bridge.state, "camera", payload.get("device"))
    fmt = payload.get("format", "FITS").upper()
    valid = {"FITS", "NATIVE", "XISF"}
    if fmt not in valid:
        raise HTTPException(status_code=400, detail=f"format must be one of {valid}")
    await bridge.indi.send_switch(dev, "CCD_TRANSFER_FORMAT", {
        "FORMAT_FITS": fmt == "FITS",
        "FORMAT_NATIVE": fmt == "NATIVE",
        "FORMAT_XISF": fmt == "XISF",
    })
    return {"ok": True}


@router.post("/capture_format")
async def capture_format(
    payload: dict = Body(..., example={"format": "RAW"}),
    bridge: Bridge = Depends(get_bridge),
) -> dict:
    """Formato sensore (per camere color): RAW (mosaic Bayer) / RGB (debayered)."""
    dev = await resolve_device(bridge.state, "camera", payload.get("device"))
    fmt = payload.get("format", "RAW").upper()
    valid = {"RAW", "RGB"}
    if fmt not in valid:
        raise HTTPException(status_code=400, detail=f"format must be one of {valid}")
    await bridge.indi.send_switch(dev, "CCD_CAPTURE_FORMAT", {
        "INDI_RAW": fmt == "RAW",
        "INDI_RGB": fmt == "RGB",
    })
    return {"ok": True}


@router.post("/abort")
async def abort(device: str | None = None, bridge: Bridge = Depends(get_bridge)) -> dict:
    dev = await resolve_device(bridge.state, "camera", device)
    await bridge.indi.send_switch(dev, "CCD_ABORT_EXPOSURE", {"ABORT": True})
    return {"ok": True}


@router.post("/cooler")
async def cooler(
    payload: dict = Body(..., example={"on": True, "target": -10.0}),
    bridge: Bridge = Depends(get_bridge),
) -> dict:
    dev = await resolve_device(bridge.state, "camera", payload.get("device"))
    on = bool(payload.get("on", True))
    target = payload.get("target")
    await bridge.indi.send_switch(dev, "CCD_COOLER", {"COOLER_ON": on, "COOLER_OFF": not on})
    if target is not None:
        await bridge.indi.send_number(dev, "CCD_TEMPERATURE", {"CCD_TEMPERATURE_VALUE": float(target)})
    return {"ok": True}


@router.post("/binning")
async def binning(
    payload: dict = Body(..., example={"x": 1, "y": 1}),
    bridge: Bridge = Depends(get_bridge),
) -> dict:
    dev = await resolve_device(bridge.state, "camera", payload.get("device"))
    x = int(payload.get("x", 1))
    y = int(payload.get("y", x))
    await bridge.indi.send_number(dev, "CCD_BINNING", {"HOR_BIN": x, "VER_BIN": y})
    return {"ok": True}


@router.post("/gain")
async def set_gain(
    payload: dict = Body(..., example={"value": 100}),
    bridge: Bridge = Depends(get_bridge),
) -> dict:
    dev = await resolve_device(bridge.state, "camera", payload.get("device"))
    value = float(payload["value"])
    _, prop_name, elt_name = await _resolve_gain(bridge, dev)
    if not prop_name or not elt_name:
        # Fallback: prova entrambi senza errori
        try:
            await bridge.indi.send_number(dev, "CCD_GAIN", {"GAIN": value})
            return {"ok": True, "via": "CCD_GAIN.GAIN"}
        except Exception:
            pass
        raise HTTPException(status_code=404, detail="No gain property found on this camera")
    await bridge.indi.send_number(dev, prop_name, {elt_name: value})
    return {"ok": True, "via": f"{prop_name}.{elt_name}"}


@router.post("/offset")
async def set_offset(
    payload: dict = Body(..., example={"value": 50}),
    bridge: Bridge = Depends(get_bridge),
) -> dict:
    dev = await resolve_device(bridge.state, "camera", payload.get("device"))
    await bridge.indi.send_number(dev, "CCD_OFFSET", {"OFFSET": float(payload["value"])})
    return {"ok": True}


@router.post("/frame_type")
async def frame_type(
    payload: dict = Body(..., example={"type": "FRAME_LIGHT"}),
    bridge: Bridge = Depends(get_bridge),
) -> dict:
    dev = await resolve_device(bridge.state, "camera", payload.get("device"))
    valid = {"FRAME_LIGHT", "FRAME_DARK", "FRAME_FLAT", "FRAME_BIAS"}
    t = payload.get("type")
    if t not in valid:
        raise HTTPException(status_code=400, detail=f"type must be one of {valid}")
    await bridge.indi.send_switch(dev, "CCD_FRAME_TYPE", {k: (k == t) for k in valid})
    return {"ok": True}


def _selected_switch(prop: dict) -> str | None:
    for e in prop.get("elements", []):
        if e.get("value") is True:
            return e["name"]
    return None
