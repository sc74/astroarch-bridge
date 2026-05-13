"""Route /api/system: stato globale, snapshot, info."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from .. import __version__
from ..auth import require_token
from ..deps import Bridge, get_bridge

router = APIRouter(prefix="/api/system", tags=["system"], dependencies=[Depends(require_token)])


@router.get("/info")
async def info(bridge: Bridge = Depends(get_bridge)) -> dict:
    return {
        "name": "astroarch-bridge",
        "version": __version__,
        "developer": "Zarletti-Osservatorio Jupiter",
    }


@router.get("/snapshot")
async def snapshot(bridge: Bridge = Depends(get_bridge)) -> dict:
    return await bridge.state.snapshot()


@router.get("/connections")
async def connections(bridge: Bridge = Depends(get_bridge)) -> dict:
    snap = await bridge.state.snapshot()
    return snap["connections"]


@router.get("/devices")
async def devices(bridge: Bridge = Depends(get_bridge)) -> dict:
    return {"devices": await bridge.state.list_devices()}


@router.get("/camera_roles")
async def camera_roles(bridge: Bridge = Depends(get_bridge)) -> dict:
    """Identifica la camera primaria (imaging) e quella di guida.

    Strategia:
    1. Chiede a PHD2 quale camera sta usando (= guide)
    2. Heuristic sui nomi (ASI120/290/174/Guide/Guider)
    3. Se solo una camera -> primary
    """
    cameras = await bridge.state.find_devices_by_role("CCD_EXPOSURE")
    guide: str | None = None
    method = "none"

    # Step 1: PHD2 -> camera attiva è la guida
    try:
        if bridge.phd2.state == "connected":
            eq = await bridge.phd2.call("get_current_equipment", timeout=4.0)
            if isinstance(eq, dict):
                cam_info = eq.get("camera") or {}
                cam_name = (cam_info.get("name") or "").strip()
                if cam_name:
                    cn_low = cam_name.lower()
                    for c in cameras:
                        cl = c.lower()
                        if cl in cn_low or cn_low in cl:
                            guide = c
                            method = "phd2"
                            break
    except Exception:
        pass

    # Step 2: heuristic naming
    if guide is None:
        guide_keywords = ("guide", "guider", "asi120", "asi174",
                          "asi178", "asi290", "asi585", "qhy5")
        for c in cameras:
            cl = c.lower()
            if any(k in cl for k in guide_keywords):
                guide = c
                method = "heuristic"
                break

    # Primary = primo non-guide
    primary: str | None = None
    for c in cameras:
        if c != guide:
            primary = c
            break

    if primary is None and cameras:
        primary = cameras[0]
        guide = None
        method = "single"

    return {
        "cameras": cameras,
        "primary": primary,
        "guide": guide,
        "method": method,
    }


@router.get("/simbad")
async def simbad_search(name: str) -> dict:
    """Risolve nome oggetto astronomico in RA/Dec via Sesame (CDS) usando astropy.

    Es: /api/system/simbad?name=M31
    """
    import asyncio
    from astropy.coordinates import SkyCoord
    name = name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="empty name")

    def _resolve():
        try:
            sc = SkyCoord.from_name(name)
            return {
                "name": name,
                "ra_hours": float(sc.ra.hour),
                "dec_deg": float(sc.dec.deg),
                "ra_str": sc.ra.to_string(unit="hour", sep=":", precision=2),
                "dec_str": sc.dec.to_string(unit="deg", sep=":", precision=2,
                                            alwayssign=True),
            }
        except Exception as e:
            return {"error": str(e)}

    try:
        result = await asyncio.wait_for(asyncio.to_thread(_resolve), timeout=10.0)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="SIMBAD timeout")
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


# ============================================================================
# LAUNCH GUI APPS (KStars/Ekos + PHD2) — appaiono sul monitor del RPi
# ============================================================================
#
# Lancia i binari come subprocess con DISPLAY/XAUTHORITY dell'utente
# loggato graficamente. La finestra appare sul monitor del RPi (non
# sul telefono — è il modo per "avviare il programma" da remoto).
# Pre-controlla via pgrep per evitare doppi lanci.


def _gui_env() -> dict:
    """Costruisce l'env per lanciare app GUI sull'utente grafico.
    Assume DISPLAY=:0 (single-seat RPi, è la norma su AstroArch).
    XAUTHORITY si trova in ~/.Xauthority dell'utente con UID 1000."""
    import os
    import pwd
    env = os.environ.copy()
    env["DISPLAY"] = ":0"
    try:
        u = pwd.getpwuid(1000)
        env["XAUTHORITY"] = os.path.join(u.pw_dir, ".Xauthority")
        env["HOME"] = u.pw_dir
        env["XDG_RUNTIME_DIR"] = f"/run/user/{u.pw_uid}"
        env.setdefault("DBUS_SESSION_BUS_ADDRESS",
                       f"unix:path=/run/user/{u.pw_uid}/bus")
    except KeyError:
        pass
    return env


async def _pgrep_any(*names: str) -> bool:
    """True se almeno uno dei nomi processo passati è in esecuzione.
    Cerca match esatto (-x). Utile per distinguere wrapper script da
    binari reali: es. PHD2 su AstroArch ha il wrapper `phd2` (che potrebbe
    non essere running) e il binario `/usr/bin/phd2.bin` (quello vero).
    """
    import asyncio
    for n in names:
        proc = await asyncio.create_subprocess_exec(
            "pgrep", "-x", n,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        if await proc.wait() == 0:
            return True
    return False


async def _pkill(*names: str) -> int:
    """Termina tutti i processi che matchano uno dei nomi (esatto, -x).
    Usa SIGTERM (graceful). Ritorna il numero di processi terminati."""
    import asyncio
    killed = 0
    for n in names:
        proc = await asyncio.create_subprocess_exec(
            "pkill", "-x", n,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        if await proc.wait() == 0:
            killed += 1
    return killed


async def _launch_detached(binary: str, *args: str) -> bool:
    """Spawn binary come daemon detached, return immediato. True se
    il binario esiste."""
    import asyncio
    import shutil
    if shutil.which(binary) is None:
        return False
    # nohup + setsid → il processo sopravvive al return della HTTP request
    await asyncio.create_subprocess_exec(
        "setsid", "-f", binary, *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        env=_gui_env(),
        start_new_session=True,
    )
    return True


@router.get("/gui_apps_state")
async def gui_apps_state() -> dict:
    """Stato live di KStars e PHD2 (in esecuzione o no).
    PHD2 su AstroArch è un wrapper script che lancia /usr/bin/phd2.bin
    → cerchiamo entrambi i nomi processo per essere robusti."""
    return {
        "kstars_running": await _pgrep_any("kstars", "kstars.bin"),
        "phd2_running":   await _pgrep_any("phd2", "phd2.bin"),
    }


@router.post("/launch_kstars")
async def launch_kstars() -> dict:
    """Avvia KStars sul desktop del RPi (DISPLAY=:0).
    Se già in esecuzione, no-op."""
    import logging as _log
    _logger = _log.getLogger("astroarch_bridge.system")
    if await _pgrep_any("kstars", "kstars.bin"):
        return {"ok": True, "started": False, "already_running": True}
    ok = await _launch_detached("kstars")
    if not ok:
        raise HTTPException(status_code=503,
            detail="kstars binary not found in PATH")
    _logger.info("kstars launched via DISPLAY=:0")
    return {"ok": True, "started": True, "already_running": False}


@router.post("/launch_phd2")
async def launch_phd2() -> dict:
    """Avvia PHD2 sul desktop del RPi. Se già in esecuzione, no-op."""
    import logging as _log
    _logger = _log.getLogger("astroarch_bridge.system")
    if await _pgrep_any("phd2", "phd2.bin"):
        return {"ok": True, "started": False, "already_running": True}
    ok = await _launch_detached("phd2")
    if not ok:
        raise HTTPException(status_code=503,
            detail="phd2 binary not found in PATH")
    _logger.info("phd2 launched via DISPLAY=:0")
    return {"ok": True, "started": True, "already_running": False}


@router.post("/kill_kstars")
async def kill_kstars() -> dict:
    """Termina KStars/Ekos (SIGTERM graceful).
    L'app lo chiama solo quando il sistema Ekos è DISATTIVATO, così
    l'utente non chiude per sbaglio una sessione attiva di osservazione."""
    import logging as _log
    _logger = _log.getLogger("astroarch_bridge.system")
    n = await _pkill("kstars", "kstars.bin")
    _logger.info("kill_kstars: terminated %d processes", n)
    return {"ok": True, "killed": n}


@router.post("/kill_phd2")
async def kill_phd2() -> dict:
    """Termina PHD2 (SIGTERM graceful)."""
    import logging as _log
    _logger = _log.getLogger("astroarch_bridge.system")
    n = await _pkill("phd2", "phd2.bin")
    _logger.info("kill_phd2: terminated %d processes", n)
    return {"ok": True, "killed": n}


# ============================================================================
# EKOS MASTER CONTROL (clone del quadratino "Start/Stop Ekos" in Setup)
# ============================================================================
#
# Dal pulsante Dashboard "Attiva/Disattiva" della app:
#   - GET  /api/system/ekos_state    → status corrente (ekos + indi)
#   - POST /api/system/ekos_start    → Ekos.start() (carica profilo + INDI + connetti)
#   - POST /api/system/ekos_stop     → Ekos.stop()  (disconnetti + chiudi INDI)
#   - POST /api/system/ekos_toggle   → decide auto in base allo stato
#
# Ekos enum CommunicationStatus:
#   0=Idle, 1=Pending, 2=Started, 3=Error

_EKOS_STATUS_LABELS = {
    0: "idle", 1: "pending", 2: "started", 3: "error",
}


def _label_active(ekos_int: int | None, indi_int: int | None,
                  connected_devices: int = 0) -> str:
    """Restituisce uno dei label semplici per la UI:
    'active' = tutto su, 'inactive' = tutto giù, 'pending' = transizione,
    'error' = errore, 'unknown' = non leggibile.

    NOTA IMPORTANTE: `indiStatus` di Ekos resta a 1=Pending per sempre se
    UNO solo dei driver del profilo non riesce a connettersi (es. XAGYL
    Wheel offline, Weather Watcher offline). Quindi NON possiamo usarlo
    come hard gate per "tutto OK".
    Segnale primario:
      • ekosStatus == 2 (Started)
      • il bridge vede device INDI connessi (almeno 1)
    """
    if ekos_int is None:
        return "unknown"
    # 3 = Error
    if ekos_int == 3:
        return "error"
    if ekos_int == 2:
        # Ekos avviato: se vediamo device connessi via INDI, è attivo.
        if connected_devices > 0:
            return "active"
        # Avviato ma ancora nessun device → fase di apertura driver.
        return "pending"
    if ekos_int == 1:
        return "pending"
    # ekos_int == 0 = Idle (fermo)
    return "inactive"


@router.get("/ekos_state")
async def ekos_state(bridge: Bridge = Depends(get_bridge)) -> dict:
    """Stato master di Ekos + INDI per il pulsante Attiva/Disattiva.
    Combina lo status DBus di Ekos con il numero di device che il bridge
    vede connessi sull'INDI server: questo è più affidabile di indiStatus
    perché ignora i driver opzionali che falliscono."""
    from .capture_ekos import _dbus_call, EKOS_DBUS_SERVICE
    ekos_path = "/KStars/Ekos"

    rc1, raw1 = await _dbus_call(EKOS_DBUS_SERVICE, ekos_path,
                                  "org.kde.kstars.Ekos.ekosStatus")
    rc2, raw2 = await _dbus_call(EKOS_DBUS_SERVICE, ekos_path,
                                  "org.kde.kstars.Ekos.indiStatus")
    ekos_int = int(raw1) if rc1 == 0 and raw1.lstrip("-").isdigit() else None
    indi_int = int(raw2) if rc2 == 0 and raw2.lstrip("-").isdigit() else None

    # Conta device INDI visibili e online dal bridge.
    try:
        devices = await bridge.state.list_devices()
        n_devices = len(devices)
    except Exception:
        n_devices = 0

    return {
        "ekos_status": ekos_int,
        "ekos_status_label": _EKOS_STATUS_LABELS.get(ekos_int, "unknown"),
        "indi_status": indi_int,
        "indi_status_label": _EKOS_STATUS_LABELS.get(indi_int, "unknown"),
        "connected_devices": n_devices,
        "active": _label_active(ekos_int, indi_int, n_devices),
    }


@router.post("/ekos_start")
async def ekos_start() -> dict:
    """Avvia Ekos col profilo attivo (= quadratino Setup di Ekos, modalità ON)."""
    from .capture_ekos import _dbus_call, EKOS_DBUS_SERVICE
    rc, raw = await _dbus_call(EKOS_DBUS_SERVICE, "/KStars/Ekos",
                                "org.kde.kstars.Ekos.start")
    # Q_NOREPLY: rc=0 quasi sempre, comportamento "fire and forget".
    return {"ok": rc == 0, "raw": raw}


@router.post("/ekos_stop")
async def ekos_stop() -> dict:
    """Ferma Ekos (= quadratino Setup di Ekos, modalità OFF)."""
    from .capture_ekos import _dbus_call, EKOS_DBUS_SERVICE
    rc, raw = await _dbus_call(EKOS_DBUS_SERVICE, "/KStars/Ekos",
                                "org.kde.kstars.Ekos.stop")
    return {"ok": rc == 0, "raw": raw}


@router.post("/ekos_connect_devices")
async def ekos_connect_devices() -> dict:
    """Connetti tutti i driver INDI del profilo. Equivalente a 'Connetti'
    nel pannello Setup di Ekos (icona spina)."""
    from .capture_ekos import _dbus_call, EKOS_DBUS_SERVICE
    rc, raw = await _dbus_call(EKOS_DBUS_SERVICE, "/KStars/Ekos",
                                "org.kde.kstars.Ekos.connectDevices")
    return {"ok": rc == 0, "raw": raw}


@router.post("/ekos_disconnect_devices")
async def ekos_disconnect_devices() -> dict:
    """Disconnetti tutti i driver INDI del profilo."""
    from .capture_ekos import _dbus_call, EKOS_DBUS_SERVICE
    rc, raw = await _dbus_call(EKOS_DBUS_SERVICE, "/KStars/Ekos",
                                "org.kde.kstars.Ekos.disconnectDevices")
    return {"ok": rc == 0, "raw": raw}


@router.post("/ekos_toggle")
async def ekos_toggle(bridge: Bridge = Depends(get_bridge)) -> dict:
    """Toggle automatico: legge lo stato, poi start/stop in base a quello.
    Questo è ciò che usa il pulsante Attiva/Disattiva della Dashboard."""
    import asyncio as _asyncio
    from .capture_ekos import _dbus_call, EKOS_DBUS_SERVICE
    ekos_path = "/KStars/Ekos"

    rc1, raw1 = await _dbus_call(EKOS_DBUS_SERVICE, ekos_path,
                                  "org.kde.kstars.Ekos.ekosStatus")
    rc2, raw2 = await _dbus_call(EKOS_DBUS_SERVICE, ekos_path,
                                  "org.kde.kstars.Ekos.indiStatus")
    ekos_int = int(raw1) if rc1 == 0 and raw1.lstrip("-").isdigit() else None
    indi_int = int(raw2) if rc2 == 0 and raw2.lstrip("-").isdigit() else None
    try:
        n_devices = len(await bridge.state.list_devices())
    except Exception:
        n_devices = 0
    cur = _label_active(ekos_int, indi_int, n_devices)

    if cur == "active":
        # Tutto su → spegni
        # Prima disconnect dei device (rispetta l'ordine pulito di Ekos),
        # poi stop. Doppia chiamata per il pattern Q_NOREPLY race-safe.
        await _dbus_call(EKOS_DBUS_SERVICE, ekos_path,
                          "org.kde.kstars.Ekos.disconnectDevices")
        await _asyncio.sleep(0.20)
        await _dbus_call(EKOS_DBUS_SERVICE, ekos_path,
                          "org.kde.kstars.Ekos.stop")
        action = "stopping"
    else:
        # Tutto giù o errore → accendi.
        # start() di Ekos avvia INDI + apre i driver. Se il profilo ha
        # autoConnect=true, anche connectDevices è implicito.
        await _dbus_call(EKOS_DBUS_SERVICE, ekos_path,
                          "org.kde.kstars.Ekos.start")
        await _asyncio.sleep(0.30)
        # Ridondante ma robusto: forziamo anche il connect dei device,
        # nel caso il profilo non sia in autoConnect.
        await _dbus_call(EKOS_DBUS_SERVICE, ekos_path,
                          "org.kde.kstars.Ekos.connectDevices")
        action = "starting"
    return {"ok": True, "from": cur, "action": action}


# ============================================================================
# QR DI ACCOPPIAMENTO con IP Tailscale
# ============================================================================
#
# Il QR mostrato dalla dashboard desktop usa l'IP locale (LAN) come primo
# tentativo. Funziona se telefono e RPi sono sulla stessa WiFi, ma da fuori
# casa serve l'IP Tailscale.
# Questo endpoint genera il QR SEMPRE con l'IP Tailscale (se disponibile),
# fallback all'IP LAN, fallback finale 127.0.0.1.
#
# Endpoint:
#   GET /api/system/qr           → JSON {host, port, token, payload, png_base64}
#   GET /api/system/qr?fmt=png   → image/png binario (Content-Type: image/png)

def _bridge_host_for_qr() -> str:
    """Determina l'host da mettere nel QR.
    Ordine: tailscale ip -4 → primo IP non-loopback non-link-local → '127.0.0.1'.
    """
    import subprocess
    # Tentativo 1: Tailscale
    try:
        r = subprocess.run(["tailscale", "ip", "-4"],
                           capture_output=True, text=True, timeout=2.0)
        if r.returncode == 0:
            ip = r.stdout.strip().splitlines()[0].strip() if r.stdout.strip() else ""
            if ip and not ip.startswith("127."):
                return ip
    except Exception:
        pass
    # Tentativo 2: hostname -I (prima interfaccia LAN, no loopback)
    try:
        r = subprocess.run(["hostname", "-I"],
                           capture_output=True, text=True, timeout=1.0)
        if r.returncode == 0:
            for ip in r.stdout.strip().split():
                if ip and not ip.startswith("127.") and not ip.startswith("169.254."):
                    return ip
    except Exception:
        pass
    return "127.0.0.1"


@router.get("/qr")
async def qr_pairing(fmt: str = "json"):
    """Genera il QR di accoppiamento con l'IP Tailscale.

    Query:
      fmt: "json" (default, ritorna anche PNG in base64) oppure "png" (binary)
    """
    import base64
    import io
    import json as _json
    from fastapi.responses import Response

    from ..config import get_settings
    settings = get_settings()
    host = _bridge_host_for_qr()
    port = settings.port
    token = settings.resolve_token()  # legge da file se .token è vuoto

    payload = _json.dumps({
        "v": 1,
        "type": "astroarch-bridge",
        "host": host,
        "port": port,
        "token": token,
    }, separators=(",", ":"))

    # Genera PNG del QR (M error correction, dimensioni standard per scan rapido)
    try:
        import qrcode
        import qrcode.constants
        qr = qrcode.QRCode(version=None,
                           error_correction=qrcode.constants.ERROR_CORRECT_M,
                           box_size=8, border=2)
        qr.add_data(payload)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        png_bytes = buf.getvalue()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"QR generation failed: {e}")

    if fmt.lower() == "png":
        return Response(content=png_bytes, media_type="image/png",
                        headers={"Cache-Control": "no-store"})

    return {
        "host": host,
        "port": port,
        "token": token,
        "payload": payload,
        "png_base64": base64.b64encode(png_bytes).decode("ascii"),
    }
