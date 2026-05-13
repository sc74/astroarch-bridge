"""Route /api/guide: PHD2."""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Body, Depends, HTTPException

from ..auth import require_token
from ..deps import Bridge, get_bridge
from ..phd2.client import Phd2RpcError

router = APIRouter(prefix="/api/guide", tags=["guide"], dependencies=[Depends(require_token)])
_logger = logging.getLogger("astroarch_bridge.guide")


def _phd2_http_error(op: str, e: BaseException) -> HTTPException:
    """Mappa eccezioni PHD2 a HTTPException con status code coerenti.
    Risolve il problema "Internal Server Error" quando PHD2 va in timeout
    o è in stato strano: invece di lasciar propagare l'eccezione (che
    diventa 500), ritorniamo 504/503/422 con un detail leggibile."""
    if isinstance(e, asyncio.TimeoutError):
        _logger.warning("PHD2 timeout on %s", op)
        return HTTPException(status_code=504,
            detail=f"PHD2 timeout su {op}. PHD2 è in stato bloccato? "
                   f"Verifica sul desktop che il server sia avviato e "
                   f"che nessun dialog modale stia bloccando.")
    if isinstance(e, Phd2RpcError):
        _logger.warning("PHD2 RPC error on %s: %s", op, e)
        return HTTPException(status_code=422, detail=f"PHD2: {e}")
    if isinstance(e, ConnectionError):
        _logger.warning("PHD2 not reachable on %s: %s", op, e)
        return HTTPException(status_code=503,
            detail=f"PHD2 non raggiungibile. Avvia PHD2 e abilita il server.")
    _logger.exception("PHD2 unexpected error on %s", op)
    return HTTPException(status_code=500,
        detail=f"Errore inatteso su {op}: {type(e).__name__}: {e}")


@router.get("/status")
async def status(bridge: Bridge = Depends(get_bridge)) -> dict:
    return {
        "connection": bridge.phd2.state,
        "live": dict(bridge.phd2.live),
    }


@router.post("/start")
async def start(
    payload: dict = Body(default={}),
    bridge: Bridge = Depends(get_bridge),
) -> dict:
    try:
        result = await bridge.phd2.start_guiding(
            settle_pixels=float(payload.get("settle_pixels", 1.5)),
            settle_time=float(payload.get("settle_time", 10.0)),
            settle_timeout=float(payload.get("settle_timeout", 60.0)),
        )
    except Exception as e:
        raise _phd2_http_error("start_guiding", e)
    return {"ok": True, "result": result}


@router.post("/stop")
async def stop(bridge: Bridge = Depends(get_bridge)) -> dict:
    try:
        await bridge.phd2.stop_capture()
    except Exception as e:
        raise _phd2_http_error("stop", e)
    return {"ok": True}


@router.post("/dither")
async def dither(
    payload: dict = Body(default={}),
    bridge: Bridge = Depends(get_bridge),
) -> dict:
    try:
        result = await bridge.phd2.dither(
            amount=float(payload.get("amount", 3.0)),
            ra_only=bool(payload.get("ra_only", False)),
            settle_pixels=float(payload.get("settle_pixels", 1.5)),
            settle_time=float(payload.get("settle_time", 10.0)),
            settle_timeout=float(payload.get("settle_timeout", 60.0)),
        )
    except Exception as e:
        raise _phd2_http_error("dither", e)
    return {"ok": True, "result": result}


@router.post("/loop")
async def loop_(bridge: Bridge = Depends(get_bridge)) -> dict:
    try:
        await bridge.phd2.loop()
    except Exception as e:
        raise _phd2_http_error("loop", e)
    return {"ok": True}


@router.post("/clear_calibration")
async def clear_calibration(
    payload: dict = Body(default={}),
    bridge: Bridge = Depends(get_bridge),
) -> dict:
    which = payload.get("which", "Both")
    try:
        await bridge.phd2.clear_calibration(which)
    except Exception as e:
        raise _phd2_http_error("clear_calibration", e)
    return {"ok": True}


@router.post("/pause")
async def pause(
    payload: dict = Body(default={}),
    bridge: Bridge = Depends(get_bridge),
) -> dict:
    try:
        await bridge.phd2.set_paused(bool(payload.get("paused", True)),
                                    full=bool(payload.get("full", False)))
    except Exception as e:
        raise _phd2_http_error("pause", e)
    return {"ok": True}


@router.post("/find_star")
async def find_star(bridge: Bridge = Depends(get_bridge)) -> dict:
    try:
        r = await bridge.phd2.call("find_star", timeout=30.0)
    except Exception as e:
        raise _phd2_http_error("find_star", e)
    return {"ok": True, "result": r}


@router.post("/calibrate")
async def calibrate(bridge: Bridge = Depends(get_bridge)) -> dict:
    """Avvia calibration completa: clear cal → loop una volta per essere
    sicuri che ci sia un frame fresco → guide(recalibrate=True).

    Bug fix v0.2.25: prima si chiamava direttamente `guide` senza alcun
    timeout esteso, e PHD2 a volte impiegava >10s a rispondere
    all'acknowledgment se la sua stato interno era in transizione
    (Looping/Stopped/Selected). Risultato: asyncio.TimeoutError →
    Internal Server Error 500 visibile in app.
    Adesso:
      - log esplicito di ogni step
      - timeout esteso a 30s (l'acknowledgment di "guide" deve essere
        comunque rapido ma diamo margine)
      - errori mappati a 504/422/503 con detail leggibili
    """
    _logger.info("calibrate: clearing calibration (Both)")
    try:
        await bridge.phd2.call("clear_calibration", "Both", timeout=10.0)
    except Exception as e:
        raise _phd2_http_error("calibrate (clear_calibration)", e)

    # Piccola pausa: PHD2 ha bisogno di un attimo per processare il clear
    # prima di poter accettare un nuovo guide command.
    await asyncio.sleep(0.5)

    _logger.info("calibrate: triggering guide(recalibrate=True)")
    try:
        await bridge.phd2.call("guide", {
            "settle": {"pixels": 1.5, "time": 10.0, "timeout": 60.0},
            "recalibrate": True,
        }, timeout=30.0)
    except Exception as e:
        raise _phd2_http_error("calibrate (guide)", e)
    return {"ok": True}


@router.get("/profile")
async def profile(bridge: Bridge = Depends(get_bridge)) -> dict:
    """Restituisce info su profilo guide attivo (camera, mount, scope)."""
    try:
        eq = await bridge.phd2.call("get_current_equipment", timeout=5.0)
    except Exception:
        eq = None
    info: dict = {"equipment": eq or {}}
    try:
        info["pixel_scale"] = await bridge.phd2.call("get_pixel_scale", timeout=3.0)
    except Exception:
        pass
    try:
        info["calibrated"] = await bridge.phd2.call("get_calibrated", timeout=3.0)
    except Exception:
        pass
    try:
        info["app_state"] = await bridge.phd2.call("get_app_state", timeout=3.0)
    except Exception:
        pass
    return info


@router.get("/star_image")
async def star_image(
    fmt: str = "json", size: int = 0,
    bridge: Bridge = Depends(get_bridge),
):
    """Ritorna l'immagine del riquadro intorno alla stella di guida di PHD2.

    PHD2 espone `get_star_image` via JSON-RPC che ritorna:
      {
        "frame": int,                  # numero frame
        "width": int, "height": int,   # dimensioni del crop in pixel
        "star_pos": [x, y],            # posizione stella nel crop
        "pixels": "<base64-rawdata>"   # array di uint16 little-endian
      }
    Lo riconvertiamo in PNG 8-bit stretchato (auto-stretch in stile PI)
    così la app può mostrarlo direttamente con <Image.memory>.

    Query:
      fmt:  "json" (default, ritorna anche PNG in base64) o "png" (binary)
      size: opzionale, suggerimento dimensione (ignorato da PHD2 di solito)
    """
    import base64
    import io
    import struct
    import numpy as np
    from fastapi.responses import Response
    from ..images.processor import _percentile_stretch

    params: list = []
    if size > 0:
        params = [size]
    try:
        res = await bridge.phd2.call("get_star_image", params, timeout=5.0)
    except Phd2RpcError as e:
        # PHD2 ritorna errore se non c'è stella selezionata o se è in modalità
        # incompatibile (es. looping ma senza star)
        raise HTTPException(status_code=409, detail=f"PHD2: {e}")
    except asyncio.TimeoutError:
        # Comune: get_star_image durante settling/transitorio. Trattiamo
        # come 409 (transitoriamente non disponibile) così l'UI mostra
        # "no star selected" invece di spammare errori.
        raise HTTPException(status_code=409,
            detail="PHD2 non ha risposto in tempo (probabilmente nessuna stella selezionata)")
    except ConnectionError as e:
        raise HTTPException(status_code=503, detail=f"PHD2 not reachable: {e}")
    except Exception as e:
        _logger.exception("get_star_image unexpected")
        raise HTTPException(status_code=500,
            detail=f"PHD2 get_star_image unexpected: {type(e).__name__}: {e}")

    if not isinstance(res, dict) or "pixels" not in res:
        raise HTTPException(status_code=502,
                            detail=f"PHD2 get_star_image bad payload: {res}")

    w = int(res.get("width", 0))
    h = int(res.get("height", 0))
    if w <= 0 or h <= 0:
        raise HTTPException(status_code=502, detail="PHD2: invalid image size")

    # pixels è base64 di un array di uint16 (PHD2 convention)
    try:
        raw = base64.b64decode(res["pixels"])
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"PHD2 b64 decode: {e}")

    if len(raw) != w * h * 2:
        raise HTTPException(status_code=502,
                            detail=f"PHD2: pixel buffer len {len(raw)} != {w*h*2}")

    arr = np.frombuffer(raw, dtype="<u2").reshape((h, w)).astype(np.float64)

    # Stretch con lo stesso algoritmo che usiamo per i frame Ekos.
    stretched = _percentile_stretch(arr)

    # Crea PNG via PIL
    try:
        from PIL import Image
        img = Image.fromarray(stretched, mode="L").convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=False)
        png_bytes = buf.getvalue()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"PNG encode: {e}")

    star_pos = res.get("star_pos") or [w / 2.0, h / 2.0]
    payload = {
        "frame": res.get("frame"),
        "width": w,
        "height": h,
        "star_x": float(star_pos[0]) if len(star_pos) > 0 else None,
        "star_y": float(star_pos[1]) if len(star_pos) > 1 else None,
    }

    if fmt.lower() == "png":
        return Response(content=png_bytes, media_type="image/png",
                        headers={"Cache-Control": "no-store",
                                 "X-Star-X": str(payload["star_x"] or ""),
                                 "X-Star-Y": str(payload["star_y"] or ""),
                                 "X-Width": str(w),
                                 "X-Height": str(h),
                                 "X-Frame": str(payload["frame"] or "")})
    payload["png_base64"] = base64.b64encode(png_bytes).decode("ascii")
    return payload
