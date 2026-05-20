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
        # v0.3.3: setpoint temperatura per-job (opzionale).
        # Se il job ha `temperatureC` valorizzata, emettiamo i tag
        # <TemperatureValue> + <TemperatureEnforced>1 così Ekos attende
        # il setpoint prima di avviare lo scatto del job. Se None,
        # NON emettiamo nulla (Ekos parte alla T attuale del cooler —
        # non-invasiveness rule, vedi commento in _esq_for_jobs docstring).
        temp_c = job.get("temperatureC")
        if temp_c is None:
            # backward-compat: accetta anche la chiave plain "temperature"
            temp_c = job.get("temperature")
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
        if temp_c is not None:
            try:
                temp_f = float(temp_c)
                parts.append(f"<TemperatureValue>{temp_f:g}</TemperatureValue>")
                parts.append("<TemperatureEnforced>1</TemperatureEnforced>")
            except (TypeError, ValueError):
                pass
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


# ============================================================================
# v0.3.4: AUTO-DITHER tra scatti, bridge-native.
# ============================================================================
# Problema risolto:
#   Il tag <GuideDitherPerJob>1</GuideDitherPerJob> nell'ESQ funziona solo se
#   Ekos.Guide è collegato a PHD2 (`connectGuider`). Spesso quella connessione
#   non è stabilita (l'utente non ha mai cliccato "Connect external" in Ekos),
#   e il dither nativo Ekos non parte mai.
#
# Soluzione:
#   Il bridge intercetta via dbus-monitor il signal `Ekos.Capture.captureComplete`
#   (emesso dopo ogni frame salvato). Quando si attiva e il job ha
#   `ditherEachFrame=true`, il bridge:
#     1) Mette in PAUSA la sequenza Ekos (`Capture.pause`)
#     2) Chiama `phd2.dither()` direttamente sul bridge (RPC PHD2)
#     3) Aspetta che PHD2 abbia stabilizzato (settle_done event)
#     4) Riavvia la sequenza Ekos (`Capture.start(train)`)
#
# Funziona indipendentemente dal fatto che Ekos↔PHD2 siano collegati o no.
# Compatibile col dither nativo Ekos: se quello è attivo lo lasciamo fare,
# il bridge serve solo come SAFETY NET.
#
# Vive come task asincrono singleton — start/stop pilotati da /ekos_run e
# /ekos_abort.
# ============================================================================

_auto_dither_task: asyncio.Task | None = None
_auto_dither_bridge = None  # type: ignore  # populated by ekos_run
_auto_dither_state = {
    "enabled": False,
    "train": "",
    "frames_seen": 0,
    "last_dither_ts": None,
    "last_error": None,
    "amount": 3.0,
    "settle_pixels": 1.5,
    "settle_time": 10.0,
    "settle_timeout": 60.0,
}


def _read_ekos_dither_settings() -> dict:
    """Legge le impostazioni Dither che l'utente ha configurato in Ekos
    (~/.config/kstarsrc sezione [Guide]). NON le modifica.
    Mappa keys Ekos → keys del nostro state:
      DitherPixels → amount      (default 3.0 — quanto sposta in pixel)
      DitherSettle → settle_time (default 10.0 — secondi MINIMI di settling)

    NB: NON usiamo `DitherThreshold` come settle_pixels: in Ekos quella chiave
    è la soglia di errore guida sopra la quale Ekos halt/alerta — semantica
    diversa dal `settle.pixels` di PHD2 (RMS max tollerata in settling).
    Per `settle_pixels` lasciamo il default 1.5 (ragionevole per la maggior
    parte dei setup); l'app può ancora forzare un override via job."""
    cfg = Path.home() / ".config/kstarsrc"
    out = {}
    if not cfg.exists():
        return out
    try:
        in_guide = False
        for line in cfg.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s.startswith("[") and s.endswith("]"):
                in_guide = (s == "[Guide]")
                continue
            if not in_guide or "=" not in s:
                continue
            k, _, v = s.partition("=")
            k = k.strip(); v = v.strip()
            if k == "DitherPixels":
                try: out["amount"] = float(v)
                except ValueError: pass
            elif k == "DitherSettle":
                try: out["settle_time"] = float(v)
                except ValueError: pass
    except Exception as e:
        _logger.warning("cannot read Ekos dither settings: %s", e)
    return out


def _read_active_train_name() -> str:
    """Risolve il NOME del train attivo dalla userdb di KStars usando
    CaptureTrainID di kstarsrc. Ritorna stringa vuota se non lo trova.

    Esempio: CaptureTrainID=1 → 'Principale'.
    Serve per chiamare Ekos.Capture.start(train_name) — passare ""
    fa fallire il restart su molte versioni Ekos."""
    train_id = _read_active_train_id()
    if train_id is None:
        return ""
    try:
        conn = sqlite3.connect(f"file:{KSTARS_USERDB}?mode=ro", uri=True, timeout=2.0)
        try:
            cur = conn.cursor()
            cur.execute("SELECT name FROM opticaltrains WHERE id = ?", (train_id,))
            row = cur.fetchone()
            return row[0] if row else ""
        finally:
            conn.close()
    except Exception as e:
        _logger.warning("cannot read train name: %s", e)
        return ""


async def _auto_dither_worker() -> None:
    """Long-running: ascolta dbus-monitor per `captureComplete` di Ekos.
    Ad ogni evento esegue il ciclo pause → dither → start.
    Restart automatico in caso di crash di dbus-monitor."""
    while _auto_dither_state["enabled"]:
        try:
            env = os.environ.copy()
            uid = os.getuid()
            env.setdefault("DBUS_SESSION_BUS_ADDRESS",
                           f"unix:path=/run/user/{uid}/bus")
            proc = await asyncio.create_subprocess_exec(
                "dbus-monitor", "--session",
                "type='signal',interface='org.kde.kstars.Ekos.Capture',member='captureComplete'",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
            )
            _logger.info("auto-dither: dbus-monitor started pid=%s", proc.pid)
            try:
                while _auto_dither_state["enabled"]:
                    line = await proc.stdout.readline()
                    if not line:
                        break
                    if b"captureComplete" in line:
                        _auto_dither_state["frames_seen"] += 1
                        _logger.info("auto-dither: captureComplete intercepted "
                                     "(frame #%d)", _auto_dither_state["frames_seen"])
                        # Spawn cycle as task so we don't block the readline loop
                        asyncio.create_task(_auto_dither_cycle())
            finally:
                try:
                    proc.terminate()
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except Exception:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _logger.warning("auto-dither: monitor loop crashed: %s — retry in 3s", e)
            await asyncio.sleep(3)


async def _auto_dither_cycle() -> None:
    """Esegue un ciclo: pause Ekos → dither PHD2 → WAIT SettleDone → restart Ekos.

    v0.3.5: importante — la chiamata a `phd2.dither(...)` ritorna SUBITO
    con l'ACK JSON-RPC (~40ms). PHD2 fa il vero dither (move + settle) in
    asincrono, emettendo eventi `Settling` (start) e `SettleDone` (end).
    Prima riprendevamo Ekos appena tornata la ACK → Ekos partiva con il
    prossimo scatto MENTRE PHD2 era ancora in settling → trails.
    Adesso aspettiamo che `phd2.live.settling` torni False (con timeout).
    """
    bridge = _auto_dither_bridge
    if bridge is None:
        _logger.warning("auto-dither: bridge non disponibile")
        return
    train = _auto_dither_state["train"]
    try:
        # 1) Pausa Ekos.
        _logger.info("auto-dither: pausing Ekos.Capture (train=%r)", train)
        await _dbus_call(EKOS_DBUS_SERVICE, EKOS_CAPTURE_PATH,
                          "org.kde.kstars.Ekos.Capture.pause", timeout=5.0)
        await asyncio.sleep(0.5)

        # 2) Dither PHD2 (la chiamata ritorna subito con l'ACK)
        amount = _auto_dither_state["amount"]
        settle_pixels = _auto_dither_state["settle_pixels"]
        settle_time = _auto_dither_state["settle_time"]
        settle_timeout = _auto_dither_state["settle_timeout"]
        _logger.info("auto-dither: dither RPC sent (amount=%.1fpx, settle_pixels=%.1fpx, "
                     "settle_time=%.0fs)", amount, settle_pixels, settle_time)
        try:
            await bridge.phd2.dither(
                amount=amount,
                ra_only=False,
                settle_pixels=settle_pixels,
                settle_time=settle_time,
                settle_timeout=settle_timeout,
            )
        except Exception as e:
            _auto_dither_state["last_error"] = f"PHD2 dither ACK failed: {e}"
            _logger.warning("auto-dither: PHD2 dither ACK failed (%s), riprendo Ekos", e)
            # fallthrough to resume Ekos comunque
        else:
            # 2b) Aspettiamo l'evento SettleDone (settling torna False).
            # PHD2 prima setta settling=True (Settling event), poi False (SettleDone).
            # Aspettiamo che diventi True ENTRO 5s (PHD2 prende un attimo a partire)
            # poi che torni False ENTRO settle_timeout (max attesa configurata).
            try:
                # wait for Settling=True
                t0 = time.monotonic()
                while time.monotonic() - t0 < 5.0:
                    if bridge.phd2.live.get("settling") is True:
                        break
                    await asyncio.sleep(0.2)
                # wait for Settling=False (SettleDone)
                t1 = time.monotonic()
                max_wait = float(settle_timeout) + 5.0
                while time.monotonic() - t1 < max_wait:
                    if bridge.phd2.live.get("settling") is not True:
                        break
                    await asyncio.sleep(0.3)
                else:
                    _logger.warning("auto-dither: settle timeout (%.0fs), riprendo comunque", max_wait)
                elapsed = time.monotonic() - t0
                _logger.info("auto-dither: PHD2 settled after %.1fs, resuming Ekos", elapsed)
                _auto_dither_state["last_dither_ts"] = time.time()
                _auto_dither_state["last_error"] = None
            except Exception as e:
                _auto_dither_state["last_error"] = f"settle wait failed: {e}"
                _logger.warning("auto-dither: settle wait failed (%s), riprendo", e)

        # 3) Riavvia Ekos.Capture passando il NOME del train (es. "Principale").
        # v0.3.5: prima passavamo train="" → start() falliva silenziosamente
        # e il job restava in pausa per sempre.
        if not train:
            train = _read_active_train_name()
            _auto_dither_state["train"] = train
            _logger.info("auto-dither: resolved train name → %r", train)
        await _dbus_call(EKOS_DBUS_SERVICE, EKOS_CAPTURE_PATH,
                          "org.kde.kstars.Ekos.Capture.start", train, timeout=5.0)
    except Exception as e:
        _auto_dither_state["last_error"] = f"cycle failed: {e}"
        _logger.exception("auto-dither cycle failed")
        # Tentativo last-resort: prova a riavviare Ekos
        try:
            await _dbus_call(EKOS_DBUS_SERVICE, EKOS_CAPTURE_PATH,
                              "org.kde.kstars.Ekos.Capture.start", train or "", timeout=5.0)
        except Exception:
            pass


async def _start_auto_dither(bridge, train: str) -> None:
    """Avvia il worker di auto-dither (idempotente)."""
    global _auto_dither_task, _auto_dither_bridge
    _auto_dither_bridge = bridge
    _auto_dither_state["enabled"] = True
    _auto_dither_state["train"] = train
    _auto_dither_state["frames_seen"] = 0
    _auto_dither_state["last_error"] = None
    if _auto_dither_task is None or _auto_dither_task.done():
        _auto_dither_task = asyncio.create_task(_auto_dither_worker())
        _logger.info("auto-dither: worker started for train=%r", train)


async def _stop_auto_dither() -> None:
    """Ferma il worker di auto-dither (idempotente)."""
    global _auto_dither_task
    _auto_dither_state["enabled"] = False
    if _auto_dither_task is not None and not _auto_dither_task.done():
        _auto_dither_task.cancel()
        try:
            await _auto_dither_task
        except (asyncio.CancelledError, Exception):
            pass
    _auto_dither_task = None
    _logger.info("auto-dither: worker stopped")


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
async def ekos_run(payload: dict = Body(...), bridge: Bridge = Depends(get_bridge)) -> dict:
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
    # v0.3.4/0.3.5: auto-dither bridge-native.
    # Se almeno un job ha `ditherEachFrame=true`, accendiamo il watcher che
    # via dbus-monitor intercetta `captureComplete` e chiama PHD2.dither().
    any_dither = any(bool(j.get("ditherEachFrame")) for j in jobs)
    # v0.3.5: priorità ai parametri dither salvati dall'utente in Ekos
    # (kstarsrc [Guide] DitherPixels / DitherSettle / DitherThreshold).
    # Solo se l'app passa override espliciti per job li usiamo come override.
    ekos_dither = _read_ekos_dither_settings()
    if ekos_dither.get("amount") is not None:
        _auto_dither_state["amount"] = float(ekos_dither["amount"])
    if ekos_dither.get("settle_time") is not None:
        _auto_dither_state["settle_time"] = float(ekos_dither["settle_time"])
    if ekos_dither.get("settle_pixels") is not None:
        _auto_dither_state["settle_pixels"] = float(ekos_dither["settle_pixels"])
    # Override per-job (raro): app può passare ditherAmount nel CaptureJob
    for j in jobs:
        if j.get("ditherAmount") is not None:
            _auto_dither_state["amount"] = float(j["ditherAmount"])
        if j.get("ditherSettlePixels") is not None:
            _auto_dither_state["settle_pixels"] = float(j["ditherSettlePixels"])
        if j.get("ditherSettleTime") is not None:
            _auto_dither_state["settle_time"] = float(j["ditherSettleTime"])
        break
    # v0.3.5: risolvi nome reale del train per il Capture.start() di restart
    # (se l'app passa train vuoto, leggiamo CaptureTrainID → nome dal userdb)
    effective_train = train or _read_active_train_name() or ""
    _logger.info("ekos_run: dither config — amount=%.1fpx settle_pixels=%.1f "
                 "settle_time=%.0fs train=%r",
                 _auto_dither_state["amount"],
                 _auto_dither_state["settle_pixels"],
                 _auto_dither_state["settle_time"],
                 effective_train)
    if any_dither and started:
        await _start_auto_dither(bridge, effective_train)
    else:
        await _stop_auto_dither()
    return {
        "ok": True,
        "esq_path": str(esq_path),
        "loaded": rc2 == 0,
        "load_response": out2,
        "started": started,
        "start_response": start_msg,
        "jobs_count": len(jobs),
        "auto_dither_enabled": any_dither,
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
    # v0.3.4: ferma anche il watcher auto-dither
    await _stop_auto_dither()
    rc, out = await _dbus_call(EKOS_DBUS_SERVICE, EKOS_CAPTURE_PATH,
                                "org.kde.kstars.Ekos.Capture.abort", "")
    return {"ok": rc == 0, "raw": out}


@router.post("/auto_dither_arm")
async def auto_dither_arm(
    payload: dict = Body(default={}),
    bridge: Bridge = Depends(get_bridge),
) -> dict:
    """v0.3.5: arming MANUALE del watcher auto-dither.

    Utile dopo un restart del bridge mentre una sequenza Ekos è già in corso
    (la sequenza non viene interrotta dal restart, ma il watcher sì perché
    il task asincrono viene perso). Senza questo endpoint l'utente dovrebbe
    abort+restart della sequenza dall'app per ri-armare.

    Body:
      enabled: bool — true accende il watcher, false lo spegne
      train:   str  — nome del train (es. 'Principale'); se omesso, viene
                       letto dal userdb tramite CaptureTrainID
    """
    enabled = bool(payload.get("enabled", True))
    if not enabled:
        await _stop_auto_dither()
        return {"ok": True, "enabled": False}

    train = payload.get("train") or _read_active_train_name() or ""
    # Re-read dither settings da Ekos (in case the user has changed them
    # tra restart e arm)
    ekos_dither = _read_ekos_dither_settings()
    if ekos_dither.get("amount") is not None:
        _auto_dither_state["amount"] = float(ekos_dither["amount"])
    if ekos_dither.get("settle_time") is not None:
        _auto_dither_state["settle_time"] = float(ekos_dither["settle_time"])
    await _start_auto_dither(bridge, train)
    return {
        "ok": True, "enabled": True, "train": train,
        "amount": _auto_dither_state["amount"],
        "settle_time": _auto_dither_state["settle_time"],
        "settle_pixels": _auto_dither_state["settle_pixels"],
    }


@router.get("/auto_dither_status")
async def auto_dither_status() -> dict:
    """v0.3.4: stato del watcher auto-dither bridge-native.

    Espone:
      - enabled: se il watcher è attivo
      - train: optical train che sta seguendo
      - frames_seen: quanti captureComplete intercettati dalla partenza
      - last_dither_ts: timestamp Unix dell'ultimo dither riuscito
      - last_error: ultimo errore (None se tutto ok)
      - amount / settle_*: parametri correnti del dither
    """
    return dict(_auto_dither_state)


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
