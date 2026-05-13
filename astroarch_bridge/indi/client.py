"""Client TCP asincrono per INDI server.

DoD:
- Connessione asyncio a INDI (default 127.0.0.1:7624)
- Reconnect con backoff esponenziale, mai busy-loop
- Feed parser su byte stream
- Coda comandi (newXxxVector) serializzata per device
- Stato connessione esposto e propagato
- enableBLOB per ricevere immagini live se richiesto

Errori prevenuti:
- E1: server giù -> reconnect (no crash, no flood log)
- E12: comandi concorrenti -> queue per device + lock writer
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Callable

from .protocol import (
    IndiEvent,
    IndiParser,
    build_enable_blob,
    build_get_properties,
    build_new_number,
    build_new_switch,
    build_new_text,
)

log = logging.getLogger(__name__)


class ConnectionState:
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECT = "reconnecting"


EventListener = Callable[[IndiEvent], None]
StateListener = Callable[[str], None]


class IndiClient:
    """Client INDI asincrono con riconnessione automatica."""

    def __init__(
        self,
        host: str,
        port: int,
        on_event: EventListener,
        on_connection_state: StateListener | None = None,
        reconnect_min: float = 1.0,
        reconnect_max: float = 30.0,
        enable_blob_devices: bool | str | list[str] = "auto",
    ):
        self._host = host
        self._port = port
        self._on_event = on_event
        self._on_state = on_connection_state or (lambda _s: None)
        self._reconnect_min = reconnect_min
        self._reconnect_max = reconnect_max
        self._enable_blob_devices = enable_blob_devices

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._writer_lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._state = ConnectionState.DISCONNECTED
        self._parser = IndiParser(self._handle_event)
        self._device_locks: dict[str, asyncio.Lock] = {}
        self._known_devices: set[str] = set()

    # --- Properties ---------------------------------------------------------

    @property
    def state(self) -> str:
        return self._state

    @property
    def known_devices(self) -> list[str]:
        return sorted(self._known_devices)

    # --- Lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="indi-client")

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
        self._parser.close()
        self._set_state(ConnectionState.DISCONNECTED)

    # --- Main loop ----------------------------------------------------------

    async def _run(self) -> None:
        backoff = self._reconnect_min
        while not self._stop_event.is_set():
            try:
                self._set_state(ConnectionState.CONNECTING)
                self._reader, self._writer = await asyncio.open_connection(self._host, self._port)
                self._set_state(ConnectionState.CONNECTED)
                backoff = self._reconnect_min
                # Subito chiediamo le proprietà di tutti i driver attivi
                await self._send_raw(build_get_properties())
                await self._read_loop()
            except (OSError, asyncio.IncompleteReadError) as e:
                log.info("INDI disconnected: %s", e)
            except Exception:
                log.exception("INDI client unexpected error")
            finally:
                with contextlib.suppress(Exception):
                    if self._writer is not None:
                        self._writer.close()
                        await self._writer.wait_closed()
                self._reader = None
                self._writer = None
                # Reset parser e devices conosciuti
                self._parser = IndiParser(self._handle_event)
                self._known_devices.clear()
            if self._stop_event.is_set():
                break
            self._set_state(ConnectionState.RECONNECT)
            await asyncio.sleep(backoff)
            backoff = min(self._reconnect_max, backoff * 2)

    async def _read_loop(self) -> None:
        assert self._reader is not None
        while not self._stop_event.is_set():
            chunk = await self._reader.read(65536)
            if not chunk:
                raise OSError("EOF from INDI server")
            self._parser.feed(chunk)

    # --- Event piping -------------------------------------------------------

    def _handle_event(self, ev: IndiEvent) -> None:
        if ev.kind == "def" and ev.device:
            if ev.device not in self._known_devices:
                self._known_devices.add(ev.device)
            # Auto-enable BLOB su camere quando vediamo CCD1 (BLOB property)
            if ev.property and ev.property.type.value == "BLOB":
                if self._should_enable_blob(ev.device):
                    log.info("INDI enableBLOB Also for device=%s prop=%s",
                             ev.device, ev.name)
                    asyncio.create_task(self._send_raw(build_enable_blob(ev.device, "Also")))
        try:
            self._on_event(ev)
        except Exception:
            log.exception("on_event listener crashed (event ignored)")

    def _should_enable_blob(self, device: str) -> bool:
        if isinstance(self._enable_blob_devices, bool):
            return self._enable_blob_devices
        # Heuristic: tutte le camere (cose con "CCD" nel nome o ToupTek/ZWO/QHY)
        if self._enable_blob_devices == "auto":
            dl = device.lower()
            return any(k in dl for k in ("ccd", "toup", "zwo", "qhy", "asi", "atik", "atr"))
        return device in self._enable_blob_devices

    # --- State helper -------------------------------------------------------

    def _set_state(self, s: str) -> None:
        if self._state == s:
            return
        self._state = s
        log.info("INDI connection state: %s", s)
        try:
            self._on_state(s)
        except Exception:
            log.exception("on_state listener crashed")

    # --- Commands -----------------------------------------------------------

    async def _device_lock(self, device: str) -> asyncio.Lock:
        lock = self._device_locks.get(device)
        if lock is None:
            lock = asyncio.Lock()
            self._device_locks[device] = lock
        return lock

    async def send_switch(self, device: str, name: str, values: dict[str, bool]) -> None:
        lock = await self._device_lock(device)
        async with lock:
            await self._send_raw(build_new_switch(device, name, values))

    async def send_number(self, device: str, name: str, values: dict[str, float]) -> None:
        lock = await self._device_lock(device)
        async with lock:
            await self._send_raw(build_new_number(device, name, values))

    async def send_text(self, device: str, name: str, values: dict[str, str]) -> None:
        lock = await self._device_lock(device)
        async with lock:
            await self._send_raw(build_new_text(device, name, values))

    async def request_properties(self, device: str = "", name: str = "") -> None:
        await self._send_raw(build_get_properties(device=device, name=name))

    async def enable_blob(self, device: str, mode: str = "Also") -> None:
        await self._send_raw(build_enable_blob(device, mode))

    async def _send_raw(self, data: bytes) -> None:
        if self._writer is None:
            raise ConnectionError("INDI not connected")
        async with self._writer_lock:
            self._writer.write(data)
            try:
                await asyncio.wait_for(self._writer.drain(), timeout=5.0)
            except asyncio.TimeoutError:
                log.warning("INDI write drain timeout")


class _DummyClock:
    """Helper per testing - non usato in prod."""
    def __init__(self): self.t = time.monotonic()
