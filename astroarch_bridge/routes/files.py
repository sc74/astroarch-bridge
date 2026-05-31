"""Route /api/files: browse FITS catture + download thumbnail/full + delete."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import FileResponse, Response

from ..auth import require_token
from ..deps import Bridge, get_bridge
from ..images.processor import process_fits_async
from ..images.watcher import list_recent_fits

router = APIRouter(prefix="/api/files", tags=["files"], dependencies=[Depends(require_token)])


@router.get("/recent")
async def recent(
    limit: int = Query(50, ge=1, le=500),
    bridge: Bridge = Depends(get_bridge),
) -> dict:
    base = Path(bridge.images_dir)
    items = await asyncio.to_thread(list_recent_fits, base, limit)
    return {
        "base": str(base),
        "count": len(items),
        "items": [
            {
                "name": p.name,
                "path": str(p.relative_to(base)) if base in p.parents or p == base else str(p),
                "size": _safe_size(p),
                "mtime": _safe_mtime(p),
            }
            for p in items
        ],
    }


@router.get("/preview")
async def preview(
    path: str,
    thumbnail: bool = False,
    bridge: Bridge = Depends(get_bridge),
) -> Response:
    """Restituisce JPEG preview (o thumbnail) di un FITS."""
    base = Path(bridge.images_dir).resolve()
    full = (base / path).resolve()
    if not _is_inside(full, base):
        raise HTTPException(status_code=400, detail="path traversal blocked")
    if not full.exists():
        raise HTTPException(status_code=404, detail="file not found")
    try:
        result = await process_fits_async(full)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"process failed: {e}")
    data = result.thumbnail if thumbnail else result.jpeg
    return Response(content=data, media_type="image/jpeg")


@router.get("/download")
async def download(
    path: str,
    bridge: Bridge = Depends(get_bridge),
) -> FileResponse:
    base = Path(bridge.images_dir).resolve()
    full = (base / path).resolve()
    if not _is_inside(full, base):
        raise HTTPException(status_code=400, detail="path traversal blocked")
    if not full.exists():
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(str(full), media_type="application/octet-stream", filename=full.name)


@router.delete("/file")
async def delete_file(
    path: str,
    bridge: Bridge = Depends(get_bridge),
) -> dict:
    """Cancella un singolo file FITS dal RPi.
    Path-traversal protected: il file deve essere dentro images_dir.
    Accetta solo estensioni .fit/.fits/.fz per sicurezza.
    """
    base = Path(bridge.images_dir).resolve()
    full = (base / path).resolve()
    if not _is_inside(full, base):
        raise HTTPException(status_code=400, detail="path traversal blocked")
    if not full.exists():
        raise HTTPException(status_code=404, detail="file not found")
    if full.suffix.lower() not in {".fit", ".fits", ".fz"}:
        raise HTTPException(status_code=400,
                            detail="solo file .fit/.fits/.fz cancellabili")
    try:
        full.unlink()
        return {"ok": True, "path": str(full.relative_to(base))}
    except OSError as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/delete_many")
async def delete_many(
    payload: dict = Body(..., example={"paths": ["dir/a.fits", "dir/b.fits"]}),
    bridge: Bridge = Depends(get_bridge),
) -> dict:
    """Cancella più file in batch."""
    paths = payload.get("paths") or []
    if not isinstance(paths, list):
        raise HTTPException(status_code=400, detail="paths deve essere lista")
    base = Path(bridge.images_dir).resolve()
    deleted = []
    errors = []
    total_bytes = 0
    for p in paths:
        full = (base / p).resolve()
        if not _is_inside(full, base):
            errors.append({"path": str(p), "error": "path traversal blocked"})
            continue
        if not full.exists():
            errors.append({"path": str(p), "error": "not found"})
            continue
        if full.suffix.lower() not in {".fit", ".fits", ".fz"}:
            errors.append({"path": str(p), "error": "wrong extension"})
            continue
        try:
            sz = full.stat().st_size
            full.unlink()
            deleted.append(str(p))
            total_bytes += sz
        except OSError as e:
            errors.append({"path": str(p), "error": str(e)})
    return {
        "ok": True,
        "deleted_count": len(deleted),
        "deleted": deleted,
        "errors": errors,
        "freed_bytes": total_bytes,
    }


@router.get("/disk_usage")
async def disk_usage(bridge: Bridge = Depends(get_bridge)) -> dict:
    """Spazio disco della partizione images_dir."""
    base = Path(bridge.images_dir)
    base.mkdir(parents=True, exist_ok=True)
    try:
        st = os.statvfs(str(base))
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        used = total - free
    except (OSError, AttributeError):
        return {"error": "unavailable"}
    # Quanto occupa la cartella images
    images_used = 0
    files_count = 0
    try:
        for p in base.rglob("*"):
            if p.is_file():
                images_used += p.stat().st_size
                files_count += 1
    except OSError:
        pass
    return {
        "total_bytes": total, "free_bytes": free, "used_bytes": used,
        "images_dir": str(base),
        "images_dir_bytes": images_used,
        "images_files_count": files_count,
    }


# ============================================================================
# v0.3.14: navigazione directory + shortcut + listing sequenze (.esq)
# ============================================================================
# Segnalato da Tucniak: poter navigare le cartelle sul Pi e puntare a una
# cartella diversa da ~/Pictures/Ekos. SICUREZZA: tutta la navigazione è
# confinata DENTRO la home dell'utente (no path traversal verso / o /etc).

def _user_root() -> Path:
    return Path.home().resolve()


@router.get("/roots")
async def roots(bridge: Bridge = Depends(get_bridge)) -> dict:
    """Shortcut comuni per il file browser dell'app (cartelle che esistono)."""
    home = _user_root()
    candidates = [
        ("Ekos / Pictures", Path(bridge.images_dir)),
        ("Desktop", home / "Desktop"),
        ("Pictures", home / "Pictures"),
        ("Home", home),
        ("Documenti", home / "Documents"),
    ]
    out = []
    seen = set()
    for label, p in candidates:
        try:
            rp = p.resolve()
        except OSError:
            continue
        if rp.exists() and rp.is_dir() and _is_inside(rp, home) and str(rp) not in seen:
            seen.add(str(rp))
            out.append({"label": label, "path": _rel_to_home(rp)})
    return {"home": str(home), "roots": out}


@router.get("/browse")
async def browse(
    path: str = Query("", description="path relativo alla home utente"),
    only_dirs: bool = False,
    exts: str = Query("", description="estensioni filtro, es. 'esq,fits' (vuoto=tutte)"),
) -> dict:
    """Naviga una directory DENTRO la home utente. Ritorna sottocartelle +
    file (con size/mtime). `path` è relativo alla home. Protetto da
    path-traversal (risolve e verifica che resti dentro la home)."""
    home = _user_root()
    target = (home / path).resolve() if path else home
    if not _is_inside(target, home):
        raise HTTPException(status_code=400, detail="path traversal blocked")
    if not target.exists() or not target.is_dir():
        raise HTTPException(status_code=404, detail="directory not found")
    ext_set = {e.strip().lower().lstrip(".") for e in exts.split(",") if e.strip()}
    dirs, files = [], []
    try:
        for entry in sorted(target.iterdir(), key=lambda x: x.name.lower()):
            if entry.name.startswith("."):
                continue  # nascondi dotfile
            if entry.is_dir():
                dirs.append({"name": entry.name, "path": _rel_to_home(entry)})
            elif not only_dirs:
                if ext_set and entry.suffix.lower().lstrip(".") not in ext_set:
                    continue
                files.append({
                    "name": entry.name, "path": _rel_to_home(entry),
                    "size": _safe_size(entry), "mtime": _safe_mtime(entry),
                })
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"read error: {e}")
    parent = None
    if target != home:
        parent = _rel_to_home(target.parent)
    return {
        "path": _rel_to_home(target),
        "abs": str(target),
        "parent": parent,
        "dirs": dirs,
        "files": files,
    }


def _rel_to_home(p: Path) -> str:
    """Path relativo alla home ('' = home stessa)."""
    home = _user_root()
    try:
        rel = p.resolve().relative_to(home)
        return str(rel) if str(rel) != "." else ""
    except ValueError:
        return ""


def _is_inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _safe_size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def _safe_mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0
