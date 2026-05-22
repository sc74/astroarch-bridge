#!/usr/bin/env python3
"""Tkinter dashboard for astroarch-bridge.

Shows:
- systemd user service status (running / stopped / failed)
- INDI / PHD2 connection (from the bridge)
- Device count, properties, WS clients
- Token + URL (with copy)
- Buttons: Start / Stop / Restart / Reload / Open log

Starts automatically when the user opens the session (autostart .desktop)
or by clicking the icon on the desktop.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import urllib.error
import urllib.request
import gettext
import locale
from pathlib import Path
from tkinter import messagebox, ttk

try:
    import qrcode  # type: ignore
    _HAS_QR = True
except ImportError:
    _HAS_QR = False

# --- TRANSLATION SETTINGS ---

# 1. Initialize and configure standard system environment locales
try:
    locale.setlocale(locale.LC_ALL, "")
except Exception:
    pass

LOCALE_DIR = Path("/usr/share/astroarch-bridge/desktop_dashboard/locales")
if not LOCALE_DIR.exists():
    LOCALE_DIR = Path(__file__).parent / "locales"

# 2. Utilize Object-Oriented gettext API for bulletproof Python thread safety
try:
    current_locale, _encoding = locale.getlocale()
    lang = [current_locale.split('_')[0]] if current_locale else ['it']
    translation = gettext.translation(
        domain="base", localedir=str(LOCALE_DIR), languages=lang, fallback=True
    )
    _ = translation.gettext
except Exception as e:
    print(f"Translation engine initialization failed, using runtime fallback: {e}")
    _ = gettext.gettext

SERVICE = "astroarch-bridge.service"
TOKEN_FILE = Path.home() / ".config" / "astroarch-bridge" / "token"
REFRESH_MS = 2000

# Hotspot IP defined by AstroArch (create_ap.sh / NetworkManager shared mode)
HOTSPOT_IP = "10.42.0.1"
# Default port if the systemd override is not readable
DEFAULT_PORT = 8765


def _read_service_port() -> int:
    """Reads ASTROARCH_PORT from the current user's systemd drop-in.

    Path: ~/.config/systemd/user/astroarch-bridge.service.d/override.conf
    Expected line: Environment=ASTROARCH_PORT=XXXX
    Returns DEFAULT_PORT if the file is missing or malformed.
    """
    override = (
        Path.home()
        / ".config"
        / "systemd"
        / "user"
        / "astroarch-bridge.service.d"
        / "override.conf"
    )
    if override.exists():
        for line in override.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("Environment=ASTROARCH_PORT="):
                try:
                    return int(line.split("=", 2)[2])
                except (IndexError, ValueError):
                    pass
    # Fallback: environment variable (useful during development)
    try:
        return int(os.environ.get("ASTROARCH_PORT", DEFAULT_PORT))
    except ValueError:
        return DEFAULT_PORT


def _best_ip() -> str:
    """Determines the best IP to expose in the dashboard / QR.

    Priority:
    0. Tailscale IP (100.x.y.z) — reachable from ANYWHERE (preferred for the QR
       so the mobile app connects from home, observatory or mobile data alike).
    1. Active WiFi hotspot (10.42.0.1 assigned to wlan0 by NetworkManager)
    2. Primary IP of wlan0 if connected to a modem/router
    3. Primary IP of eth0 / other physical interface
    4. Fallback 127.0.0.1
    """
    # 0. Tailscale (best: works from any network). The QR/URL should point here
    #    so a single scan configures the app for use anywhere.
    try:
        r = subprocess.run(
            ["tailscale", "ip", "-4"],
            capture_output=True, text=True, timeout=2,
        )
        if r.returncode == 0 and r.stdout.strip():
            ip = r.stdout.strip().splitlines()[0].strip()
            if ip and not ip.startswith("127."):
                return ip
    except Exception:
        pass

    # 1. Check if the AstroArch hotspot is active (NM connection type=wifi mode=ap)
    try:
        r = subprocess.run(
            ["nmcli", "-t", "-f", "NAME,DEVICE,STATE", "connection", "show", "--active"],
            capture_output=True, text=True, timeout=3,
        )
        if r.returncode == 0:
            for line in r.stdout.splitlines():
                # line format: name:device:state
                parts = line.split(":")
                if len(parts) >= 3 and parts[1] == "wlan0" and parts[2] == "activated":
                    # Verify it is the hotspot (ap mode)
                    r2 = subprocess.run(
                        ["nmcli", "-t", "-f", "802-11-wireless.mode",
                         "connection", "show", parts[0]],
                        capture_output=True, text=True, timeout=3,
                    )
                    if r2.returncode == 0 and "ap" in r2.stdout.lower():
                        return HOTSPOT_IP
    except Exception:
        pass

    # 2 & 3. Look for IP on wlan0, then on any physical interface
    for iface in ("wlan0", "eth0", ""):
        try:
            cmd = ["ip", "-4", "-o", "addr", "show"]
            if iface:
                cmd += ["dev", iface]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
            for line in r.stdout.splitlines():
                parts = line.split()
                # parts[1] = interface, parts[3] = addr/prefix
                if len(parts) >= 4:
                    ip = parts[3].split("/")[0]
                    if ip.startswith("127.") or ip.startswith("169.254."):
                        continue
                    return ip
        except Exception:
            pass

    return "127.0.0.1"


# Palette
BG = "#0a0d12"
PANEL = "#121821"
PANEL2 = "#1a212d"
LINE = "#222b3a"
TEXT = "#e6eaf2"
MUTED = "#8a93a6"
ACCENT = "#f5a623"
ACCENT2 = "#5fb7ff"
OK = "#3ed598"
WARN = "#ffb454"
ERR = "#ff5b6e"


def systemctl(*args, capture=True):
    cmd = ["systemctl", "--user", *args]
    res = subprocess.run(cmd, capture_output=capture, text=True, timeout=10)
    return res


def service_status() -> str:
    """Returns 'active', 'inactive', 'failed', 'activating', or 'unknown'."""
    res = systemctl("is-active", SERVICE)
    return res.stdout.strip() or "unknown"


def read_token() -> str:
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    return ""


def http_json(url: str, token: str, timeout: float = 2.0) -> dict | None:
    try:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, json.JSONDecodeError):
        return None


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title(_("Astroarch Bridge — Dashboard"))
        root.configure(bg=BG)
        root.geometry("520x540")
        root.minsize(480, 480)

        try:
            root.wm_wmclass("astroarchbridge", "AstroarchBridge")
        except Exception:
            pass

        try:
            icon_path = Path("/usr/share/astroarch-bridge/desktop_dashboard/astroarch_bridge.png")

            if icon_path.exists():
                self.window_icon = tk.PhotoImage(file=str(icon_path))
                root.wm_iconphoto(True, self.window_icon)
        except Exception as e:
            print(f"Erreur chargement icône : {e}")

        title_font = tkfont.Font(family="DejaVu Sans", size=15, weight="bold")
        body_font = tkfont.Font(family="DejaVu Sans", size=10)
        mono_font = tkfont.Font(family="DejaVu Sans Mono", size=10)
        small_font = tkfont.Font(family="DejaVu Sans", size=9)

        outer = tk.Frame(root, bg=BG, padx=18, pady=14)
        outer.pack(fill="both", expand=True)

        # Header
        hdr = tk.Frame(outer, bg=BG)
        hdr.pack(fill="x")
        tk.Label(hdr, text=_("Astroarch "), bg=BG, fg=TEXT, font=title_font).pack(side="left")
        tk.Label(hdr, text=_("Bridge"), bg=BG, fg=ACCENT, font=title_font).pack(side="left")
        tk.Label(hdr, text=_("  · Zarletti-Osservatorio Jupiter"),
                 bg=BG, fg=MUTED, font=small_font).pack(side="left")

        # Big status card
        self.status_card = tk.Frame(outer, bg=PANEL, highlightbackground=LINE,
                                    highlightthickness=1, padx=16, pady=14)
        self.status_card.pack(fill="x", pady=(14, 0))
        self.status_dot_canvas = tk.Canvas(self.status_card, width=14, height=14,
                                           bg=PANEL, highlightthickness=0)
        self.status_dot_canvas.pack(side="left", padx=(0, 10))
        self.status_dot = self.status_dot_canvas.create_oval(2, 2, 12, 12, fill=MUTED, outline="")
        self.status_text = tk.Label(self.status_card, text="…",
                                    bg=PANEL, fg=TEXT,
                                    font=tkfont.Font(family="DejaVu Sans", size=14, weight="bold"))
        self.status_text.pack(side="left")

        # INDI/PHD2/devices grid
        info = tk.Frame(outer, bg=BG)
        info.pack(fill="x", pady=(10, 0))
        self.indi_var = tk.StringVar(value="—")
        self.phd2_var = tk.StringVar(value="—")
        self.dev_var = tk.StringVar(value="—")
        self.props_var = tk.StringVar(value="—")
        self.ws_var = tk.StringVar(value="—")
        self.frames_var = tk.StringVar(value="—")

        def cell(parent, label, var, col):
            f = tk.Frame(parent, bg=PANEL, padx=10, pady=8,
                         highlightbackground=LINE, highlightthickness=1)
            f.grid(row=col // 3, column=col % 3, sticky="nsew", padx=3, pady=3)
            tk.Label(f, text=label, bg=PANEL, fg=MUTED, font=small_font, anchor="w")\
                .pack(fill="x")
            tk.Label(f, textvariable=var, bg=PANEL, fg=TEXT,
                     font=tkfont.Font(family="DejaVu Sans", size=11, weight="bold"),
                     anchor="w").pack(fill="x")

        for i in range(3):
            info.columnconfigure(i, weight=1)
        cell(info, _("INDI"), self.indi_var, 0)
        cell(info, _("PHD2"), self.phd2_var, 1)
        cell(info, _("DEVICES"), self.dev_var, 2)
        cell(info, _("PROPERTIES"), self.props_var, 3)
        cell(info, _("WS STATE"), self.ws_var, 4)
        cell(info, _("WS FRAMES"), self.frames_var, 5)

        # URL + token
        access = tk.LabelFrame(outer, text=_(" App access "), bg=BG, fg=MUTED,
                               font=small_font, bd=1, relief="solid",
                               labelanchor="nw", padx=10, pady=8)
        access.configure(highlightbackground=LINE)
        access.pack(fill="x", pady=(12, 0))

        urlrow = tk.Frame(access, bg=BG)
        urlrow.pack(fill="x", pady=2)
        tk.Label(urlrow, text=_("URL"), bg=BG, fg=MUTED, font=small_font, width=7, anchor="w")\
            .pack(side="left")
        self.url_var = tk.StringVar(value=self._auto_url())
        tk.Entry(urlrow, textvariable=self.url_var, bg=PANEL2, fg=TEXT,
                 font=mono_font, relief="flat", insertbackground=TEXT,
                 readonlybackground=PANEL2, state="readonly").pack(side="left", fill="x", expand=True)
        tk.Button(urlrow, text="↺", command=self._refresh_url, bg=PANEL2, fg=ACCENT2,
                  font=small_font, relief="flat", padx=8,
                  cursor="hand2").pack(side="left", padx=(4, 0))

        tokrow = tk.Frame(access, bg=BG)
        tokrow.pack(fill="x", pady=2)
        tk.Label(tokrow, text=_("Token"), bg=BG, fg=MUTED, font=small_font, width=7, anchor="w")\
            .pack(side="left")
        self.token_var = tk.StringVar(value=read_token())
        tk.Entry(tokrow, textvariable=self.token_var, bg=PANEL2, fg=TEXT,
                 font=mono_font, relief="flat", insertbackground=TEXT,
                 readonlybackground=PANEL2, state="readonly").pack(side="left", fill="x", expand=True)
        tk.Button(tokrow, text=_("Copy"), command=self._copy_token, bg=ACCENT2, fg="black",
                  font=small_font, relief="flat", padx=10).pack(side="left", padx=(6, 0))

        # QR code for mobile app
        qrwrap = tk.LabelFrame(outer, text=_(" QR CODE for mobile app "), bg=BG, fg=MUTED,
                               font=small_font, bd=1, relief="solid",
                               labelanchor="nw", padx=10, pady=8)
        qrwrap.pack(fill="x", pady=(10, 0))
        qrrow = tk.Frame(qrwrap, bg=BG)
        qrrow.pack(fill="x")
        self.qr_canvas = tk.Label(qrrow, bg="white", width=180, height=180)
        self.qr_canvas.pack(side="left", padx=(0, 12))
        qrinfo = tk.Frame(qrrow, bg=BG)
        qrinfo.pack(side="left", fill="both", expand=True)
        tk.Label(qrinfo, text=_("Open the Astroarch Interface app,"),
                 bg=BG, fg=TEXT, font=body_font, anchor="w", justify="left")\
            .pack(fill="x", anchor="w")
        tk.Label(qrinfo, text=_("tap the 📷 SCAN QR button on the"),
                 bg=BG, fg=TEXT, font=body_font, anchor="w", justify="left")\
            .pack(fill="x", anchor="w")
        tk.Label(qrinfo, text=_("Login screen."),
                 bg=BG, fg=TEXT, font=body_font, anchor="w", justify="left")\
            .pack(fill="x", anchor="w")
        tk.Label(qrinfo, text=_("Host, port and token will be"),
                 bg=BG, fg=MUTED, font=small_font, anchor="w", justify="left")\
            .pack(fill="x", anchor="w", pady=(8, 0))
        tk.Label(qrinfo, text=_("filled in automatically."),
                 bg=BG, fg=MUTED, font=small_font, anchor="w", justify="left")\
            .pack(fill="x", anchor="w")
        self.qr_status_var = tk.StringVar(value="")
        tk.Label(qrinfo, textvariable=self.qr_status_var, bg=BG, fg=ACCENT2,
                 font=small_font, anchor="w", justify="left")\
            .pack(fill="x", anchor="w", pady=(8, 0))
        self._qr_image_ref = None  # keep reference to prevent GC
        self._render_qr()

        # Buttons - "Connect/Disconnect" act on the systemd service
        btns = tk.Frame(outer, bg=BG)
        btns.pack(fill="x", pady=(14, 0))
        self._btn(btns, _("▶ CONNECT"), OK, self.start, 0)
        self._btn(btns, _("■ DISCONNECT"), ERR, self.stop, 1)
        self._btn(btns, _("↻ Restart"), ACCENT, self.restart, 2)
        self._btn(btns, _("≡ Log"), PANEL2, self.show_log, 3)
        for i in range(4):
            btns.columnconfigure(i, weight=1)

        # Log preview
        logbar = tk.Frame(outer, bg=BG)
        logbar.pack(fill="x", pady=(10, 4))
        tk.Label(logbar, text=_("LATEST LOG LINES"), bg=BG, fg=MUTED, font=small_font).pack(side="left")
        self.log_box = tk.Text(outer, bg="#05080e", fg=MUTED, font=mono_font,
                               height=8, relief="flat", borderwidth=1,
                               highlightbackground=LINE, highlightthickness=1)
        self.log_box.pack(fill="both", expand=True, pady=(0, 6))
        self.log_box.configure(state="disabled")

        # Footer
        ft = tk.Frame(outer, bg=BG)
        ft.pack(fill="x")
        self.footer_var = tk.StringVar(value="")
        tk.Label(ft, textvariable=self.footer_var, bg=BG, fg=MUTED, font=small_font)\
            .pack(side="left")

        # Stop event for clean shutdown
        self._stop_evt = threading.Event()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._refresh_async()
        root.after(REFRESH_MS, self._tick)

    def _btn(self, parent, label, color, cmd, col):
        b = tk.Button(parent, text=label, command=cmd, bg=color, fg="black",
                      font=tkfont.Font(family="DejaVu Sans", size=10, weight="bold"),
                      relief="flat", padx=12, pady=8, cursor="hand2",
                      activebackground=color)
        b.grid(row=0, column=col, sticky="ew", padx=2)
        return b

    def _auto_url(self) -> str:
        """Builds the connection URL: detected network IP + service port."""
        ip = _best_ip()
        port = _read_service_port()
        return f"http://{ip}:{port}"

    def _refresh_url(self):
        """Refreshes the URL (called by the ↺ button and the network tick)."""
        new_url = self._auto_url()
        self.url_var.set(new_url)
        self._render_qr()
        self.footer_var.set(_("URL updated → {}").format(new_url))

    # --- Actions ---
    def start(self):
        threading.Thread(target=self._svc_action, args=("start",), daemon=True).start()

    def stop(self):
        if not messagebox.askyesno(_("Confirm"),
                                   _("Stop the bridge? The mobile app will lose the connection.")):
            return
        threading.Thread(target=self._svc_action, args=("stop",), daemon=True).start()

    def restart(self):
        threading.Thread(target=self._svc_action, args=("restart",), daemon=True).start()

    def show_log(self):
        # Open live log in terminal (xterm/gnome-terminal/konsole)
        for term in (("konsole", "-e"), ("gnome-terminal", "--"), ("xterm", "-e")):
            try:
                subprocess.Popen([*term, "journalctl", "--user", "-u", SERVICE, "-f"])
                return
            except FileNotFoundError:
                continue
        messagebox.showwarning(_("Log"),
            _("No terminal found. Open manually:\n  journalctl --user -u astroarch-bridge -f"))

    def _svc_action(self, action: str):
        try:
            res = systemctl(action, SERVICE)
            self.footer_var.set(
                _("systemctl {}: rc={} ").format(action, res.returncode)
                + (res.stderr.strip()[:80] if res.returncode else "ok"))
        except Exception as e:
            self.footer_var.set(_("error: {}").format(e))

    def _copy_token(self):
        tok = self.token_var.get()
        if not tok:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(tok)
        self.footer_var.set(_("Token copied to clipboard"))

    # --- QR code ---
    def _qr_payload(self) -> str:
        """JSON payload that the app reads to self-configure."""
        url = self.url_var.get() or self._auto_url()
        # Extract host:port from the URL
        try:
            from urllib.parse import urlparse
            u = urlparse(url)
            host = u.hostname or "127.0.0.1"
            port = u.port or _read_service_port()
        except Exception:
            host, port = "127.0.0.1", _read_service_port()
        return json.dumps({
            "v": 1,
            "type": "astroarch-bridge",
            "host": host,
            "port": port,
            "token": self.token_var.get(),
        }, separators=(",", ":"))

    def _render_qr(self):
        if not _HAS_QR:
            self.qr_canvas.configure(text=_("qrcode lib\nnot installed"),
                                     fg=ERR, bg=PANEL, width=24, height=12)
            return
        try:
            qr = qrcode.QRCode(version=None,
                               error_correction=qrcode.constants.ERROR_CORRECT_M,
                               box_size=4, border=2)
            qr.add_data(self._qr_payload())
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
            # Render as PhotoImage (tkinter)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            buf.seek(0)
            from tkinter import PhotoImage
            self._qr_image_ref = PhotoImage(data=buf.getvalue())
            self.qr_canvas.configure(image=self._qr_image_ref, width=200, height=200, text="")
            self.qr_status_var.set(_("QR updated · {} bytes").format(len(self._qr_payload())))
        except Exception as e:
            self.qr_canvas.configure(text=_("QR error: {}").format(e), bg=PANEL, fg=ERR)
            self.qr_status_var.set("")

    # --- Refresh loop ---
    def _tick(self):
        if self._stop_evt.is_set():
            return
        threading.Thread(target=self._refresh_async, daemon=True).start()
        self.root.after(REFRESH_MS, self._tick)

    def _refresh_async(self):
        st = service_status()
        # Refresh IP/port at each tick (changes if hotspot is enabled/disabled)
        new_url = self._auto_url()
        self.root.after(0, lambda u=new_url: self._maybe_update_url(u))
        self.root.after(0, lambda: self._update_status(st))
        if st == "active":
            url = self.url_var.get() or self._auto_url()
            tok = self.token_var.get() or read_token()
            if tok:
                conn = http_json(f"{url.rstrip('/')}/api/system/connections", tok)
                snap = http_json(f"{url.rstrip('/')}/api/system/snapshot", tok, timeout=4.0)
                if conn or snap:
                    self.root.after(0, lambda: self._update_state(conn, snap))
        # Log tail
        log = self._tail_log(20)
        self.root.after(0, lambda: self._update_log(log))

    def _tail_log(self, lines: int) -> str:
        try:
            r = subprocess.run(
                ["journalctl", "--user", "-u", SERVICE, "-n", str(lines), "--no-pager"],
                capture_output=True, text=True, timeout=5)
            return r.stdout or ""
        except Exception as e:
            return _("(log error: {})").format(e)

    def _maybe_update_url(self, new_url: str):
        """Updates the URL and QR only if the IP/port has changed."""
        if self.url_var.get() != new_url:
            self.url_var.set(new_url)
            self._render_qr()

    def _update_status(self, st: str):
        color = {"active": OK, "activating": WARN, "reloading": WARN,
                 "inactive": MUTED, "failed": ERR}.get(st, MUTED)
        labels = {"active": _("Service active"), "inactive": _("Service stopped"),
                  "failed": _("Service error"), "activating": _("Starting…")}
        self.status_dot_canvas.itemconfig(self.status_dot, fill=color)
        self.status_text.configure(text=labels.get(st, st), fg=color)

    def _update_state(self, conn: dict | None, snap: dict | None):
        if conn:
            self.indi_var.set(conn.get("indi", "—"))
            self.phd2_var.set(conn.get("phd2", "—"))
        if snap:
            self.dev_var.set(str(len(snap.get("devices", []))))
            self.props_var.set(str(len(snap.get("properties", []))))
        # WS clients (snapshot does not expose them, leaving placeholder)
        self.ws_var.set("hub ok")
        self.frames_var.set("hub ok")

    def _update_log(self, text: str):
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.insert("1.0", text)
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _on_close(self):
        self._stop_evt.set()
        self.root.destroy()


def main():
    root = tk.Tk()
    # Set dark theme for ttk widgets
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
