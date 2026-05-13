"""Route /api/capture - integrazione con Ekos via DBus.

Permette di pianificare una sequenza in Ekos Capture caricando un file .esq.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from xml.sax.saxutils import escape

from fastapi import APIRouter, Body, Depends, HTTPException

from ..auth import require_token
from ..config import get_settings
from ..deps import Bridge, get_bridge

router = APIRouter(prefix="/api/capture", tags=["capture"], dependencies=[Depends(require_token)])
_logger = logging.getLogger("astroarch_bridge.capture_ekos")

EKOS_DBUS_SERVICE = "org.kde.kstars"
EKOS_CAPTURE_PATH = "/KStars/Ekos/Capture"

# Dove Ekos persiste le impostazioni Capture per train (KStars 3.x)
KSTARS_USERDB = Path.home() / ".local/share/kstars/userdb.sqlite"


def _read_ekos_capture_settings(train_id: int | None = None) -> dict:
    """Legge dalla userdb di KStars le impostazioni Capture configurate
    dall'utente (cartella di salvataggio, placeholder format, formato suffix).

    NON modifica niente — è SOLO una lettura. Serve per includere nell'ESQ
    gli stessi valori che l'utente vede nella sua UI Ekos, così l'app non
    sovrascrive mai i suoi path con valori vuoti.

    Args:
      train_id: opzionale, se None usa il primo train trovato

    Returns: dict con chiavi (può essere vuoto se db non leggibile):
      fits_dir            str   — cartella salvataggio (fileDirectoryT)
      placeholder_format  str   — pattern path (placeholderFormatT)
      placeholder_suffix  int   — formatSuffixN
      target_name         str   — targetNameT
      formats_list        list  — formatsList
      filters_list        list  — filtersList
    """
    if not KSTARS_USERDB.exists():
        _logger.warning("KStars userdb not found at %s", KSTARS_USERDB)
        return {}
    try:
        conn = sqlite3.connect(f"file:{KSTARS_USERDB}?mode=ro", uri=True,
                                timeout=2.0)
        try:
            cur = conn.cursor()
            if train_id is not None:
                cur.execute(
                    "SELECT settings FROM opticaltrainsettings "
                    "WHERE opticaltrain = ? LIMIT 1", (train_id,))
            else:
                # Prendi il primo train con opticaltrain piccolo (escludiamo
                # 4294967295 che è il "global default")
                cur.execute(
                    "SELECT settings FROM opticaltrainsettings "
                    "WHERE opticaltrain < 1000 "
                    "ORDER BY opticaltrain LIMIT 1")
            row = cur.fetchone()
        finally:
            conn.close()
        if not row:
            return {}
        settings = json.loads(row[0])
        # La struttura è {"0": {capture settings}, "1": {focus settings}, ...}
        # Capture è sotto la chiave "0"
        cap = settings.get("0") or {}
        out = {
            "fits_dir": cap.get("fileDirectoryT"),
            "placeholder_format": cap.get("placeholderFormatT"),
            "placeholder_suffix": cap.get("formatSuffixN"),
            "target_name": cap.get("targetNameT"),
            "formats_list": cap.get("formatsList") or [],
            "filters_list": cap.get("filtersList") or [],
        }
        _logger.info("Ekos capture settings from userdb: fits_dir=%r "
                     "placeholder=%r suffix=%s",
                     out["fits_dir"], out["placeholder_format"],
                     out["placeholder_suffix"])
        return out
    except Exception as e:
        _logger.warning("cannot read Ekos capture settings: %s", e)
        return {}


def _read_active_train_id() -> int | None:
    """Legge CaptureTrainID da ~/.config/kstarsrc."""
    cfg = Path.home() / ".config/kstarsrc"
    if not cfg.exists():
        return None
    try:
        for line in cfg.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("CaptureTrainID="):
                v = line.split("=", 1)[1].strip()
                return int(v) if v.lstrip("-").isdigit() else None
    except Exception:
        return None
    return None


def _frame_type_label(ft: str) -> str:
    """FRAME_LIGHT -> Light"""
    return ft.replace("FRAME_", "").capitalize()


def _esq_for_jobs(jobs: list[dict], target_name: str = "",
                  fits_dir: str | None = None,
                  placeholder_format: str | None = None,
                  placeholder_suffix: int | None = None,
                  upload_mode: int | None = None) -> str:
    """Genera XML .esq compatibile Ekos 2.x da lista di job dict.

    NON-INVASIVENESS RULE (v0.2.25): se l'app NON passa un valore esplicito,
    NON includiamo il relativo tag nell'ESQ. In quel caso Ekos usa la sua
    impostazione configurata in Preferences → FITS Settings (FITS Default
    Folder) e il PlaceholderFormat globale. L'app è una GUI: deve essere
    trasparente, non sovrascrivere silenziosamente i path dell'utente.

    Args:
      fits_dir: se None, omette `<FITSDirectory>` → Ekos default
      placeholder_format: se None, omette `<PlaceholderFormat>` → Ekos default
      upload_mode: se None, omette `<UploadMode>` → Ekos default
                   (0=Client, 1=Local, 2=Both)
    """
    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             "<SequenceQueue version='2.6'>",
             "<GuideDeviation enabled='false'>2</GuideDeviation>",
             "<HFRCheck enabled='false'><HFRDeviation>2</HFRDeviation>"
             "<HFRCheckAlgorithm>0</HFRCheckAlgorithm>"
             "<HFRCheckThreshold>10</HFRCheckThreshold>"
             "<HFRCheckFrames>1</HFRCheckFrames></HFRCheck>",
             "<RefocusOnTemperatureDelta enabled='false'>1</RefocusOnTemperatureDelta>",
             "<RefocusEveryN enabled='false'>60</RefocusEveryN>",
             "<RefocusOnMeridianFlip enabled='false'/>"]
    for job in jobs:
        f = job.get("filter") or ""
        target = job.get("targetName") or target_name or ""
        cap_fmt = (job.get("captureFormat") or "RAW").upper()
        cap_fmt_label = "RAW 16" if cap_fmt == "RAW" else "RGB"
        encoding = (job.get("transferFormat") or "FITS").upper()
        if encoding == "NATIVE":
            encoding = "Native"
        gain = job.get("gain", 100)
        offset = job.get("offset", 50)
        bin_x = job.get("binX", 1)
        bin_y = job.get("binY", bin_x)
        count = int(job.get("count", 1))
        exp = float(job.get("exposureSec", 60))
        delay = float(job.get("delaySec", 0))
        ft_label = _frame_type_label(job.get("frameType", "FRAME_LIGHT"))
        dither = "1" if job.get("ditherEachFrame") else "0"
        parts.append("<Job>")
        parts.append(f"<Exposure>{exp:g}</Exposure>")
        parts.append(f"<Format>{escape(cap_fmt_label)}</Format>")
        parts.append(f"<Encoding>{escape(encoding)}</Encoding>")
        parts.append(f"<Binning><X>{bin_x}</X><Y>{bin_y}</Y></Binning>")
        parts.append("<Frame><X>0</X><Y>0</Y><W>0</W><H>0</H></Frame>")
        if f:
            parts.append(f"<Filter>{escape(f)}</Filter>")
        parts.append(f"<Type>{ft_label}</Type>")
        parts.append(f"<Count>{count}</Count>")
        parts.append(f"<Delay>{int(delay)}</Delay>")
        if target:
            parts.append(f"<TargetName>{escape(target)}</TargetName>")
        parts.append(f"<GuideDitherPerJob>{dither}</GuideDitherPerJob>")
        # NON-INVASIVENESS: tag opzionali, omessi se non specificati esplicitamente.
        # Quando omessi Ekos usa le impostazioni della sua UI/Preferenze.
        if fits_dir:
            parts.append(f"<FITSDirectory>{escape(fits_dir)}</FITSDirectory>")
        if placeholder_format:
            parts.append(f"<PlaceholderFormat>{escape(placeholder_format)}</PlaceholderFormat>")
            # Suffix: usa quello passato, default 1 (= numerazione)
            suf = placeholder_suffix if placeholder_suffix is not None else 1
            parts.append(f"<PlaceholderSuffix>{int(suf)}</PlaceholderSuffix>")
        if upload_mode is not None:
            parts.append(f"<UploadMode>{int(upload_mode)}</UploadMode>")
        # Gain/offset come PropertyVector
        parts.append("<Properties>")
        parts.append(f"<PropertyVector name='CCD_CONTROLS'>"
                     f"<OneElement name='Gain'>{gain:g}</OneElement></PropertyVector>")
        parts.append(f"<PropertyVector name='CCD_GAIN'>"
                     f"<OneElement name='GAIN'>{gain:g}</OneElement></PropertyVector>")
        parts.append(f"<PropertyVector name='CCD_OFFSET'>"
                     f"<OneElement name='OFFSET'>{offset:g}</OneElement></PropertyVector>")
        parts.append("</Properties>")
        parts.append("<Calibration><PreAction><Type>1</Type></PreAction>"
                     "<FlatDuration dark='false'><Type>ADU</Type>"
                     "<Value>25000</Value><Tolerance>1000</Tolerance>"
                     "<SkyFlat>false</SkyFlat></FlatDuration></Calibration>")
        parts.append("</Job>")
    parts.append("</SequenceQueue>")
    return "\n".join(parts)


async def _dbus_call(*args: str, timeout: float = 10.0) -> tuple[int, str]:
    """Esegue qdbus6 e ritorna (returncode, stdout)."""
    env = os.environ.copy()
    uid = os.getuid()
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{uid}/bus")
    proc = await asyncio.create_subprocess_exec(
        "qdbus6", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return -1, "timeout"
    return proc.returncode, stdout.decode("utf-8", "replace").strip()


@router.get("/ekos_user_settings")
async def ekos_user_settings() -> dict:
    """Restituisce le impostazioni Capture che l'utente ha settato dentro
    Ekos sul desktop (cartella di salvataggio, placeholder format, ...).

    Letti read-only dal database utente di KStars (~/.local/share/kstars/
    userdb.sqlite, tabella opticaltrainsettings). NON modifichiamo niente
    — la app è una GUI, può solo leggere.

    Utile per:
      - mostrare nella UI dell'app quale cartella verrà usata
      - debug del problema "Ekos mi ha cancellato la cartella" (era un
        bug, fix in v0.2.28)
    """
    train_id = _read_active_train_id()
    settings = _read_ekos_capture_settings(train_id)
    return {
        "train_id": train_id,
        "userdb_path": str(KSTARS_USERDB),
        "userdb_exists": KSTARS_USERDB.exists(),
        **settings,
    }


@router.get("/ekos_alive")
async def ekos_alive() -> dict:
    """Verifica se Ekos è raggiungibile via DBus."""
    rc, out = await _dbus_call(EKOS_DBUS_SERVICE, EKOS_CAPTURE_PATH,
                                "org.kde.kstars.Ekos.Capture.getJobCount")
    return {
        "alive": rc == 0,
        "job_count": int(out) if rc == 0 and out.isdigit() else None,
        "raw": out,
    }


@router.post("/ekos_run")
async def ekos_run(payload: dict = Body(...)) -> dict:
    """Genera ESQ dai jobs ricevuti, lo carica in Ekos Capture, avvia.

    Body:
      jobs: list di CaptureJob serializzati (vedi /api/capture/ekos_run/preview_esq)
      target: str (target name, default "AstroarchInterface")
      fits_dir: str opzionale
      train: str (optical train, default "")
      master: bool (default true)
      auto_start: bool (default true)
    """
    jobs = payload.get("jobs") or []
    if not jobs:
        raise HTTPException(status_code=400, detail="jobs required")
    target = payload.get("target") or "AstroarchInterface"
    train = payload.get("train") or ""
    master = bool(payload.get("master", True))
    auto_start = bool(payload.get("auto_start", True))

    # ========================================================================
    # v0.2.28: LEGGE LE IMPOSTAZIONI UTENTE DA EKOS (userdb.sqlite)
    # ========================================================================
    # Prima di v0.2.25 il bridge forzava fits_dir a
    #   ~/Pictures/Ekos/AstroarchInterface/
    # In v0.2.25 abbiamo rimosso il default ma omettendo i tag dall'ESQ —
    # Ekos al loadSequenceQueue si trovava i campi VUOTI e li sovrascriveva
    # nella sua UI (cartella e formato cancellati, job si fermava).
    #
    # FIX DEFINITIVO: leggiamo `fileDirectoryT` e `placeholderFormatT` dal
    # database utente di KStars (tabella opticaltrainsettings) per il train
    # attivo. Sono ESATTAMENTE i valori che l'utente vede nella sua UI Ekos.
    # Li mettiamo nell'ESQ così l'app non modifica mai le sue scelte.
    # L'app può ancora forzare override esplicito passandoli nel payload.
    user_settings = _read_ekos_capture_settings(_read_active_train_id())
    fits_dir = payload.get("fits_dir") or user_settings.get("fits_dir")
    placeholder_format = (payload.get("placeholder_format")
                          or user_settings.get("placeholder_format"))
    placeholder_suffix = (payload.get("placeholder_suffix")
                          if payload.get("placeholder_suffix") is not None
                          else user_settings.get("placeholder_suffix"))
    upload_mode = payload.get("upload_mode")
    _logger.info("ekos_run: using fits_dir=%r placeholder=%r suffix=%s",
                 fits_dir, placeholder_format, placeholder_suffix)

    # Genera ESQ
    esq = _esq_for_jobs(jobs, target_name=target, fits_dir=fits_dir,
                       placeholder_format=placeholder_format,
                       placeholder_suffix=placeholder_suffix,
                       upload_mode=upload_mode)

    # L'ESQ è un FILE temporaneo di servizio: lo salviamo in /tmp, non
    # nella cartella immagini dell'utente. Sopravvive solo finché Ekos
    # lo legge (qualche secondo) ed è rigenerato ad ogni run.
    save_dir = Path("/tmp/astroarch_bridge")
    save_dir.mkdir(parents=True, exist_ok=True)
    esq_path = save_dir / f"sequence_{int(time.time())}.esq"
    esq_path.write_text(esq, encoding="utf-8")

    # Carica in Ekos via DBus
    rc1, out1 = await _dbus_call(
        EKOS_DBUS_SERVICE, EKOS_CAPTURE_PATH,
        "org.kde.kstars.Ekos.Capture.clearSequenceQueue",
    )
    rc2, out2 = await _dbus_call(
        EKOS_DBUS_SERVICE, EKOS_CAPTURE_PATH,
        "org.kde.kstars.Ekos.Capture.loadSequenceQueue",
        str(esq_path),
        train,
        "true" if master else "false",
        target,
    )
    if rc2 != 0 or out2.lower() == "false":
        raise HTTPException(status_code=500, detail=f"loadSequenceQueue failed: {out2}")

    started = False
    start_msg = ""
    if auto_start:
        rc3, out3 = await _dbus_call(
            EKOS_DBUS_SERVICE, EKOS_CAPTURE_PATH,
            "org.kde.kstars.Ekos.Capture.start", train,
        )
        started = rc3 == 0
        start_msg = out3
    return {
        "ok": True,
        "esq_path": str(esq_path),
        "loaded": rc2 == 0,
        "load_response": out2,
        "started": started,
        "start_response": start_msg,
        "jobs_count": len(jobs),
    }


@router.get("/ekos_status")
async def ekos_status() -> dict:
    """Stato live della sequenza Ekos Capture."""
    out: dict = {}
    rc, val = await _dbus_call(EKOS_DBUS_SERVICE, EKOS_CAPTURE_PATH,
                                "org.kde.kstars.Ekos.Capture.getJobCount")
    out["job_count"] = int(val) if rc == 0 and val.isdigit() else 0
    rc, val = await _dbus_call(EKOS_DBUS_SERVICE, EKOS_CAPTURE_PATH,
                                "org.kde.kstars.Ekos.Capture.getActiveJobID")
    out["active_job_id"] = int(val) if rc == 0 and val.lstrip("-").isdigit() else None
    rc, val = await _dbus_call(EKOS_DBUS_SERVICE, EKOS_CAPTURE_PATH,
                                "org.kde.kstars.Ekos.Capture.getActiveJobRemainingTime")
    out["remaining_seconds"] = int(val) if rc == 0 and val.lstrip("-").isdigit() else None
    rc, val = await _dbus_call(EKOS_DBUS_SERVICE, EKOS_CAPTURE_PATH,
                                "org.kde.kstars.Ekos.Capture.getOverallRemainingTime")
    out["overall_remaining_seconds"] = int(val) if rc == 0 and val.lstrip("-").isdigit() else None
    if out.get("active_job_id") is not None and out["active_job_id"] >= 0:
        rc, val = await _dbus_call(EKOS_DBUS_SERVICE, EKOS_CAPTURE_PATH,
                                    "org.kde.kstars.Ekos.Capture.getJobImageProgress",
                                    str(out["active_job_id"]))
        out["job_image_progress"] = int(val) if rc == 0 and val.lstrip("-").isdigit() else None
        rc, val = await _dbus_call(EKOS_DBUS_SERVICE, EKOS_CAPTURE_PATH,
                                    "org.kde.kstars.Ekos.Capture.getJobImageCount",
                                    str(out["active_job_id"]))
        out["job_image_count"] = int(val) if rc == 0 and val.lstrip("-").isdigit() else None
        rc, val = await _dbus_call(EKOS_DBUS_SERVICE, EKOS_CAPTURE_PATH,
                                    "org.kde.kstars.Ekos.Capture.getJobState",
                                    str(out["active_job_id"]))
        out["job_state"] = val if rc == 0 else None
    return out


@router.post("/ekos_abort")
async def ekos_abort() -> dict:
    rc, out = await _dbus_call(EKOS_DBUS_SERVICE, EKOS_CAPTURE_PATH,
                                "org.kde.kstars.Ekos.Capture.abort", "")
    return {"ok": rc == 0, "raw": out}


@router.post("/ekos_clear")
async def ekos_clear() -> dict:
    rc, out = await _dbus_call(EKOS_DBUS_SERVICE, EKOS_CAPTURE_PATH,
                                "org.kde.kstars.Ekos.Capture.clearSequenceQueue")
    return {"ok": rc == 0, "raw": out}


@router.post("/preview_esq")
async def preview_esq(payload: dict = Body(...)) -> dict:
    """Genera ESQ ma non lo invia. Utile per debug.

    Come /ekos_run: per default legge fits_dir/placeholder_format/suffix
    dalla userdb di KStars (ciò che l'utente vede in Ekos). Override
    espliciti possibili.
    """
    jobs = payload.get("jobs") or []
    target = payload.get("target") or ""
    user_settings = _read_ekos_capture_settings(_read_active_train_id())
    return {"esq": _esq_for_jobs(jobs, target_name=target,
        fits_dir=payload.get("fits_dir") or user_settings.get("fits_dir"),
        placeholder_format=(payload.get("placeholder_format")
                            or user_settings.get("placeholder_format")),
        placeholder_suffix=(payload.get("placeholder_suffix")
                            if payload.get("placeholder_suffix") is not None
                            else user_settings.get("placeholder_suffix")),
        upload_mode=payload.get("upload_mode"))}
