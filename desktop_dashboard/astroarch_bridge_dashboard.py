#!/usr/bin/env python3
"""Dashboard tkinter per astroarch-bridge.

Mostra:
- Stato servizio systemd user (running / stopped / failed)
- Connessione INDI / PHD2 (dal bridge)
- Numero device, properties, clients WS
- Token + URL (con copy)
- Bottoni: Start / Stop / Restart / Reload / Apri log

Si avvia automaticamente quando l'utente apre la sessione (autostart .desktop)
oppure cliccando l'icona sul desktop.
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
from pathlib import Path
from tkinter import messagebox, ttk

try:
    import qrcode  # type: ignore
    _HAS_QR = True
except ImportError:
    _HAS_QR = False

SERVICE = "astroarch-bridge.service"
DEFAULT_URL = "http://127.0.0.1:8765"
TOKEN_FILE = Path.home() / ".config" / "astroarch-bridge" / "token"
REFRESH_MS = 2000

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
        root.title("Astroarch Bridge — Dashboard")
        root.configure(bg=BG)
        root.geometry("520x540")
        root.minsize(480, 480)

        title_font = tkfont.Font(family="DejaVu Sans", size=15, weight="bold")
        body_font = tkfont.Font(family="DejaVu Sans", size=10)
        mono_font = tkfont.Font(family="DejaVu Sans Mono", size=10)
        small_font = tkfont.Font(family="DejaVu Sans", size=9)

        outer = tk.Frame(root, bg=BG, padx=18, pady=14)
        outer.pack(fill="both", expand=True)

        # Header
        hdr = tk.Frame(outer, bg=BG)
        hdr.pack(fill="x")
        tk.Label(hdr, text="Astroarch ", bg=BG, fg=TEXT, font=title_font).pack(side="left")
        tk.Label(hdr, text="Bridge", bg=BG, fg=ACCENT, font=title_font).pack(side="left")
        tk.Label(hdr, text="  · Zarletti-Osservatorio Jupiter",
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
        cell(info, "INDI", self.indi_var, 0)
        cell(info, "PHD2", self.phd2_var, 1)
        cell(info, "DEVICES", self.dev_var, 2)
        cell(info, "PROPERTIES", self.props_var, 3)
        cell(info, "WS STATE", self.ws_var, 4)
        cell(info, "WS FRAMES", self.frames_var, 5)

        # URL + token
        access = tk.LabelFrame(outer, text=" Accesso app ", bg=BG, fg=MUTED,
                               font=small_font, bd=1, relief="solid",
                               labelanchor="nw", padx=10, pady=8)
        access.configure(highlightbackground=LINE)
        access.pack(fill="x", pady=(12, 0))

        urlrow = tk.Frame(access, bg=BG)
        urlrow.pack(fill="x", pady=2)
        tk.Label(urlrow, text="URL", bg=BG, fg=MUTED, font=small_font, width=7, anchor="w")\
            .pack(side="left")
        self.url_var = tk.StringVar(value=self._auto_url())
        tk.Entry(urlrow, textvariable=self.url_var, bg=PANEL2, fg=TEXT,
                 font=mono_font, relief="flat", insertbackground=TEXT,
                 readonlybackground=PANEL2, state="readonly").pack(side="left", fill="x", expand=True)

        tokrow = tk.Frame(access, bg=BG)
        tokrow.pack(fill="x", pady=2)
        tk.Label(tokrow, text="Token", bg=BG, fg=MUTED, font=small_font, width=7, anchor="w")\
            .pack(side="left")
        self.token_var = tk.StringVar(value=read_token())
        tk.Entry(tokrow, textvariable=self.token_var, bg=PANEL2, fg=TEXT,
                 font=mono_font, relief="flat", insertbackground=TEXT,
                 readonlybackground=PANEL2, state="readonly").pack(side="left", fill="x", expand=True)
        tk.Button(tokrow, text="Copia", command=self._copy_token, bg=ACCENT2, fg="black",
                  font=small_font, relief="flat", padx=10).pack(side="left", padx=(6, 0))

        # QR code per app mobile
        qrwrap = tk.LabelFrame(outer, text=" QR CODE per app mobile ", bg=BG, fg=MUTED,
                               font=small_font, bd=1, relief="solid",
                               labelanchor="nw", padx=10, pady=8)
        qrwrap.pack(fill="x", pady=(10, 0))
        qrrow = tk.Frame(qrwrap, bg=BG)
        qrrow.pack(fill="x")
        self.qr_canvas = tk.Label(qrrow, bg="white", width=180, height=180)
        self.qr_canvas.pack(side="left", padx=(0, 12))
        qrinfo = tk.Frame(qrrow, bg=BG)
        qrinfo.pack(side="left", fill="both", expand=True)
        tk.Label(qrinfo, text="Apri l'app Astroarch Interface,",
                 bg=BG, fg=TEXT, font=body_font, anchor="w", justify="left")\
            .pack(fill="x", anchor="w")
        tk.Label(qrinfo, text="tappa il pulsante 📷 SCAN QR sulla",
                 bg=BG, fg=TEXT, font=body_font, anchor="w", justify="left")\
            .pack(fill="x", anchor="w")
        tk.Label(qrinfo, text="schermata Login.",
                 bg=BG, fg=TEXT, font=body_font, anchor="w", justify="left")\
            .pack(fill="x", anchor="w")
        tk.Label(qrinfo, text="Host, porta e token verranno",
                 bg=BG, fg=MUTED, font=small_font, anchor="w", justify="left")\
            .pack(fill="x", anchor="w", pady=(8, 0))
        tk.Label(qrinfo, text="popolati automaticamente.",
                 bg=BG, fg=MUTED, font=small_font, anchor="w", justify="left")\
            .pack(fill="x", anchor="w")
        self.qr_status_var = tk.StringVar(value="")
        tk.Label(qrinfo, textvariable=self.qr_status_var, bg=BG, fg=ACCENT2,
                 font=small_font, anchor="w", justify="left")\
            .pack(fill="x", anchor="w", pady=(8, 0))
        self._qr_image_ref = None  # tieni reference per non far GC
        self._render_qr()

        # Buttons - "Connetti/Disconnetti" agiscono sul servizio systemd:
        # quando il servizio gira, l'app mobile può connettersi.
        btns = tk.Frame(outer, bg=BG)
        btns.pack(fill="x", pady=(14, 0))
        self._btn(btns, "▶ CONNETTI", OK, self.start, 0)
        self._btn(btns, "■ DISCONNETTI", ERR, self.stop, 1)
        self._btn(btns, "↻ Riavvia", ACCENT, self.restart, 2)
        self._btn(btns, "≡ Log", PANEL2, self.show_log, 3)
        for i in range(4):
            btns.columnconfigure(i, weight=1)

        # Log preview
        logbar = tk.Frame(outer, bg=BG)
        logbar.pack(fill="x", pady=(10, 4))
        tk.Label(logbar, text="ULTIME RIGHE LOG", bg=BG, fg=MUTED, font=small_font).pack(side="left")
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
        # Tenta di leggere ip Tailscale: tailscale ip -4
        try:
            r = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=2)
            ip = r.stdout.strip().splitlines()[0] if r.returncode == 0 else "127.0.0.1"
        except Exception:
            ip = "127.0.0.1"
        return f"http://{ip}:8765"

    # --- Actions ---
    def start(self):
        threading.Thread(target=self._svc_action, args=("start",), daemon=True).start()

    def stop(self):
        if not messagebox.askyesno("Conferma",
                                   "Fermare il bridge? L'app mobile perderà la connessione."):
            return
        threading.Thread(target=self._svc_action, args=("stop",), daemon=True).start()

    def restart(self):
        threading.Thread(target=self._svc_action, args=("restart",), daemon=True).start()

    def show_log(self):
        # apri log live in terminale (xterm/gnome-terminal/konsole)
        for term in (("konsole", "-e"), ("gnome-terminal", "--"), ("xterm", "-e")):
            try:
                subprocess.Popen([*term, "journalctl", "--user", "-u", SERVICE, "-f"])
                return
            except FileNotFoundError:
                continue
        messagebox.showwarning("Log",
            "Nessun terminale trovato. Apri manualmente:\n  journalctl --user -u astroarch-bridge -f")

    def _svc_action(self, action: str):
        try:
            res = systemctl(action, SERVICE)
            self.footer_var.set(
                f"systemctl {action}: rc={res.returncode} "
                + (res.stderr.strip()[:80] if res.returncode else "ok"))
        except Exception as e:
            self.footer_var.set(f"errore: {e}")

    def _copy_token(self):
        tok = self.token_var.get()
        if not tok:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(tok)
        self.footer_var.set("Token copiato negli appunti")

    # --- QR code ---
    def _qr_payload(self) -> str:
        """Payload JSON che l'app legge per autoconfigurarsi."""
        url = self.url_var.get() or DEFAULT_URL
        # Estrai host:porta dall'URL
        try:
            from urllib.parse import urlparse
            u = urlparse(url)
            host = u.hostname or "127.0.0.1"
            port = u.port or 8765
        except Exception:
            host, port = "127.0.0.1", 8765
        return json.dumps({
            "v": 1,
            "type": "astroarch-bridge",
            "host": host,
            "port": port,
            "token": self.token_var.get(),
        }, separators=(",", ":"))

    def _render_qr(self):
        if not _HAS_QR:
            self.qr_canvas.configure(text="qrcode lib\nnon installata",
                                     fg=ERR, bg=PANEL, width=24, height=12)
            return
        try:
            qr = qrcode.QRCode(version=None,
                               error_correction=qrcode.constants.ERROR_CORRECT_M,
                               box_size=4, border=2)
            qr.add_data(self._qr_payload())
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
            # Rendering a PhotoImage (tkinter)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            buf.seek(0)
            from tkinter import PhotoImage
            self._qr_image_ref = PhotoImage(data=buf.getvalue())
            self.qr_canvas.configure(image=self._qr_image_ref, width=200, height=200, text="")
            self.qr_status_var.set(f"QR aggiornato · {len(self._qr_payload())} byte")
        except Exception as e:
            self.qr_canvas.configure(text=f"QR error: {e}", bg=PANEL, fg=ERR)
            self.qr_status_var.set("")

    # --- Refresh loop ---
    def _tick(self):
        if self._stop_evt.is_set():
            return
        threading.Thread(target=self._refresh_async, daemon=True).start()
        self.root.after(REFRESH_MS, self._tick)

    def _refresh_async(self):
        st = service_status()
        self.root.after(0, lambda: self._update_status(st))
        if st == "active":
            url = self.url_var.get() or DEFAULT_URL
            tok = self.token_var.get() or read_token()
            if tok:
                conn = http_json(f"{url.replace('http://', 'http://').rstrip('/')}/api/system/connections", tok)
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
            return f"(log error: {e})"

    def _update_status(self, st: str):
        color = {"active": OK, "activating": WARN, "reloading": WARN,
                 "inactive": MUTED, "failed": ERR}.get(st, MUTED)
        labels = {"active": "Servizio attivo", "inactive": "Servizio fermato",
                  "failed": "Servizio in errore", "activating": "Avvio in corso…"}
        self.status_dot_canvas.itemconfig(self.status_dot, fill=color)
        self.status_text.configure(text=labels.get(st, st), fg=color)

    def _update_state(self, conn: dict | None, snap: dict | None):
        if conn:
            self.indi_var.set(conn.get("indi", "—"))
            self.phd2_var.set(conn.get("phd2", "—"))
        if snap:
            self.dev_var.set(str(len(snap.get("devices", []))))
            self.props_var.set(str(len(snap.get("properties", []))))
        # WS clients (snapshot non li espone, lasciamo placeholder)
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
    # Imposta tema scuro per ttk widgets
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
