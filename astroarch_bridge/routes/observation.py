"""Route /api/observation - orchestratore "pre-flight" stile Ekos.

Pipeline:
  1. RESOLVE TARGET (SIMBAD se nome) -> RA/Dec
  2. SLEW + TRACK (mount goto + verifica state Ok)
  3. PLATE SOLVE (solve-field sull'ultimo frame)
  4. SYNC mount sul risultato
  5. AUTOFOCUS (opzionale)
  6. GUIDE CALIBRATE (opzionale, se non già calibrato)
  7. GUIDE START (settle entro N secondi)
  8. CAPTURE LOAD + START (via Ekos DBus)

Ogni fase: state pending|running|done|failed|skipped + messaggio.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

from fastapi import APIRouter, Body, Depends, HTTPException

from ..auth import require_token
from ..deps import Bridge, get_bridge
from ._roles import resolve_device

router = APIRouter(prefix="/api/observation", tags=["observation"],
                   dependencies=[Depends(require_token)])

_RUNS: dict[str, dict] = {}

PHASES = [
    "resolve_target",
    "slew",
    "tracking",
    "plate_solve",
    "sync_mount",
    "autofocus",
    "guide_calibrate",
    "guide_start",
    "capture_load",
    "capture_started",
]


def _new_run(target_name: str | None) -> dict:
    return {
        "id": f"obs_{int(time.time() * 1000)}",
        "target_name": target_name,
        "ra_hours": None,
        "dec_deg": None,
        "phases": [
            {"name": p, "status": "pending", "msg": "", "started_at": None, "ended_at": None}
            for p in PHASES
        ],
        "status": "running",   # running | done | failed | aborted
        "current_phase": None,
        "error": None,
        "created_at": time.time(),
    }


def _phase(run: dict, name: str) -> dict | None:
    for p in run["phases"]:
        if p["name"] == name:
            return p
    return None


def _start_phase(run: dict, name: str) -> dict:
    p = _phase(run, name)
    p["status"] = "running"
    p["started_at"] = time.time()
    run["current_phase"] = name
    return p


def _end_phase(run: dict, name: str, ok: bool, msg: str = "", skipped: bool = False) -> None:
    p = _phase(run, name)
    if p is None:
        return
    p["status"] = "skipped" if skipped else ("done" if ok else "failed")
    p["msg"] = msg
    p["ended_at"] = time.time()


@router.get("/{run_id}")
async def get_run(run_id: str) -> dict:
    run = _RUNS.get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    return run


@router.post("/{run_id}/abort")
async def abort_run(run_id: str) -> dict:
    run = _RUNS.get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    run["status"] = "aborting"
    return {"ok": True}


@router.post("/run")
async def run(payload: dict = Body(...), bridge: Bridge = Depends(get_bridge)) -> dict:
    """Avvia una sessione di osservazione completa.

    Body:
      target_name: str opzionale (sarà risolto SIMBAD)
      ra_hours, dec_deg: float opzionali (alternativa a target_name)
      jobs: list[CaptureJob] opzionale (se assente, no capture)
      do_plate_solve: bool default true
      do_autofocus: bool default false
      do_guide_calibrate: bool default false (solo se non già calibrato)
      do_guide_start: bool default true
      use_ekos_capture: bool default true (altrimenti DIRECT)
    """
    target = payload.get("target_name")
    ra = payload.get("ra_hours")
    dec = payload.get("dec_deg")
    if target is None and (ra is None or dec is None):
        raise HTTPException(status_code=400,
                            detail="target_name o (ra_hours, dec_deg) richiesti")

    run = _new_run(target)
    run["payload"] = dict(payload)
    if ra is not None: run["ra_hours"] = float(ra)
    if dec is not None: run["dec_deg"] = float(dec)
    _RUNS[run["id"]] = run
    asyncio.create_task(_run_observation(bridge, run))
    return {"ok": True, "run_id": run["id"]}


async def _run_observation(bridge: Bridge, run: dict) -> None:
    """Esegue le fasi sequenzialmente. Ogni fase aggiorna lo stato in run."""
    payload = run["payload"]

    try:
        # 1. Resolve target
        _start_phase(run, "resolve_target")
        if run["target_name"] and (run["ra_hours"] is None or run["dec_deg"] is None):
            try:
                from astropy.coordinates import SkyCoord
                sc = await asyncio.to_thread(SkyCoord.from_name, run["target_name"])
                run["ra_hours"] = float(sc.ra.hour)
                run["dec_deg"] = float(sc.dec.deg)
                _end_phase(run, "resolve_target", True,
                           f"{run['target_name']}: RA {run['ra_hours']:.4f}h Dec {run['dec_deg']:.4f}°")
            except Exception as e:
                _end_phase(run, "resolve_target", False, f"SIMBAD: {e}")
                run["status"] = "failed"
                run["error"] = "Target resolution failed"
                return
        else:
            _end_phase(run, "resolve_target", True, "RA/Dec forniti dal client")

        if _aborted(run): return

        # 2. Slew (manda goto with track action)
        _start_phase(run, "slew")
        try:
            mount = await resolve_device(bridge.state, "mount", payload.get("mount_device"))
            await bridge.indi.send_switch(mount, "ON_COORD_SET",
                                          {"SLEW": False, "TRACK": True, "SYNC": False})
            await bridge.indi.send_number(mount, "EQUATORIAL_EOD_COORD",
                                          {"RA": run["ra_hours"], "DEC": run["dec_deg"]})
            _end_phase(run, "slew", True, f"comando slew inviato a {mount}")
        except Exception as e:
            _end_phase(run, "slew", False, str(e))
            run["status"] = "failed"; run["error"] = "slew failed"; return

        if _aborted(run): return

        # 3. Tracking - aspetta state Ok per EQUATORIAL_EOD_COORD
        _start_phase(run, "tracking")
        ok = await _wait_property_state(bridge, mount, "EQUATORIAL_EOD_COORD",
                                         "Ok", timeout=300)
        if not ok:
            _end_phase(run, "tracking", False, "timeout 5 min slew")
            run["status"] = "failed"; run["error"] = "tracking timeout"; return
        _end_phase(run, "tracking", True, "mount on target, tracking attivo")

        if _aborted(run): return

        # 4. Plate solve (opzionale)
        if payload.get("do_plate_solve", True):
            _start_phase(run, "plate_solve")
            ok, result = await _do_plate_solve(bridge, run, hint_ra=run["ra_hours"] * 15.0,
                                               hint_dec=run["dec_deg"], hint_radius=5.0)
            if not ok:
                _end_phase(run, "plate_solve", False, "solver failed")
                run["status"] = "failed"; run["error"] = "plate_solve failed"; return
            _end_phase(run, "plate_solve", True,
                       f"RA {result['ra_hours']:.4f}h Dec {result['dec_deg']:.4f}° "
                       f"({result['scale_arcsec_px']:.1f}\"/px)")
            # 5. Sync mount sul solve
            _start_phase(run, "sync_mount")
            try:
                await bridge.indi.send_switch(mount, "ON_COORD_SET",
                                              {"SLEW": False, "TRACK": False, "SYNC": True})
                await bridge.indi.send_number(mount, "EQUATORIAL_EOD_COORD",
                                              {"RA": result["ra_hours"],
                                               "DEC": result["dec_deg"]})
                # Re-track
                await asyncio.sleep(0.5)
                await bridge.indi.send_switch(mount, "ON_COORD_SET",
                                              {"SLEW": False, "TRACK": True, "SYNC": False})
                _end_phase(run, "sync_mount", True, "mount sincronizzata sul solve")
            except Exception as e:
                _end_phase(run, "sync_mount", False, str(e))
        else:
            _end_phase(run, "plate_solve", True, "skipped", skipped=True)
            _end_phase(run, "sync_mount", True, "skipped", skipped=True)

        if _aborted(run): return

        # 6. Autofocus (opzionale)
        if payload.get("do_autofocus", False):
            _start_phase(run, "autofocus")
            try:
                from .focuser import _AUTOFOCUS_RUNS, _run_autofocus  # type: ignore
                # Trigger autofocus
                foc = await resolve_device(bridge.state, "focuser",
                                           payload.get("focuser_device"))
                from .align import _resolve_primary_camera
                cam = await _resolve_primary_camera(bridge, payload.get("camera_device"))
                pos_prop = await bridge.state.get_property(foc, "ABS_FOCUS_POSITION")
                cur = pos_prop["elements"][0].get("value", 0) if pos_prop else 0
                run_id_af = f"af_{int(time.time() * 1000)}"
                _AUTOFOCUS_RUNS[run_id_af] = {
                    "id": run_id_af, "focuser": foc, "camera": cam,
                    "step_size": int(payload.get("af_step_size", 50)),
                    "n_steps": int(payload.get("af_n_steps", 9)),
                    "exposure": float(payload.get("af_exposure", 2.0)),
                    "start_pos": int(cur), "samples": [],
                    "best_pos": None, "best_hfr": None,
                    "status": "running", "step_idx": 0, "error": None,
                }
                await _run_autofocus(bridge, run_id_af)
                af = _AUTOFOCUS_RUNS[run_id_af]
                if af["status"] == "done":
                    _end_phase(run, "autofocus", True,
                               f"best pos {af['best_pos']} HFR {af['best_hfr']:.2f}")
                else:
                    _end_phase(run, "autofocus", False,
                               af.get("error") or "autofocus non completato",
                               skipped=False)
            except Exception as e:
                _end_phase(run, "autofocus", False, f"errore: {e}")
                # Non blocca
        else:
            _end_phase(run, "autofocus", True, "skipped", skipped=True)

        if _aborted(run): return

        # 7. Guide calibrate (opzionale)
        if payload.get("do_guide_calibrate", False):
            _start_phase(run, "guide_calibrate")
            try:
                if bridge.phd2.state != "connected":
                    _end_phase(run, "guide_calibrate", False, "PHD2 non connesso")
                else:
                    await bridge.phd2.call("clear_calibration", "Both", timeout=5.0)
                    await bridge.phd2.call("guide", {
                        "settle": {"pixels": 1.5, "time": 10.0, "timeout": 60.0},
                        "recalibrate": True,
                    }, timeout=15.0)
                    # Aspetta che esca da Calibrating
                    deadline = time.time() + 240
                    while time.time() < deadline:
                        if _aborted(run): return
                        st = bridge.phd2.live.get("app_state")
                        if st == "Guiding":
                            break
                        if st == "Stopped":
                            _end_phase(run, "guide_calibrate", False,
                                       "calibration interrotta (Stopped)")
                            run["status"] = "failed"; run["error"] = "guide cal stopped"; return
                        await asyncio.sleep(2)
                    _end_phase(run, "guide_calibrate", True, "calibration ok")
            except Exception as e:
                _end_phase(run, "guide_calibrate", False, f"{e}")
                run["status"] = "failed"; run["error"] = "guide cal failed"; return
        else:
            _end_phase(run, "guide_calibrate", True, "skipped", skipped=True)

        if _aborted(run): return

        # 8. Guide start
        if payload.get("do_guide_start", True):
            _start_phase(run, "guide_start")
            try:
                if bridge.phd2.state != "connected":
                    _end_phase(run, "guide_start", False, "PHD2 non connesso (skip)",
                               skipped=True)
                else:
                    if bridge.phd2.live.get("app_state") != "Guiding":
                        await bridge.phd2.call("guide", {
                            "settle": {"pixels": 1.5, "time": 10.0, "timeout": 90.0},
                            "recalibrate": False,
                        }, timeout=15.0)
                        # Aspetta settled
                        deadline = time.time() + 180
                        while time.time() < deadline:
                            if _aborted(run): return
                            st = bridge.phd2.live.get("app_state")
                            settling = bridge.phd2.live.get("settling")
                            if st == "Guiding" and not settling:
                                break
                            await asyncio.sleep(2)
                    _end_phase(run, "guide_start", True, "guiding attivo")
            except Exception as e:
                _end_phase(run, "guide_start", False, f"{e}")
                run["status"] = "failed"; run["error"] = "guide start failed"; return
        else:
            _end_phase(run, "guide_start", True, "skipped", skipped=True)

        if _aborted(run): return

        # 9. Capture load + start (via Ekos)
        jobs = payload.get("jobs") or []
        if jobs:
            _start_phase(run, "capture_load")
            if payload.get("use_ekos_capture", True):
                try:
                    from .capture_ekos import _esq_for_jobs, _dbus_call, EKOS_DBUS_SERVICE, EKOS_CAPTURE_PATH
                    from ..config import get_settings as _gs
                    fits_dir = str(_gs().images_dir)
                    esq = _esq_for_jobs(jobs, target_name=run["target_name"] or "Observation",
                                        fits_dir=fits_dir)
                    save_dir = Path(fits_dir) / "AstroarchInterface"
                    save_dir.mkdir(parents=True, exist_ok=True)
                    esq_path = save_dir / f"obs_{run['id']}.esq"
                    esq_path.write_text(esq, encoding="utf-8")
                    rc, out = await _dbus_call(EKOS_DBUS_SERVICE, EKOS_CAPTURE_PATH,
                                                "org.kde.kstars.Ekos.Capture.clearSequenceQueue")
                    rc, out = await _dbus_call(EKOS_DBUS_SERVICE, EKOS_CAPTURE_PATH,
                                                "org.kde.kstars.Ekos.Capture.loadSequenceQueue",
                                                str(esq_path), "", "true",
                                                run["target_name"] or "Observation")
                    if rc != 0 or out.lower() == "false":
                        _end_phase(run, "capture_load", False, f"loadSequenceQueue: {out}")
                        run["status"] = "failed"; run["error"] = "capture load failed"; return
                    _end_phase(run, "capture_load", True, f"{len(jobs)} job in coda Ekos")
                    # Start
                    _start_phase(run, "capture_started")
                    rc, out = await _dbus_call(EKOS_DBUS_SERVICE, EKOS_CAPTURE_PATH,
                                                "org.kde.kstars.Ekos.Capture.start", "")
                    _end_phase(run, "capture_started", rc == 0, out or "started")
                except Exception as e:
                    _end_phase(run, "capture_load", False, f"{e}")
                    run["status"] = "failed"; run["error"] = "capture load exception"; return
            else:
                # DIRECT: non avviato qui, l'app userà SequenceRunner
                _end_phase(run, "capture_load", True, "DIRECT mode (app gestisce)",
                           skipped=True)
                _end_phase(run, "capture_started", True, "DIRECT mode", skipped=True)
        else:
            _end_phase(run, "capture_load", True, "no jobs", skipped=True)
            _end_phase(run, "capture_started", True, "no jobs", skipped=True)

        run["status"] = "done"
    except Exception as e:
        run["status"] = "failed"
        run["error"] = f"unexpected: {e}"
    finally:
        run["current_phase"] = None


def _aborted(run: dict) -> bool:
    if run["status"] == "aborting":
        run["status"] = "aborted"
        return True
    return False


async def _wait_property_state(bridge: Bridge, dev: str, name: str,
                                target_state: str, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        p = await bridge.state.get_property(dev, name)
        if p and p.get("state") == target_state:
            return True
        await asyncio.sleep(0.8)
    return False


async def _do_plate_solve(bridge: Bridge, run: dict,
                           hint_ra: float, hint_dec: float,
                           hint_radius: float) -> tuple[bool, dict]:
    from .align import _SOLVE_RUNS, _run_solve
    from ..config import get_settings
    snap = await bridge.state.snapshot()
    last = snap.get("last_frame", {}) or {}
    fits_path = last.get("path")
    if not fits_path:
        # Catturiamo un frame veloce
        from .align import _resolve_primary_camera
        cam = await _resolve_primary_camera(bridge, run["payload"].get("camera_device"))
        await bridge.indi.send_number(cam, "CCD_EXPOSURE", {"CCD_EXPOSURE_VALUE": 5})
        ok = await _wait_property_state(bridge, cam, "CCD_EXPOSURE", "Ok", 30)
        if not ok:
            return False, {}
        await asyncio.sleep(2)
        snap = await bridge.state.snapshot()
        last = snap.get("last_frame", {}) or {}
        fits_path = last.get("path")
        if not fits_path:
            return False, {}
    s = get_settings()
    p = Path(fits_path)
    if not p.is_absolute():
        p = s.images_dir / p
    if not p.exists():
        return False, {}
    sv_id = f"sv_{int(time.time()*1000)}"
    _SOLVE_RUNS[sv_id] = {"id": sv_id, "path": str(p), "status": "running",
                          "result": None, "error": None, "stdout_tail": ""}
    await _run_solve(sv_id, p, {"hint_ra": hint_ra, "hint_dec": hint_dec,
                                 "hint_radius": hint_radius})
    sv = _SOLVE_RUNS[sv_id]
    if sv["status"] == "done" and sv["result"]:
        return True, sv["result"]
    return False, {}
