"""Route /api/setup — Profili Ekos / lista driver attivi.

Su AstroArch i profili Ekos sono in ~/.config/Ekos/ — leggibile
direttamente dal bridge (lo stesso utente).
"""
from __future__ import annotations

import os
from pathlib import Path
from xml.etree import ElementTree as ET

from fastapi import APIRouter, Depends

from ..auth import require_token
from ..deps import Bridge, get_bridge

router = APIRouter(prefix="/api/setup", tags=["setup"], dependencies=[Depends(require_token)])

EKOS_DB_PATHS = [
    Path.home() / ".local/share/kstars/userdb.sqlite",
    Path.home() / ".config/Ekos/userdb.sqlite",
]


@router.get("/profiles")
async def profiles() -> dict:
    """Elenca i profili Ekos noti (parse minimale)."""
    profiles_list = []
    # Tentativo: parse SQLite userdb.sqlite (tabella profile, driver)
    for p in EKOS_DB_PATHS:
        if p.exists():
            try:
                import sqlite3
                conn = sqlite3.connect(str(p))
                cur = conn.cursor()
                cur.execute("SELECT id, name FROM profile")
                rows = cur.fetchall()
                profiles_list = [{"id": r[0], "name": r[1]} for r in rows]
                conn.close()
                break
            except Exception:
                continue
    return {"profiles": profiles_list, "db_path": str(EKOS_DB_PATHS[0])}


@router.get("/active_drivers")
async def active_drivers(bridge: Bridge = Depends(get_bridge)) -> dict:
    """Lista driver INDI attualmente connessi al bridge."""
    devices = await bridge.state.list_devices()
    out = []
    for d in devices:
        conn = await bridge.state.get_property(d, "CONNECTION")
        is_connected = False
        if conn:
            for e in conn.get("elements", []):
                if e["name"] == "CONNECT" and e.get("value") == True:
                    is_connected = True
                    break
        out.append({"name": d, "connected": is_connected})
    return {"drivers": out}
