"""Bearer token authentication per REST e WebSocket.

DoD:
- Dependency FastAPI per REST -> 401 se invalido
- Helper per WS che ritorna bool e gestisce close 1008
- Compare costante-tempo (no timing leaks)
- Log ratelimited su tentativi falliti

Errori prevenuti:
- E7: token sbagliato non trapelato via timing
- Header malformato/case-insensitive non causa crash
"""
from __future__ import annotations

import hmac
import logging
import time
from typing import Optional

from fastapi import Header, HTTPException, status
from starlette.websockets import WebSocket

from .config import get_settings

log = logging.getLogger(__name__)

# Ratelimiting molto semplice per log auth fail (no DDoS, solo sanity).
_LAST_FAIL_LOG_TS: float = 0.0
_FAIL_LOG_INTERVAL = 5.0  # secondi


def _constant_time_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _log_fail(reason: str, peer: str) -> None:
    global _LAST_FAIL_LOG_TS
    now = time.monotonic()
    if now - _LAST_FAIL_LOG_TS >= _FAIL_LOG_INTERVAL:
        log.warning("auth fail: %s (peer=%s)", reason, peer)
        _LAST_FAIL_LOG_TS = now


def _extract_bearer(header_value: Optional[str]) -> Optional[str]:
    if not header_value:
        return None
    parts = header_value.strip().split(None, 1)
    if len(parts) != 2:
        return None
    scheme, token = parts
    if scheme.lower() != "bearer":
        return None
    return token.strip() or None


def require_token(authorization: Optional[str] = Header(default=None)) -> None:
    """FastAPI dependency: verifica Bearer token o solleva 401."""
    settings = get_settings()
    expected = settings.resolve_token()
    received = _extract_bearer(authorization)
    if not received:
        _log_fail("missing/malformed Authorization header", peer="?")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not _constant_time_eq(received, expected):
        _log_fail("bad token", peer="?")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def authenticate_ws(ws: WebSocket) -> bool:
    """Per WebSocket: token preso da query `?token=` o header Authorization.
    Ritorna True se ok; altrimenti chiude con 1008 e ritorna False.
    """
    settings = get_settings()
    expected = settings.resolve_token()
    received = ws.query_params.get("token")
    if not received:
        received = _extract_bearer(ws.headers.get("authorization"))
    peer = ws.client.host if ws.client else "?"
    if not received or not _constant_time_eq(received, expected):
        _log_fail("bad ws token", peer=peer)
        await ws.close(code=1008, reason="unauthorized")
        return False
    return True
