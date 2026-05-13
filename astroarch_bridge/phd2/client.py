"""Client PHD2 (Server Event Protocol + JSON-RPC line-delimited).

PHD2 espone su TCP (default 4400) un protocollo:
- Per ogni evento server: una riga JSON con campo "Event"
- Per JSON-RPC requests: invii {"method", "params", "id"} \\n e ricevi {"jsonrpc","result"|"error","id"} \\n

DoD:
- Connessione TCP asincrona
- Reconnect con backoff
- Parsing line-by-line tollerante
- Stato live (RMS, peaks, SNR, calibrated, app_state)
- Metodi RPC: guide, stop_capture, dither, loop, get_app_state, get_pixel_scale...
- Listener eventi per propagare al state manager

Errori prevenuti:
- E4: PHD2 disconnesso -> reconnect, metodi RPC sollevano se non connesso
- Riga JSON malformata -> log warning, skip riga
- Coda RPC pending -> timeout per ogni call
"""
from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import time
from typing import Any, Callable

log = logging.getLogger(__name__)


class Phd2State:
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECT = "reconnecting"


class Phd2AppState:
    """Stati applicazione PHD2 (campo AppState negli eventi)."""
    STOPPED = "Stopped"
    SELECTED = "Selected"
    CALIBRATING = "Calibrating"
    GUIDING = "Guiding"
    LOSTLOCK = "LostLock"
    PAUSED = "Paused"
    LOOPING = "Looping"


EventListener = Callable[[dict], None]
StateListener = Callable[[str], None]


class Phd2Client:
    def __init__(
        self,
        host: str,
        port: int,
        on_event: EventListener,
        on_connection_state: StateListener | None = None,
        reconnect_min: float = 2.0,
        reconnect_max: float = 60.0,
    ):
        self._host = host
        self._port = port
        self._on_event = on_event
        self._on_state = on_connection_state or (lambda _s: None)
        self._reconnect_min = reconnect_min
        self._reconnect_max = reconnect_max

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._state = Phd2State.DISCONNECTED
        self._writer_lock = asyncio.Lock()

        self._next_id = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}

        # Stato live aggregato
        self.live: dict[str, Any] = {
            "app_state": Phd2AppState.STOPPED,
            "calibrated": False,
            "rms_total": None,
            "rms_ra": None,
            "rms_dec": None,
            "peak_ra": None,
            "peak_dec": None,
            "snr": None,
            "star_lost": False,
            "settling": False,
            "last_event_ts": 0.0,
            "version": None,
            "pixel_scale": None,
            "exposure": None,
        }

    # --- Lifecycle ----------------------------------------------------------

    @property
    def state(self) -> str:
        return self._state

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="phd2-client")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._writer is not None:
            with contextlib.suppress(Exception):
                self._writer.close()
                await self._writer.wait_closed()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self._fail_all_pending(ConnectionError("phd2 stopped"))
        self._set_state(Phd2State.DISCONNECTED)

    # --- Loop ---------------------------------------------------------------

    async def _run(self) -> None:
        backoff = self._reconnect_min
        while not self._stop_event.is_set():
            try:
                self._set_state(Phd2State.CONNECTING)
                self._reader, self._writer = await asyncio.open_connection(self._host, self._port)
                self._set_state(Phd2State.CONNECTED)
                backoff = self._reconnect_min
                # PHD2 manda un Version evento iniziale; richiediamo anche app_state e pixel_scale
                asyncio.create_task(self._post_connect_probe())
                await self._read_loop()
            except (OSError, asyncio.IncompleteReadError) as e:
                log.info("PHD2 disconnected: %s", e)
            except Exception:
                log.exception("PHD2 client unexpected error")
            finally:
                with contextlib.suppress(Exception):
                    if self._writer is not None:
                        self._writer.close()
                        await self._writer.wait_closed()
                self._reader = None
                self._writer = None
                self._fail_all_pending(ConnectionError("phd2 disconnected"))
            if self._stop_event.is_set():
                break
            self._set_state(Phd2State.RECONNECT)
            await asyncio.sleep(backoff)
            backoff = min(self._reconnect_max, backoff * 2)

    async def _read_loop(self) -> None:
        assert self._reader is not None
        while not self._stop_event.is_set():
            line = await self._reader.readline()
            if not line:
                raise OSError("EOF from PHD2")
            line_s = line.decode("utf-8", errors="replace").strip()
            if not line_s:
                continue
            try:
                msg = json.loads(line_s)
            except json.JSONDecodeError:
                log.warning("PHD2 bad json line: %r", line_s[:200])
                continue
            self._dispatch(msg)

    def _dispatch(self, msg: dict) -> None:
        # JSON-RPC response (ha jsonrpc/id)
        if "jsonrpc" in msg and "id" in msg:
            fut = self._pending.pop(msg["id"], None)
            if fut and not fut.done():
                if "error" in msg:
                    fut.set_exception(Phd2RpcError(msg["error"]))
                else:
                    fut.set_result(msg.get("result"))
            return

        # Evento server: ha "Event"
        ev_name = msg.get("Event")
        if not ev_name:
            return
        self._update_live_from_event(ev_name, msg)
        try:
            self._on_event(msg)
        except Exception:
            log.exception("phd2 on_event listener crashed")

    def _update_live_from_event(self, name: str, msg: dict) -> None:
        self.live["last_event_ts"] = time.time()
        if name == "Version":
            self.live["version"] = msg.get("PHDVersion")
        elif name == "AppState":
            self.live["app_state"] = msg.get("State", Phd2AppState.STOPPED)
        elif name == "GuideStep":
            self.live["app_state"] = Phd2AppState.GUIDING
            self.live["snr"] = msg.get("SNR")
            self.live["star_lost"] = False
            # IMPORTANTE: PHD2 manda RADistanceRaw / DECDistanceRaw in
            # arcsec signed AD OGNI frame. Questi sono i campioni che il
            # GRAFICO di PHD2 plotta sull'asse Y (linea blu = RA, rossa
            # = DEC). Senza salvarli qui, lo storico nell'app non avrebbe
            # mai dati per frame e il grafico di inseguimento resterebbe
            # vuoto (era il bug pre-v0.2.26).
            self.live["ra_raw"] = msg.get("RADistanceRaw")
            self.live["dec_raw"] = msg.get("DECDistanceRaw")
            # Anche durata pulse e star mass per diagnostica avanzata
            self.live["ra_duration"] = msg.get("RADuration")
            self.live["dec_duration"] = msg.get("DECDuration")
            self.live["star_mass"] = msg.get("StarMass")
            self.live["avg_dist"] = msg.get("AvgDist")
            self.live["frame"] = msg.get("Frame")
        elif name == "GuidingStats":
            self.live["rms_total"] = msg.get("RMS")
            self.live["rms_ra"] = msg.get("RaRMS")
            self.live["rms_dec"] = msg.get("DecRMS")
            self.live["peak_ra"] = msg.get("PeakRaErr")
            self.live["peak_dec"] = msg.get("PeakDecErr")
        elif name == "StarLost":
            self.live["star_lost"] = True
            self.live["snr"] = msg.get("SNR")
        elif name == "CalibrationComplete":
            self.live["calibrated"] = True
        elif name == "StartCalibration":
            self.live["calibrated"] = False
            self.live["app_state"] = Phd2AppState.CALIBRATING
        elif name == "Settling":
            self.live["settling"] = True
        elif name == "SettleDone":
            self.live["settling"] = False
        elif name == "LoopingExposures":
            self.live["app_state"] = Phd2AppState.LOOPING
            self.live["exposure"] = msg.get("Frame")
        elif name == "LoopingExposuresStopped":
            self.live["app_state"] = Phd2AppState.STOPPED
        elif name == "GuidingStopped":
            self.live["app_state"] = Phd2AppState.STOPPED
        elif name == "Paused":
            self.live["app_state"] = Phd2AppState.PAUSED
        elif name == "Resumed":
            self.live["app_state"] = Phd2AppState.GUIDING

    async def _post_connect_probe(self) -> None:
        try:
            ps = await self.call("get_pixel_scale")
            self.live["pixel_scale"] = ps
        except Exception:
            pass
        try:
            st = await self.call("get_app_state")
            self.live["app_state"] = st
        except Exception:
            pass

    # --- State / pending ---------------------------------------------------

    def _set_state(self, s: str) -> None:
        if self._state == s:
            return
        self._state = s
        log.info("PHD2 connection state: %s", s)
        try:
            self._on_state(s)
        except Exception:
            log.exception("phd2 on_state listener crashed")

    def _fail_all_pending(self, exc: BaseException) -> None:
        for fid, fut in list(self._pending.items()):
            if not fut.done():
                fut.set_exception(exc)
            self._pending.pop(fid, None)

    # --- RPC ----------------------------------------------------------------

    async def call(self, method: str, params: Any | None = None, timeout: float = 10.0) -> Any:
        if self._writer is None:
            raise ConnectionError("PHD2 not connected")
        rid = next(self._next_id)
        req: dict[str, Any] = {"method": method, "id": rid}
        if params is not None:
            req["params"] = params
        line = (json.dumps(req) + "\n").encode("utf-8")
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[rid] = fut
        async with self._writer_lock:
            self._writer.write(line)
            await self._writer.drain()
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            raise

    # --- High-level helpers -------------------------------------------------

    async def start_guiding(self, settle_pixels: float = 1.5,
                            settle_time: float = 10.0,
                            settle_timeout: float = 60.0) -> Any:
        return await self.call("guide", {
            "settle": {"pixels": settle_pixels, "time": settle_time, "timeout": settle_timeout},
            "recalibrate": False,
        }, timeout=15.0)

    async def stop_capture(self) -> Any:
        return await self.call("stop_capture")

    async def dither(self, amount: float = 3.0, ra_only: bool = False,
                     settle_pixels: float = 1.5, settle_time: float = 10.0,
                     settle_timeout: float = 60.0) -> Any:
        return await self.call("dither", {
            "amount": amount,
            "raOnly": ra_only,
            "settle": {"pixels": settle_pixels, "time": settle_time, "timeout": settle_timeout},
        }, timeout=15.0)

    async def loop(self) -> Any:
        return await self.call("loop")

    async def clear_calibration(self, which: str = "Both") -> Any:
        return await self.call("clear_calibration", which)

    async def get_app_state(self) -> str:
        s = await self.call("get_app_state")
        return str(s)

    async def get_connected(self) -> bool:
        return bool(await self.call("get_connected"))

    async def set_paused(self, paused: bool, full: bool = False) -> Any:
        return await self.call("set_paused", [paused, "full" if full else ""])


class Phd2RpcError(Exception):
    def __init__(self, error: dict):
        super().__init__(error.get("message", "phd2 rpc error"))
        self.error = error
