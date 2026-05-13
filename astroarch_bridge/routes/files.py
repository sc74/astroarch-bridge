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


def _is_inside(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
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
