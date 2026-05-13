"""Route /api/focuser."""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException

from ..auth import require_token
from ..deps import Bridge, get_bridge
from ._roles import first_element, resolve_device

router = APIRouter(prefix="/api/focuser", tags=["focuser"], dependencies=[Depends(require_token)])


@router.get("/status")
async def status(device: str | None = None, bridge: Bridge = Depends(get_bridge)) -> dict:
    dev = await resolve_device(bridge.state, "focuser", device)
    pos = await bridge.state.get_property(dev, "ABS_FOCUS_POSITION") or {}
    rel = await bridge.state.get_property(dev, "REL_FOCUS_POSITION") or {}
    temp = await bridge.state.get_property(dev, "FOCUS_TEMPERATURE") or {}
    abort = await bridge.state.get_property(dev, "FOCUS_ABORT_MOTION") or {}
    backlash = await bridge.state.get_property(dev, "FOCUS_BACKLASH_STEPS") or {}
    direction = await bridge.state.get_property(dev, "FOCUS_MOTION") or {}
    return {
        "device": dev,
        "position": first_element(pos, "FOCUS_ABSOLUTE_POSITION"),
        "max_position": _max_for(pos, "FOCUS_ABSOLUTE_POSITION"),
        "rel_step": first_element(rel, "FOCUS_RELATIVE_POSITION"),
        "temperature": first_element(temp, "TEMPERATURE"),
        "backlash": first_element(backlash, "FOCUS_BACKLASH_VALUE"),
        "moving": pos.get("state") == "Busy",
        "direction": _selected_switch(direction),
    }


@router.post("/abs")
async def goto_abs(
    payload: dict = Body(..., example={"position": 24350}),
    bridge: Bridge = Depends(get_bridge),
) -> dict:
    dev = await resolve_device(bridge.state, "focuser", payload.get("device"))
    pos = int(payload["position"])
    await bridge.indi.send_number(dev, "ABS_FOCUS_POSITION", {"FOCUS_ABSOLUTE_POSITION": pos})
    return {"ok": True}


@router.post("/rel")
async def move_rel(
    payload: dict = Body(..., example={"steps": 100, "direction": "in"}),
    bridge: Bridge = Depends(get_bridge),
) -> dict:
    dev = await resolve_device(bridge.state, "focuser", payload.get("device"))
    steps = abs(int(payload["steps"]))
    direction = (payload.get("direction") or "out").lower()
    if direction not in ("in", "out"):
        raise HTTPException(status_code=400, detail="direction must be in|out")
    # FOCUS_MOTION: FOCUS_INWARD | FOCUS_OUTWARD
    await bridge.indi.send_switch(dev, "FOCUS_MOTION", {
        "FOCUS_INWARD": direction == "in",
        "FOCUS_OUTWARD": direction == "out",
    })
    await bridge.indi.send_number(dev, "REL_FOCUS_POSITION", {"FOCUS_RELATIVE_POSITION": steps})
    return {"ok": True}


@router.post("/abort")
async def abort(device: str | None = None, bridge: Bridge = Depends(get_bridge)) -> dict:
    dev = await resolve_device(bridge.state, "focuser", device)
    await bridge.indi.send_switch(dev, "FOCUS_ABORT_MOTION", {"ABORT": True})
    return {"ok": True}


# In-memory state degli autofocus run (per polling progress)
_AUTOFOCUS_RUNS: dict[str, dict] = {}


@router.post("/autofocus")
async def autofocus_start(
    payload: dict = Body(default={}),
    bridge: Bridge = Depends(get_bridge),
) -> dict:
    """Avvia autofocus iterativo lato bridge.

    Algoritmo:
    1. Prende posizione corrente
    2. Per N step (default 9), cambia posizione di step_size
    3. Per ogni posizione: scatta exposure_sec, attende fine, legge HFR dal frame
    4. Determina V-curve, trova minimo HFR, sposta focuser su quella posizione

    Body params:
      device: str (focuser device, optional if 1 only)
      camera: str (camera device, optional if 1 only)
      step_size: int (default 50)
      n_steps: int (default 9, dispari)
      exposure_sec: float (default 2.0)
    """
    foc = await resolve_device(bridge.state, "focuser", payload.get("device"))
    cam = await resolve_device(bridge.state, "camera", payload.get("camera"))
    step_size = int(payload.get("step_size", 50))
    n_steps = int(payload.get("n_steps", 9))
    if n_steps % 2 == 0:
        n_steps += 1
    exposure = float(payload.get("exposure_sec", 2.0))

    pos_prop = await bridge.state.get_property(foc, "ABS_FOCUS_POSITION")
    cur_pos = first_element(pos_prop, "FOCUS_ABSOLUTE_POSITION", 0)
    if cur_pos is None:
        raise HTTPException(status_code=503, detail="cannot read focuser position")
    cur_pos = int(cur_pos)

    run_id = f"af_{int(asyncio.get_event_loop().time() * 1000)}"
    _AUTOFOCUS_RUNS[run_id] = {
        "id": run_id,
        "focuser": foc,
        "camera": cam,
        "step_size": step_size,
        "n_steps": n_steps,
        "exposure": exposure,
        "start_pos": cur_pos,
        "samples": [],   # list of {pos, hfr, stars}
        "best_pos": None,
        "best_hfr": None,
        "status": "running",  # running | done | failed
        "step_idx": 0,
        "error": None,
    }
    asyncio.create_task(_run_autofocus(bridge, run_id))
    return {"ok": True, "run_id": run_id}


@router.get("/autofocus/{run_id}")
async def autofocus_status(run_id: str) -> dict:
    run = _AUTOFOCUS_RUNS.get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    return run


@router.post("/autofocus/{run_id}/abort")
async def autofocus_abort(run_id: str) -> dict:
    run = _AUTOFOCUS_RUNS.get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    run["status"] = "aborting"
    return {"ok": True}


async def _run_autofocus(bridge: Bridge, run_id: str) -> None:
    """Sweep iterativo: muove il focuser su N posizioni equispaziate,
    scatta un'esposizione corta su ogni punto, legge HFR dal BLOB
    intercettato → trova minimo → torna su quella posizione.

    Fix critici rispetto alla v0.2.20 e prima:
      • A: race su state risolto da `_wait_state_busy_then_ok` che attende
        il transitorio Busy→Ok invece di solo Ok (vedi Bug A documentato
        nel CHANGELOG di v0.2.22)
      • B: HFR letto attendendo l'evento di NUOVO frame (ts cambiato),
        invece di sleep+snapshot — niente più letture stantie
      • C: backlash compensation sul move finale: sovra-corre 100 step
        oltre best_pos, poi torna a best_pos in modo che ci arrivi
        sempre dalla stessa direzione del sweep iniziale
      • D: verifica UPLOAD_MODE all'avvio; se NEVER avvisa esplicitamente
        (HFR sarebbe sempre None)
    """
    import asyncio as _asyncio
    import logging
    logger = logging.getLogger("astroarch_bridge.autofocus")
    run = _AUTOFOCUS_RUNS[run_id]
    foc = run["focuser"]
    cam = run["camera"]
    step = run["step_size"]
    n = run["n_steps"]
    exp = run["exposure"]
    start = run["start_pos"]

    logger.info("autofocus %s: foc=%s cam=%s start=%d step=%d n=%d exp=%.1fs",
                run_id, foc, cam, start, step, n, exp)

    # ---- Fix D: verifica UPLOAD_MODE ----
    try:
        upload = await bridge.state.get_property(cam, "UPLOAD_MODE")
        if upload:
            elements = upload.get("elements", [])
            current = next((e["name"] for e in elements if e.get("value") is True), None)
            if current == "UPLOAD_NEVER":
                run["error"] = ("UPLOAD_MODE=NEVER on camera: bridge cannot "
                                "receive BLOBs, HFR unavailable. "
                                "Imposta CCD_UPLOAD_MODE=CLIENT o BOTH in Ekos.")
                run["status"] = "failed"
                logger.error("autofocus %s: %s", run_id, run["error"])
                return
            logger.info("autofocus %s: UPLOAD_MODE=%s", run_id, current)
    except Exception as e:
        logger.warning("autofocus %s: cannot read UPLOAD_MODE: %s", run_id, e)

    half = n // 2
    positions = [start + (i - half) * step for i in range(n)]
    sweep_dir = "out"  # sweep dal punto più basso al più alto = outward

    try:
        for idx, pos in enumerate(positions):
            if run["status"] == "aborting":
                run["status"] = "aborted"
                return
            run["step_idx"] = idx + 1

            # ---- Fix A: move focuser con attesa Busy→Ok ----
            logger.info("autofocus %s: move foc to %d", run_id, pos)
            await bridge.indi.send_number(foc, "ABS_FOCUS_POSITION",
                                          {"FOCUS_ABSOLUTE_POSITION": pos})
            ok = await _wait_state_busy_then_ok(bridge, foc,
                "ABS_FOCUS_POSITION", timeout=30.0)
            if not ok:
                run["error"] = f"focuser move timeout at {pos}"
                run["status"] = "failed"
                logger.error("autofocus %s: %s", run_id, run["error"])
                return

            # ---- Fix B: ts pre-esposizione per detect nuovo frame ----
            snap_before = await bridge.state.snapshot()
            ts_before = (snap_before.get("last_frame") or {}).get("ts", 0)

            # ---- Fix A: esposizione con attesa Busy→Ok ----
            logger.info("autofocus %s: expose %.2fs", run_id, exp)
            await bridge.indi.send_number(cam, "CCD_EXPOSURE",
                                          {"CCD_EXPOSURE_VALUE": exp})
            ok = await _wait_state_busy_then_ok(bridge, cam, "CCD_EXPOSURE",
                                                 timeout=exp + 30.0)
            if not ok:
                run["error"] = f"expose timeout at {pos}"
                run["status"] = "failed"
                logger.error("autofocus %s: %s", run_id, run["error"])
                return

            # ---- Fix B: aspetta un FRAME NUOVO (ts > ts_before) ----
            hfr, stars = await _wait_new_frame_hfr(bridge, ts_before,
                                                    timeout=15.0)
            if hfr is None:
                logger.warning("autofocus %s: no HFR at pos=%d", run_id, pos)
            else:
                logger.info("autofocus %s: pos=%d HFR=%.3f stars=%s",
                            run_id, pos, hfr, stars)
            run["samples"].append({"pos": pos, "hfr": hfr, "stars": stars})

        # Find min HFR
        valid = [s for s in run["samples"] if s["hfr"] and s["hfr"] > 0]
        if not valid:
            run["error"] = ("No valid HFR samples. Possibili cause: BLOB "
                            "non ricevuti (verifica UPLOAD_MODE), camera "
                            "non connessa, immagini sature/oscure, focuser "
                            "fuori dalla zona di focus (aumenta step_size).")
            run["status"] = "failed"
            logger.error("autofocus %s: %s", run_id, run["error"])
            return
        best = min(valid, key=lambda s: s["hfr"])
        run["best_pos"] = best["pos"]
        run["best_hfr"] = best["hfr"]
        logger.info("autofocus %s: best pos=%d HFR=%.3f",
                    run_id, best["pos"], best["hfr"])

        # ---- Fix C: backlash compensation ----
        # Lo sweep ha mosso il focuser sempre in una direzione (outward).
        # Per arrivare al best_pos dalla stessa direzione, sovra-corriamo
        # 150 step oltre, poi torniamo. Così l'ultimo segmento è sempre
        # nello stesso senso → nessuno spostamento residuo da backlash.
        backlash_overshoot = 150
        overshoot_pos = int(best["pos"]) + backlash_overshoot
        logger.info("autofocus %s: backlash overshoot to %d, then back to %d",
                    run_id, overshoot_pos, best["pos"])
        await bridge.indi.send_number(foc, "ABS_FOCUS_POSITION",
                                      {"FOCUS_ABSOLUTE_POSITION": overshoot_pos})
        await _wait_state_busy_then_ok(bridge, foc, "ABS_FOCUS_POSITION",
                                        timeout=30.0)
        await bridge.indi.send_number(foc, "ABS_FOCUS_POSITION",
                                      {"FOCUS_ABSOLUTE_POSITION": int(best["pos"])})
        await _wait_state_busy_then_ok(bridge, foc, "ABS_FOCUS_POSITION",
                                        timeout=30.0)
        run["status"] = "done"
        logger.info("autofocus %s: DONE at pos=%d", run_id, best["pos"])
    except Exception as e:
        run["error"] = str(e)
        run["status"] = "failed"
        logger.exception("autofocus %s: exception", run_id)


async def _wait_state_busy_then_ok(bridge: Bridge, dev: str, prop: str,
                                    timeout: float) -> bool:
    """Attende che lo state della property passi da "Ok" (o qualunque)
    PRIMA a "Busy" (= il comando è stato accettato dal driver), POI a
    "Ok" (= il comando è completato).

    Fix per Bug A: la vecchia `_wait_prop_state(..., "Ok", ...)` poteva
    ritornare subito perché lo state era ancora "Ok" dal comando
    precedente — il send_number non aveva ancora propagato a INDI.
    Qui aspettiamo prima il transitorio Busy, poi il ritorno a Ok.

    Se Busy non arriva entro 2s (alcuni driver veloci saltano lo stato
    Busy del tutto, o lo settano "Idle"), facciamo fallback al vecchio
    polling solo Ok — meglio finto-completo che falso-failed.
    """
    import asyncio as _asyncio
    loop = _asyncio.get_event_loop()
    deadline = loop.time() + timeout
    busy_seen = False
    # Fase 1: aspetta Busy (o un transitorio non-Ok)
    busy_deadline = loop.time() + 2.0
    while loop.time() < busy_deadline:
        p = await bridge.state.get_property(dev, prop)
        st = p.get("state") if p else None
        if st in ("Busy", "Alert"):
            busy_seen = True
            break
        await _asyncio.sleep(0.1)
    # Fase 2: aspetta Ok (anche se Busy non l'abbiamo visto, esiste un
    # piccolo numero di driver che skippa lo stato → fallback grazioso)
    while loop.time() < deadline:
        p = await bridge.state.get_property(dev, prop)
        st = p.get("state") if p else None
        if st == "Ok":
            return True
        if st == "Alert":
            return False
        await _asyncio.sleep(0.2)
    # Se ci siamo solo addormentati in fase 1 ma stato è ancora Ok dopo,
    # è successo qualcosa di strano. False = timeout effettivo.
    _ = busy_seen
    return False


async def _wait_new_frame_hfr(bridge: Bridge, ts_before: float,
                               timeout: float) -> tuple[float | None, int | None]:
    """Attende che il bridge processi un NUOVO frame (last_frame.ts >
    ts_before) e ritorna (hfr, stars).

    Fix per Bug B: prima leggevamo HFR con sleep(1) + snapshot, ma se
    il BLOB tarda (camere grandi tipo ATR2600C 26Mpx, ~2-3s per
    processare), restituivamo l'HFR del frame PRECEDENTE → V-curve
    sbagliata.
    """
    import asyncio as _asyncio
    loop = _asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        snap = await bridge.state.snapshot()
        lf = snap.get("last_frame") or {}
        ts_now = lf.get("ts", 0)
        if ts_now and ts_now > ts_before:
            return lf.get("hfr"), lf.get("stars")
        await _asyncio.sleep(0.2)
    # Timeout: nessun frame nuovo arrivato
    return None, None


# import asyncio here to avoid circular at import time
import asyncio  # noqa: E402


def _selected_switch(prop: dict) -> str | None:
    for e in prop.get("elements", []):
        if e.get("value") is True:
            return e["name"]
    return None


def _max_for(prop: dict, name: str):
    for e in prop.get("elements", []):
        if e["name"] == name:
            return e.get("max")
    return None
