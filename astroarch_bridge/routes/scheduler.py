"""Route /api/scheduler — scheduler temporale multi-target.

Modello SchedulerJob:
  id, name, target_name (opzionale), ra_hours, dec_deg
  start_time (ISO), end_time (ISO), min_altitude (deg)
  capture_jobs: lista (filter, count, exposure, gain, offset, bin, frame_type)
  pre_action: park=False, slew=True, autofocus=False
  status: pending | running | done | failed | aborted

L'engine controlla periodicamente le condizioni di ogni job e avvia
quando soddisfatte (window temporale + altitudine sopra minimo + weather safe).

Salva i job su file ~/.config/astroarch-bridge/scheduler.json
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException

from ..auth import require_token
from ..config import get_settings
from ..deps import Bridge, get_bridge

router = APIRouter(prefix="/api/scheduler", tags=["scheduler"],
                   dependencies=[Depends(require_token)])

JOBS_FILE = Path.home() / ".config" / "astroarch-bridge" / "scheduler.json"


def _load_jobs() -> list[dict]:
    if not JOBS_FILE.exists():
        return []
    try:
        return json.loads(JOBS_FILE.read_text("utf-8"))
    except Exception:
        return []


def _save_jobs(jobs: list[dict]) -> None:
    JOBS_FILE.parent.mkdir(parents=True, exist_ok=True)
    JOBS_FILE.write_text(json.dumps(jobs, indent=2), encoding="utf-8")


@router.get("/weather_safe")
async def weather_safe(bridge: Bridge = Depends(get_bridge)) -> dict:
    devices = await bridge.state.list_devices()
    for d in devices:
        wp = await bridge.state.get_property(d, "WEATHER_STATUS")
        if wp:
            ok = False
            for e in wp.get("elements", []):
                if e["name"].upper() == "WEATHER_OK" and e.get("value"):
                    ok = True
            return {"device": d, "safe": ok, "state": wp.get("state")}
    return {"device": None, "safe": True, "state": "unknown"}


@router.get("/jobs")
async def list_jobs() -> dict:
    return {"jobs": _load_jobs()}


@router.post("/jobs")
async def add_job(payload: dict = Body(...)) -> dict:
    jobs = _load_jobs()
    j = dict(payload)
    j.setdefault("id", f"sj_{int(time.time() * 1000)}")
    j.setdefault("status", "pending")
    j.setdefault("created_at", datetime.now(timezone.utc).isoformat())
    jobs.append(j)
    _save_jobs(jobs)
    return {"ok": True, "id": j["id"]}


@router.delete("/jobs/{job_id}")
async def delete_job(job_id: str) -> dict:
    jobs = _load_jobs()
    jobs = [j for j in jobs if j.get("id") != job_id]
    _save_jobs(jobs)
    return {"ok": True}


@router.post("/jobs/{job_id}/status")
async def set_status(job_id: str, payload: dict = Body(...)) -> dict:
    jobs = _load_jobs()
    found = False
    for j in jobs:
        if j.get("id") == job_id:
            j["status"] = payload.get("status", "pending")
            found = True
    _save_jobs(jobs)
    if not found:
        raise HTTPException(status_code=404, detail="job not found")
    return {"ok": True}


@router.get("/sky_state")
async def sky_state(bridge: Bridge = Depends(get_bridge)) -> dict:
    """Ritorna info utili per condizioni scheduler:
    - sun_alt (gradi sotto orizzonte: nautical = -12, astronomical = -18)
    - moon_alt, moon_phase
    - weather safe (se disponibile)
    """
    info: dict[str, Any] = {}
    try:
        from astropy.coordinates import (AltAz, EarthLocation, get_sun, get_body)
        from astropy.time import Time
        # Posizione: prova a ottenerla dal mount, fallback Roma
        lat_deg = 45.0
        lon_deg = 12.0
        elev_m = 100.0
        for d in await bridge.state.list_devices():
            geo = await bridge.state.get_property(d, "GEOGRAPHIC_COORD")
            if geo:
                for e in geo.get("elements", []):
                    if e["name"] == "LAT" and e.get("value") is not None:
                        lat_deg = float(e["value"])
                    if e["name"] == "LONG" and e.get("value") is not None:
                        v = float(e["value"])
                        # INDI usa 0..360; converto in -180..180
                        lon_deg = v if v <= 180 else v - 360
                    if e["name"] == "ELEV" and e.get("value") is not None:
                        elev_m = float(e["value"])
                break
        loc = EarthLocation(lat=lat_deg, lon=lon_deg, height=elev_m)
        now = Time.now()
        altaz = AltAz(obstime=now, location=loc)
        sun = get_sun(now).transform_to(altaz)
        moon = get_body("moon", now).transform_to(altaz)
        info["sun_alt"] = float(sun.alt.deg)
        info["moon_alt"] = float(moon.alt.deg)
        info["lat"] = lat_deg
        info["lon"] = lon_deg
        info["time_utc"] = now.iso
        # Twilight phase
        sa = info["sun_alt"]
        if sa > 0:
            phase = "day"
        elif sa > -6:
            phase = "civil_twilight"
        elif sa > -12:
            phase = "nautical_twilight"
        elif sa > -18:
            phase = "astronomical_twilight"
        else:
            phase = "night"
        info["twilight_phase"] = phase
    except Exception as e:
        info["error"] = str(e)
    # Weather
    try:
        ws = await weather_safe(bridge)  # reuse
        info["weather_safe"] = ws.get("safe", True)
    except Exception:
        info["weather_safe"] = True
    return info


@router.post("/jobs/{job_id}/check_conditions")
async def check_conditions(job_id: str, bridge: Bridge = Depends(get_bridge)) -> dict:
    """Valuta se un job può partire ora (window temporale + altitudine + meteo)."""
    jobs = _load_jobs()
    job = next((j for j in jobs if j.get("id") == job_id), None)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    issues = []
    sky = await sky_state(bridge)
    # Twilight
    twilight_required = job.get("require_night", True)
    if twilight_required and sky.get("sun_alt", 0) > -12:
        issues.append(f"sun above -12° ({sky.get('sun_alt'):.1f}°)")
    # Time window
    now = datetime.now(timezone.utc)
    if job.get("start_time"):
        try:
            st = datetime.fromisoformat(job["start_time"].replace("Z", "+00:00"))
            if now < st:
                issues.append(f"start_time future: {st.isoformat()}")
        except Exception:
            pass
    if job.get("end_time"):
        try:
            et = datetime.fromisoformat(job["end_time"].replace("Z", "+00:00"))
            if now > et:
                issues.append(f"past end_time {et.isoformat()}")
        except Exception:
            pass
    # Altitude
    min_alt = job.get("min_altitude", 30)
    ra_h = job.get("ra_hours")
    dec_d = job.get("dec_deg")
    if ra_h is not None and dec_d is not None and "lat" in sky:
        try:
            from astropy.coordinates import (AltAz, EarthLocation, SkyCoord)
            from astropy.time import Time
            import astropy.units as u
            loc = EarthLocation(lat=sky["lat"], lon=sky["lon"], height=100)
            sc = SkyCoord(ra=ra_h * 15 * u.deg, dec=dec_d * u.deg)
            altaz = sc.transform_to(AltAz(obstime=Time.now(), location=loc))
            target_alt = float(altaz.alt.deg)
            if target_alt < min_alt:
                issues.append(f"alt {target_alt:.1f}° < min {min_alt}°")
        except Exception as e:
            issues.append(f"alt calc failed: {e}")
    # Weather
    if not sky.get("weather_safe", True):
        issues.append("weather unsafe")
    return {"job_id": job_id, "can_run": len(issues) == 0, "issues": issues, "sky": sky}