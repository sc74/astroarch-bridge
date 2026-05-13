"""Watcher su cartella Ekos: nuovi FITS -> processor -> callback.

Usa watchdog per cross-platform notification.

DoD:
- Watch ricorsivo su images_dir
- Filtra solo .fit/.fits/.fz
- Stabilizzazione: aspetta size invariata per stabilize_ms prima di leggere
- Coda asyncio per debounce + serializzazione
- Callback async con ProcessResult
- Resiliente a errori singoli (un FITS rotto non blocca gli altri)

Errori prevenuti:
- E5: FITS in scrittura -> stabilize check
- E9: errori I/O sporadici -> try/except per file, log warning, prosegue
- spam di eventi -> set di path "in elaborazione" per dedup
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Awaitable, Callable

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .processor import ProcessResult, process_fits_async

log = logging.getLogger(__name__)

FITS_EXTENSIONS = {".fit", ".fits", ".fz"}

ResultCallback = Callable[[Path, ProcessResult], Awaitable[None]]


class _Handler(FileSystemEventHandler):
    """Bridge da watchdog (sync thread) a asyncio queue."""

    def __init__(self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue):
        self._loop = loop
        self._queue = queue

    def _enqueue(self, path: str) -> None:
        p = Path(path)
        if p.suffix.lower() not in FITS_EXTENSIONS:
            return
        try:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, p)
        except RuntimeError:
            # event loop closed
            pass

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._enqueue(event.src_path)

    def on_modified(self, event: FileSystemEvent) -> None:
        # Alcuni programmi creano file vuoto e poi scrivono -> usiamo modified come trigger
        if not event.is_directory:
            self._enqueue(event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._enqueue(event.dest_path)


class FitsWatcher:
    def __init__(
        self,
        images_dir: Path,
        on_result: ResultCallback,
        max_dim: int = 1600,
        thumbnail_dim: int = 512,
        jpeg_quality: int = 85,
        stabilize_ms: int = 500,
    ):
        self._dir = images_dir
        self._on_result = on_result
        self._max_dim = max_dim
        self._thumbnail_dim = thumbnail_dim
        self._jpeg_quality = jpeg_quality
        self._stabilize_s = stabilize_ms / 1000.0

        self._observer: Observer | None = None
        self._queue: asyncio.Queue[Path] = asyncio.Queue(maxsize=256)
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._in_progress: set[Path] = set()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._dir.mkdir(parents=True, exist_ok=True)
        loop = asyncio.get_running_loop()
        handler = _Handler(loop, self._queue)
        self._observer = Observer()
        self._observer.schedule(handler, str(self._dir), recursive=True)
        self._observer.start()
        self._stop_event.clear()
        self._task = asyncio.create_task(self._consumer(), name="fits-watcher")
        log.info("FITS watcher started on %s", self._dir)

    async def stop(self) -> None:
        self._stop_event.set()
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=2.0)
            self._observer = None
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _consumer(self) -> None:
        while not self._stop_event.is_set():
            try:
                path = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            if path in self._in_progress:
                continue
            self._in_progress.add(path)
            asyncio.create_task(self._process_one(path))

    async def _process_one(self, path: Path) -> None:
        try:
            ok = await self._wait_stable(path)
            if not ok:
                return
            try:
                result = await process_fits_async(
                    path,
                    max_dim=self._max_dim,
                    thumbnail_dim=self._thumbnail_dim,
                    jpeg_quality=self._jpeg_quality,
                )
            except Exception as e:
                log.warning("FITS process failed for %s: %s", path.name, e)
                return
            try:
                await self._on_result(path, result)
            except Exception:
                log.exception("on_result callback failed")
        finally:
            self._in_progress.discard(path)

    async def _wait_stable(self, path: Path, max_wait: float = 30.0) -> bool:
        """Aspetta che il file abbia size invariata per stabilize_s. Ritorna False se sparisce."""
        deadline = time.monotonic() + max_wait
        last_size = -1
        last_change = time.monotonic()
        while time.monotonic() < deadline:
            try:
                size = path.stat().st_size
            except FileNotFoundError:
                return False
            except OSError as e:
                log.debug("stat failed for %s: %s", path, e)
                await asyncio.sleep(0.2)
                continue
            now = time.monotonic()
            if size != last_size:
                last_size = size
                last_change = now
            elif size > 0 and (now - last_change) >= self._stabilize_s:
                return True
            await asyncio.sleep(min(0.2, self._stabilize_s / 2))
        log.warning("FITS %s did not stabilize within %.1fs", path.name, max_wait)
        return False


def list_recent_fits(images_dir: Path, limit: int = 50) -> list[Path]:
    """Elenco ricorsivo dei FITS più recenti (per /api/files)."""
    if not images_dir.exists():
        return []
    files: list[tuple[float, Path]] = []
    for ext in FITS_EXTENSIONS:
        for p in images_dir.rglob(f"*{ext}"):
            try:
                files.append((p.stat().st_mtime, p))
            except OSError:
                continue
    files.sort(key=lambda t: t[0], reverse=True)
    return [p for _, p in files[:limit]]
