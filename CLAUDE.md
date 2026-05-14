# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`astroarch-bridge` is a FastAPI + WebSocket Python daemon that runs on a Raspberry Pi 5 (AstroArch) and exposes KStars/Ekos, INDI server (port 7624) and PHD2 (port 4400) to an Android app over Tailscale. It is the backend half of [astroarch-interface-app](https://github.com/Johannes1979I/astroarch-interface-app); the two repos must stay version-aligned.

Hard architectural rule: **the bridge is a non-invasive secondary client**. It must NOT change the user's Ekos configuration. Specifically:

- It connects to the INDI server in parallel with Ekos (`enableBLOB Also`) — it never sends `enableBLOB Only` and never reconnects the user's drivers.
- It does NOT alter `UPLOAD_MODE`, `CCD_EXPOSURE_VALUE`, save folder, placeholder format, target coordinates or any other Ekos UI setting. Read these from canonical sources: DBus on `Ekos.*`, INDI properties, or read-only access to `~/.local/share/kstars/userdb.sqlite` (`opticaltrainsettings.fileDirectoryT` / `placeholderFormatT`).
- `routes/capture_ekos.py::_esq_for_jobs` only writes a tag into the generated ESQ if the caller passed an explicit value; otherwise Ekos uses its own settings.

When you add a new endpoint or change an existing one, treat any write to Ekos/INDI/PHD2 as a potential regression of this rule and double-check.

## Tooling: uv (not pip)

This project uses [`uv`](https://github.com/astral-sh/uv) as the package manager. There is **no** `requirements.txt`; dependencies live in `pyproject.toml` and lock in `uv.lock`. The Makefile is the canonical entry to all commands:

```bash
make install     # uv sync — install/update env from uv.lock
make run-app     # uv run astroarch_bridge — start the daemon
make run-tests   # uv run pytest -s tests/
```

Direct equivalents if you can't use make: `uv sync`, `uv run astroarch_bridge`, `uv run pytest -s tests/test_state.py::test_name`.

Python is pinned to `>=3.13,<3.14` in `pyproject.toml`. On AstroArch installs that still use the system Python 3.14, `scripts/install_deps_pacman.sh` is the workaround (installs deps via pacman, then bridge with `--no-pip`).

## Entry point and request lifecycle

`python -m astroarch_bridge` (or the `astroarch-bridge` console script) runs `astroarch_bridge/__main__.py::main()`, which boots `uvicorn` with `astroarch_bridge.app:create_app` as factory. `create_app()` is the single place where everything is wired:

1. `Settings` is loaded (`config.py`, pydantic-settings, reads `ASTROARCH_*` env vars). Token comes from env or auto-generates to `~/.config/astroarch-bridge/token`.
2. `StateManager` (`state.py`) is the in-memory mirror of INDI properties, PHD2 live data, last frame, and INDI messages. Everything else reads through it.
3. Two `WsHub`s (`ws/hub.py`) — one for `/ws/state` JSON snapshots, one for `/ws/frames` binary JPEG frames — get listener functions registered on the `StateManager`. Any state mutation broadcasts to subscribers.
4. `IndiClient` (`indi/client.py`) and `Phd2Client` (`phd2/client.py`) run as long-lived asyncio tasks. They auto-reconnect; their state changes feed back into `StateManager` through callbacks set up in `create_app`.
5. All REST routers in `routes/` are mounted under `/api/<area>/`. Auth is a single bearer-token middleware (`auth.py`) that protects every router via `Depends(require_token)`.

The deps container (`deps.py::Bridge`) is what handlers receive via `Depends(get_bridge)` and gives them `bridge.state`, `bridge.indi`, `bridge.phd2`.

## Where logic lives

- `routes/system.py` — system control, master Ekos toggle, **launch/kill of KStars and PHD2 GUI apps**. The launch path goes through `_user_graphical_env()` which reads `DISPLAY`/`XAUTHORITY`/`WAYLAND_DISPLAY` from a desktop process (`plasmashell`/`kwin`/`gnome-shell`/...). Hardcoding `:0` won't work on most setups.
- `routes/align.py` — plate solving via `Ekos.Align.captureAndSolve` over DBus + a fallback that drives `solve-field` directly. Calls to Q_NOREPLY DBus methods (e.g. `setSolverAction`) must be doubled with a sleep gap; see the comment in `ekos_capture_and_solve` for the rationale.
- `routes/capture_ekos.py` — generates `.esq` files for `Ekos.Capture.loadSequenceQueue`. **Reads** the user's `fileDirectoryT` / `placeholderFormatT` from `userdb.sqlite` via `_read_ekos_capture_settings()` instead of forcing values.
- `routes/focuser_ekos.py` — Ekos-native autofocus driver. Uses a `dbus-monitor` subprocess to intercept `newHFR`/`newStatus`/`newLog` signals because qdbus6 has no subscribe option.
- `routes/guide.py` — PHD2 RPC wrappers + the live guide-star image endpoint (decodes uint16 buffer from `get_star_image`, stretches, returns PNG).
- `indi/protocol.py` + `indi/client.py` — a custom incremental XML parser (no external INDI lib). The `enableBLOB Also` call in `client.py` is what makes the bridge a *secondary* client to Ekos.
- `images/processor.py::_percentile_stretch` — PixInsight-style adaptive STF (median-based MTF midtone). Used for plate solve previews and guide-star image. **Don't** revert to fixed midtone — bright skies blow out.

## Ekos DBus quirks worth knowing

- Many `Ekos.*` setters are Q_NOREPLY (fire-and-forget). When you need a setter immediately followed by a sync method, double the setter call with `asyncio.sleep(0.1-0.25)` between them. See `routes/align.py` for the canonical pattern.
- `Ekos.Focus.status` returns the type `(i)` struct — qdbus6 needs `--literal` to print it, then parse the int with a regex.
- `Ekos.indiStatus` stays at `1=Pending` forever if even one driver in the active profile fails to connect. Don't gate "system active" on this — use `ekosStatus == 2` plus a count of devices the bridge sees.
- `qdbus6 --literal` is required for any method returning arrays or structs (e.g. `getSolutionResult`, `getTargetCoords`, `fov`, `telescopeInfo`).

## systemd unit gotcha (deploy/astroarch-bridge.service)

**Do not add `PrivateTmp=true` back to the unit.** With it set, the service sees an isolated `/tmp` and cannot read `/tmp/xauth_XXXXXX` (the SDDM-created X cookie), so launching KStars/PHD2 from `routes/system.py` fails with "could not connect to display :0". The comment in the unit file explains this; preserve it.

## Tests

`pytest` with `asyncio_mode=auto` (set in `tests/pytest.ini`). `tests/conftest.py` forces `ASTROARCH_TOKEN=test-token` so the auth middleware doesn't trigger token auto-generation. Tests are in `tests/test_*.py` and cover config, the INDI protocol parser, and the state manager.

Single test: `uv run pytest -s tests/test_state.py::test_name`.

## Related repos

- [astroarch-interface-app](https://github.com/Johannes1979I/astroarch-interface-app) — the Android Flutter app. App and bridge releases are version-locked; if you change a REST contract here, bump there too.
- [astroarch-interface](https://github.com/Johannes1979I/astroarch-interface) — the original monorepo, kept as combined history (read-only reference).

## Local diagnostics

`/api/system/gui_session_info` (no app needed, just `curl` with the bearer token) returns the DISPLAY / XAUTHORITY / session type the bridge detected. Use this first whenever `launch_kstars` or `launch_phd2` doesn't actually open a window.
